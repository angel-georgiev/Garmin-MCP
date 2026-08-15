"""Command line entry point: `garmin-mcp serve | login | status | logout`."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from garminconnect import Garmin, GarminConnectAuthenticationError

from . import __version__
from .config import Settings


def _prompt_mfa() -> str:
    return input("Garmin MFA code: ").strip()


def cmd_login(args: argparse.Namespace) -> int:
    """Interactive login that caches OAuth tokens for the server to reuse."""
    settings = Settings.from_env()
    email = args.email or settings.email or input("Garmin Connect email: ").strip()
    password = settings.password or getpass.getpass("Garmin Connect password: ")
    tokenstore = Path(args.tokenstore).expanduser() if args.tokenstore else settings.tokenstore
    tokenstore.mkdir(parents=True, exist_ok=True)

    client = Garmin(
        email=email,
        password=password,
        is_cn=settings.is_cn,
        prompt_mfa=_prompt_mfa,
    )
    try:
        client.login(str(tokenstore))
    except GarminConnectAuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface whatever Garmin threw
        print(f"Login failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Signed in as {client.full_name or email}.")
    print(f"Tokens cached in {tokenstore}.")
    print("You can now remove GARMIN_PASSWORD from the environment if you prefer.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Report whether the cached session still works."""
    settings = Settings.from_env()
    tokens = "present" if settings.has_cached_tokens else "empty"
    print(f"Token store:  {settings.tokenstore} ({tokens})")
    print(f"Credentials:  {'set' if settings.has_credentials else 'not set'} in environment")
    print(f"Domain:       {'garmin.cn' if settings.is_cn else 'garmin.com'}")

    client = Garmin(
        email=settings.email,
        password=settings.password,
        is_cn=settings.is_cn,
    )
    try:
        client.login(str(settings.tokenstore))
    except Exception as exc:  # noqa: BLE001
        print(f"Not authenticated: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Run `garmin-mcp login` to sign in.", file=sys.stderr)
        return 1

    print(f"Authenticated as {client.full_name} ({client.display_name})")
    print(f"Units:        {client.unit_system}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Delete cached tokens."""
    settings = Settings.from_env()
    store = settings.tokenstore
    removed = 0
    if store.is_dir():
        for path in store.glob("*token*"):
            path.unlink()
            removed += 1
    print(f"Removed {removed} token file(s) from {store}.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the MCP server (stdio by default)."""
    settings = Settings.from_env()

    # Half-configured OAuth is the dangerous case: the operator believes the
    # server is protected while it is serving health data to anyone. Refuse.
    if settings.auth_passphrase and not settings.public_url:
        print(
            "GARMIN_MCP_AUTH_PASSPHRASE is set but GARMIN_MCP_PUBLIC_URL is not. OAuth "
            "needs the public HTTPS URL clients will reach (it becomes the OAuth issuer). "
            "Set it to your tunnel URL, e.g. https://example.trycloudflare.com",
            file=sys.stderr,
        )
        return 2
    if settings.public_url and not settings.auth_passphrase:
        print(
            "GARMIN_MCP_PUBLIC_URL is set but GARMIN_MCP_AUTH_PASSPHRASE is not, so the "
            "server would be publicly readable without a sign-in. Set a long, unique "
            "passphrase (not your Garmin password) to enable OAuth.",
            file=sys.stderr,
        )
        return 2
    # Plain http is allowed on loopback only — that's for testing the flow
    # locally, the same exemption OAuth makes for native apps.
    loopback = settings.public_url and settings.public_url.startswith(
        ("http://127.0.0.1", "http://localhost", "http://[::1]")
    )
    if settings.oauth_enabled and not settings.public_url.startswith("https://") and not loopback:
        print(
            f"GARMIN_MCP_PUBLIC_URL must be https:// for a connector (got {settings.public_url}). "
            "Only loopback addresses may use http, for local testing.",
            file=sys.stderr,
        )
        return 2

    if args.transport != "stdio":
        if settings.oauth_enabled:
            print(f"OAuth enabled; issuer {settings.public_url}", file=sys.stderr)
        elif args.host not in {"127.0.0.1", "localhost", "::1"}:
            print(
                f"Warning: binding to {args.host} exposes every health metric in this Garmin "
                "account to anyone who can reach the port, with no authentication. Set "
                "GARMIN_MCP_AUTH_PASSPHRASE and GARMIN_MCP_PUBLIC_URL to require a sign-in.",
                file=sys.stderr,
            )

    from .server import run

    run(transport=args.transport, host=args.host, port=args.port, path=args.path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp",
        description="MCP server for Garmin Connect data.",
    )
    parser.add_argument("--version", action="version", version=f"garmin-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport to expose (default: stdio)",
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address for the HTTP transports (default: 127.0.0.1)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8000,
        help="port for the HTTP transports (default: 8000)",
    )
    serve.add_argument(
        "--path",
        default="/mcp",
        help=(
            "URL path for streamable-http (default: /mcp). Use an unguessable path when "
            "exposing the server through a public tunnel"
        ),
    )
    serve.set_defaults(func=cmd_serve)

    login = sub.add_parser("login", help="sign in interactively and cache tokens")
    login.add_argument("--email", help="Garmin Connect email (otherwise prompted)")
    login.add_argument("--tokenstore", help="directory for cached tokens")
    login.set_defaults(func=cmd_login)

    status = sub.add_parser("status", help="check the cached session")
    status.set_defaults(func=cmd_status)

    logout = sub.add_parser("logout", help="delete cached tokens")
    logout.set_defaults(func=cmd_logout)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare `garmin-mcp` starts the server, which is what MCP clients invoke.
        args = parser.parse_args([*argv, "serve"])
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
