"""CLI entry: ``python -m dev.factory.gate_a_real``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dev.factory.gate_a_real.orchestration import (
    RealTaskGateError,
    RealTaskRunOptions,
    run_real_task_gate,
)
from dev.factory.gate_a_real.spec import RealTaskSpecError, load_real_task_spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local opt-in Gate A real-operator task runner.",
    )
    parser.add_argument(
        "--spec",
        required=True,
        type=Path,
        help="Path to the bound JSON task spec (workspace, expiry, spec_sha256).",
    )
    parser.add_argument(
        "--artifacts-dir",
        required=True,
        type=Path,
        help="Directory for transcripts, freeze candidate, and receipt.json.",
    )
    parser.add_argument(
        "--review-only",
        action="store_true",
        help="Skip builder when resume_state.json records a successful builder run.",
    )
    args = parser.parse_args(argv)

    try:
        spec = load_real_task_spec(args.spec.resolve())
    except RealTaskSpecError as exc:
        print(f"spec rejected: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_real_task_gate(
            spec,
            RealTaskRunOptions(
                artifacts_dir=args.artifacts_dir.resolve(),
                review_only=args.review_only,
            ),
        )
    except RealTaskGateError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    receipt_path = args.artifacts_dir.resolve() / "receipt.json"
    if result.receipt.ok:
        print(f"gate_a_real ok receipt={receipt_path}")
        return 0
    for problem in result.problems:
        print(problem, file=sys.stderr)
    print(f"gate_a_real failed receipt={receipt_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
