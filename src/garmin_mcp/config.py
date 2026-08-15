"""Configuration for the Garmin MCP server, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TOKENSTORE = "~/.garminconnect"
DEFAULT_MAX_CHARS = 40_000
DEFAULT_DOWNLOAD_DIR = "~/garmin-downloads"


def load_dotenv_files() -> None:
    """Load .env from the working directory and ~/.garmin-mcp.env, if present.

    Existing environment variables always win, so an explicit env var in an MCP
    client config is never overridden by a stale file on disk.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a hard dependency
        return

    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(Path.home() / ".garmin-mcp.env", override=False)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from environment variables."""

    email: str | None
    password: str | None
    tokenstore: Path
    is_cn: bool
    max_response_chars: int
    download_dir: Path

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv_files()
        email = os.getenv("GARMIN_EMAIL") or os.getenv("GARMIN_USERNAME")
        password = os.getenv("GARMIN_PASSWORD")
        tokenstore = Path(os.getenv("GARMINTOKENS") or DEFAULT_TOKENSTORE).expanduser()
        download_dir = Path(
            os.getenv("GARMIN_MCP_DOWNLOAD_DIR") or DEFAULT_DOWNLOAD_DIR
        ).expanduser()
        return cls(
            email=email.strip() if email else None,
            password=password if password else None,
            tokenstore=tokenstore,
            is_cn=_env_flag("GARMIN_IS_CN"),
            max_response_chars=_env_int("GARMIN_MCP_MAX_CHARS", DEFAULT_MAX_CHARS),
            download_dir=download_dir,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.password)

    @property
    def has_cached_tokens(self) -> bool:
        """True when the token store looks like it holds a usable session."""
        store = self.tokenstore
        if not store.is_dir():
            return False
        return any(store.glob("*token*"))
