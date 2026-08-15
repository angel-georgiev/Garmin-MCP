"""Single-user OAuth for exposing the server as a Claude custom connector.

The connector dialog's "Individual sign-in" needs the MCP server to speak OAuth.
This account has exactly one user — you — so the provider here is deliberately
small: Claude registers itself dynamically, you prove it's you by entering a
passphrase on a login page, and the server issues tokens scoped to that session.

What this is not: a multi-tenant identity provider. There are no user accounts,
no consent screen beyond the passphrase, and no delegation. The passphrase is
the only thing standing between the public tunnel URL and your health data, so
it must be long and unique — it is not your Garmin password.

The SDK's token handler already enforces PKCE, code expiry, redirect_uri
matching and client identity before calling into this provider, so the code
below concentrates on issuing, storing and expiring credentials.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

AUTH_CODE_TTL = 300  # 5 minutes, per RFC 6749 §10.5
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
LOGIN_SESSION_TTL = 900
SCOPE = "garmin:read"

# Login throttling: a public URL means anyone can reach the form.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


@dataclass
class _LoginSession:
    """An /authorize request parked while the browser proves who it is."""

    client_id: str
    params: AuthorizationParams
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > LOGIN_SESSION_TTL


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth provider guarding one account behind one passphrase."""

    def __init__(self, passphrase: str, base_url: str, storage: Path | None = None) -> None:
        if len(passphrase) < 12:
            raise ValueError(
                "The connector passphrase must be at least 12 characters. This is the "
                "only thing protecting a public URL that reads your health data."
            )
        self._passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self._storage = storage

        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self.login_sessions: dict[str, _LoginSession] = {}

        self._failed_attempts = 0
        self._locked_until = 0.0

        self._load()

    # -- persistence -------------------------------------------------------
    #
    # Tunnels drop and laptops sleep; without this every restart would force
    # you to re-add the connector. Auth codes are deliberately not persisted:
    # they live for five minutes and re-issuing one is free.

    def _load(self) -> None:
        if not self._storage or not self._storage.is_file():
            return
        try:
            raw = json.loads(self._storage.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read OAuth state (%s); starting fresh.", exc)
            return

        try:
            self.clients = {
                cid: OAuthClientInformationFull.model_validate(data)
                for cid, data in raw.get("clients", {}).items()
            }
            self.access_tokens = {
                tok: AccessToken.model_validate(data)
                for tok, data in raw.get("access_tokens", {}).items()
            }
            self.refresh_tokens = {
                tok: RefreshToken.model_validate(data)
                for tok, data in raw.get("refresh_tokens", {}).items()
            }
        except Exception as exc:  # noqa: BLE001 - never let bad state block startup
            logger.warning("Discarding unreadable OAuth state: %s", exc)
            self.clients, self.access_tokens, self.refresh_tokens = {}, {}, {}
            return

        self._expire_tokens()

    def _save(self) -> None:
        if not self._storage:
            return
        payload = {
            "clients": {cid: c.model_dump(mode="json") for cid, c in self.clients.items()},
            "access_tokens": {
                tok: t.model_dump(mode="json") for tok, t in self.access_tokens.items()
            },
            "refresh_tokens": {
                tok: t.model_dump(mode="json") for tok, t in self.refresh_tokens.items()
            },
        }
        try:
            self._storage.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._storage.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.chmod(0o600)
            tmp.replace(self._storage)
        except OSError as exc:  # pragma: no cover - disk problems shouldn't 500
            logger.warning("Could not persist OAuth state: %s", exc)

    def _expire_tokens(self) -> None:
        now = time.time()
        self.access_tokens = {
            tok: t
            for tok, t in self.access_tokens.items()
            if not t.expires_at or t.expires_at > now
        }
        self.refresh_tokens = {
            tok: t
            for tok, t in self.refresh_tokens.items()
            if not t.expires_at or t.expires_at > now
        }

    # -- client registration ----------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info
        self._save()
        logger.info("Registered OAuth client %s", client_info.client_name or client_info.client_id)

    # -- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the passphrase form."""
        self._prune_login_sessions()
        session_id = secrets.token_urlsafe(24)
        self.login_sessions[session_id] = _LoginSession(client_id=client.client_id, params=params)
        return f"{self.base_url}/login?session={session_id}"

    def _prune_login_sessions(self) -> None:
        self.login_sessions = {
            sid: s for sid, s in self.login_sessions.items() if not s.expired
        }

    @property
    def locked_out(self) -> bool:
        return time.time() < self._locked_until

    @property
    def lockout_remaining(self) -> int:
        return max(0, int(self._locked_until - time.time()))

    def check_passphrase(self, candidate: str) -> bool:
        """Constant-time passphrase check with lockout after repeated failures."""
        if self.locked_out:
            return False
        if hmac.compare_digest(candidate, self._passphrase):
            self._failed_attempts = 0
            return True
        self._failed_attempts += 1
        if self._failed_attempts >= MAX_ATTEMPTS:
            self._locked_until = time.time() + LOCKOUT_SECONDS
            self._failed_attempts = 0
            logger.warning("Too many failed connector logins; locking out for %ds", LOCKOUT_SECONDS)
        return False

    def complete_login(self, session_id: str) -> str:
        """Turn a proven login session into a redirect back to the client."""
        self._prune_login_sessions()
        session = self.login_sessions.pop(session_id, None)
        if session is None:
            raise AuthorizeError(
                error="invalid_request",
                error_description="This sign-in link expired. Reconnect the connector.",
            )

        params = session.params
        code = AuthorizationCode(
            code=f"code_{secrets.token_urlsafe(32)}",
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=session.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self.auth_codes[code.code] = code
        return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self.auth_codes.get(authorization_code)

    # -- token issuance ----------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = time.time()
        access = AccessToken(
            token=f"gmt_{secrets.token_urlsafe(32)}",
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL),
            resource=resource,
        )
        refresh = RefreshToken(
            token=f"gmr_{secrets.token_urlsafe(32)}",
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL),
        )
        self.access_tokens[access.token] = access
        self.refresh_tokens[refresh.token] = refresh
        self._expire_tokens()
        self._save()
        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh.token,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: a replayed code must not mint a second token.
        if self.auth_codes.pop(authorization_code.code, None) is None:
            raise TokenError(
                error="invalid_grant",
                error_description="authorization code has already been used",
            )
        return self._issue(
            authorization_code.client_id,
            list(authorization_code.scopes),
            authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if token and token.expires_at and token.expires_at < time.time():
            self.refresh_tokens.pop(refresh_token, None)
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the old refresh token dies with this exchange.
        self.refresh_tokens.pop(refresh_token.token, None)
        for token, access in list(self.access_tokens.items()):
            if access.client_id == refresh_token.client_id:
                self.access_tokens.pop(token, None)
        return self._issue(refresh_token.client_id, scopes or list(refresh_token.scopes), None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < time.time():
            self.access_tokens.pop(token, None)
            self._save()
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)
        self._save()


# -- login page ------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Garmin MCP</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100vh; margin: 0; padding: 1rem; }}
  form {{ width: min(24rem, 100%); display: grid; gap: 0.75rem; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 0.25rem; }}
  p {{ margin: 0; font-size: 0.85rem; opacity: 0.75; }}
  input, button {{ font: inherit; padding: 0.6rem 0.7rem; border-radius: 0.5rem;
                   border: 1px solid rgba(128,128,128,0.5); }}
  button {{ cursor: pointer; font-weight: 600; }}
  .error {{ color: #c0392b; font-size: 0.85rem; }}
</style></head>
<body><form method="post" action="/login">
  <h1>Connect Garmin MCP</h1>
  <p>Enter the connector passphrase to give Claude access to this Garmin account.</p>
  <input type="hidden" name="session" value="{session}">
  <input type="password" name="passphrase" placeholder="Connector passphrase"
         autocomplete="current-password" autofocus required>
  <button type="submit">Sign in</button>
  {error}
</form></body></html>
"""


def render_login_page(session_id: str, error: str = "") -> str:
    from html import escape

    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    return _PAGE.format(session=escape(session_id), error=error_html)


def register_login_route(mcp: Any, provider: SingleUserOAuthProvider) -> None:
    """Attach the GET/POST /login routes used by the authorize step."""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse

    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):  # pragma: no cover - exercised end to end
        if request.method == "GET":
            session_id = request.query_params.get("session", "")
            return HTMLResponse(render_login_page(session_id))

        form = await request.form()
        session_id = str(form.get("session", ""))
        passphrase = str(form.get("passphrase", ""))

        if provider.locked_out:
            return HTMLResponse(
                render_login_page(
                    session_id,
                    f"Too many attempts. Try again in {provider.lockout_remaining} seconds.",
                ),
                status_code=429,
            )

        if not provider.check_passphrase(passphrase):
            return HTMLResponse(
                render_login_page(session_id, "Incorrect passphrase."), status_code=401
            )

        try:
            redirect_to = provider.complete_login(session_id)
        except AuthorizeError as exc:
            return HTMLResponse(
                render_login_page("", exc.error_description or "Sign-in failed."),
                status_code=400,
            )
        return RedirectResponse(redirect_to, status_code=302)
