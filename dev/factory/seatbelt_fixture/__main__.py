"""CLI entry: ``python -m dev.factory.seatbelt_fixture``."""

from __future__ import annotations

import argparse
import os
import sys

from .runner import run_seatbelt_fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="macOS Seatbelt factory gate fixture")
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=None,
        help="Post-exit observation window (default 90, or FIXTURE_SETTLE_SECONDS)",
    )
    parser.add_argument("--order-id", default="factory-seatbelt-fixture-order")
    args = parser.parse_args(argv)

    receipt = run_seatbelt_fixture(
        settle_seconds=args.settle_seconds,
        order_id=args.order_id,
    )
    sys.stdout.write(receipt.emit_json_line() + "\n")
    sys.stdout.flush()
    return 0 if receipt.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
