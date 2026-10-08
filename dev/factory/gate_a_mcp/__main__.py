"""CLI: ``python -m dev.factory.gate_a_mcp`` (stdio or prestarted HTTP)."""

from __future__ import annotations

import argparse
import sys

from .preflight import start_startup_seatbelt_preflight_background
from .server import run_stdio_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="dev.factory.gate_a_mcp")
    parser.add_argument(
        "command",
        nargs="?",
        default="stdio",
        choices=("stdio", "serve-http"),
        help="stdio (default Cursor child) or serve-http (trial prestart)",
    )
    args = parser.parse_args(argv)
    if args.command == "serve-http":
        from .http_serve import run_prestarted_http_server

        run_prestarted_http_server()
        return
    start_startup_seatbelt_preflight_background()
    run_stdio_server()


if __name__ == "__main__":
    main(sys.argv[1:])
