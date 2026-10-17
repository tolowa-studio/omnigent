"""Standalone artifact retention entrypoint for Gate A real-task factory runs."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dev.factory.gate_a_real.artifact_retention import (
    prune_artifacts_root,
    resolve_artifacts_root,
    retention_config_from_environ,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune old Gate A real-task factory run trees under a pinned artifacts root.",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        help=(
            "Artifacts root to prune (default: from "
            "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT or inferred factory-* parent)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log deletions without removing directories.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.artifacts_root is not None:
        root = args.artifacts_root.expanduser().resolve()
    else:
        resolved = resolve_artifacts_root()
        if resolved is None:
            print(
                "artifacts root not configured; set "
                "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT or pass --artifacts-root",
                file=sys.stderr,
            )
            return 2
        root = resolved

    if not root.is_dir():
        print(f"artifacts root is not a directory: {root}", file=sys.stderr)
        return 2

    result = prune_artifacts_root(
        root,
        config=retention_config_from_environ(),
        dry_run=args.dry_run,
    )
    print(
        f"prune complete kept={len(result.kept)} "
        f"deleted={len(result.deleted)} skipped={len(result.skipped)} root={root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
