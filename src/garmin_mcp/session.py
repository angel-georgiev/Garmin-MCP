"""Lazy, thread-safe Garmin Connect session shared by every tool."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import TypeVar

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

LOGIN_HINT = (
    "Run `garmin-mcp login` once in a terminal to sign in (it can prompt for an MFA "
    "code) and cache tokens, or set GARMIN_EMAIL and GARMIN_PASSWORD for the server."
)


class GarminError(RuntimeError):
    """User-facing error surfaced through MCP tool results."""


class MfaRequired(GarminError):
    """Raised when Garmin wants an MFA code the server cannot ask for."""


def _mfa_not_available() -> str:
    raise MfaRequired(
        "Garmin Connect is asking for a multi-factor authentication code, which this "
        "server cannot prompt for over stdio. " + LOGIN_HINT
    )


class GarminSession:
    """Owns a single ``Garmin`` client and serializes access to it.

    ``garminconnect`` is synchronous and not safe to drive from several
    coroutines at once, so every call goes through a lock and runs in a worker
    thread to keep the MCP event loop responsive.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Garmin | None = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return self._client is not None

    def _build_client(self) -> Garmin:
        return Garmin(
            email=self.settings.email,
            password=self.settings.password,
            is_cn=self.settings.is_cn,
            prompt_mfa=_mfa_not_available,
        )

    def _login_sync(self, *, force: bool = False) -> Garmin:
        with self._lock:
            if self._client is not None and not force:
                return self._client

            settings = self.settings
            if not settings.has_cached_tokens and not settings.has_credentials:
                raise GarminError(
                    f"No Garmin credentials and no cached tokens at {settings.tokenstore}. "
                    + LOGIN_HINT
                )

            client = self._build_client()
            try:
                client.login(str(settings.tokenstore))
            except MfaRequired:
                raise
            except GarminConnectTooManyRequestsError as exc:
                raise GarminError(
                    "Garmin is rate limiting the login (HTTP 429). Wait a few minutes "
                    "before retrying; repeated logins can lock the account temporarily."
                ) from exc
            except GarminConnectAuthenticationError as exc:
                raise GarminError(f"Garmin authentication failed: {exc}\n{LOGIN_HINT}") from exc
            except GarminConnectConnectionError as exc:
                raise GarminError(f"Could not reach Garmin Connect: {exc}") from exc

            self._client = client
            return client

    def logout(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.logout()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Logout failed: %s", exc)

    # -- calling -----------------------------------------------------------

    def _call_sync(self, func: Callable[[Garmin], T]) -> T:
        client = self._login_sync()
        with self._lock:
            try:
                return func(client)
            except GarminConnectAuthenticationError:
                logger.info("Cached session rejected, re-authenticating once.")
                client = self._login_sync(force=True)
                return func(client)

    async def call(self, func: Callable[[Garmin], T]) -> T:
        """Run ``func`` against the logged-in client in a worker thread."""
        try:
            return await asyncio.to_thread(self._call_sync, func)
        except GarminError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

    async def login(self) -> Garmin:
        return await asyncio.to_thread(self._login_sync)


def _translate(exc: Exception) -> GarminError:
    """Map library/HTTP failures onto messages a user can act on."""
    name = type(exc).__name__
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return GarminError(
            "Garmin is rate limiting this account (HTTP 429). Slow down and retry in a "
            "few minutes — Garmin's API has no documented quota, so back off generously."
        )
    if isinstance(exc, GarminConnectAuthenticationError):
        return GarminError(f"Garmin authentication failed: {exc}\n{LOGIN_HINT}")
    if isinstance(exc, GarminConnectConnectionError):
        return GarminError(f"Garmin Connect request failed: {exc}")
    if name == "GarminConnectNotFoundError":
        return GarminError(
            f"Garmin returned 404 for that request: {exc}. The id may be wrong, or your "
            "device/subscription may not record this metric."
        )
    if isinstance(exc, ValueError):
        return GarminError(str(exc))
    return GarminError(f"{name}: {exc}")
