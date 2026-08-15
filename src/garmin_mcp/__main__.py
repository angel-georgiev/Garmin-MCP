"""Allow `python -m garmin_mcp` to start the server."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
