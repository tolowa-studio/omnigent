"""Immutable work-order binding and stage feature gate (off by default)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS, child_script_path

STAGE_WORKER_ENV = "OMNIGENT_FACTORY_ORDER_SCOPED_WORKER_ENABLED"

# Single internal stage work order for v0.17.0 factory integration.
INTERNAL_STAGE_ORDER_ID = "omnigent-factory-stage-work-order-v0.17.0"

# Fixed synthetic brief for offline adapter staging (not arbitrary production orders).
INTERNAL_STAGE_SYNTHETIC_BRIEF = (
    "omnigent-factory-gate-a-offline-synthetic-v0.17.0:execute_internal_stage_order"
)
INTERNAL_STAGE_BRIEF_HASH = hashlib.sha256(
    INTERNAL_STAGE_SYNTHETIC_BRIEF.encode("utf-8"),
).hexdigest()

DEFAULT_SANDBOX_BACKEND = "darwin_seatbelt"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_STDOUT_BYTES = 256_000
DEFAULT_MAX_STDERR_BYTES = 256_000

# Launcher env keys the Seatbelt executor may set (explicit allowlist contract).
DEFAULT_LAUNCHER_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "LC_ALL",
        "LANG",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TEMPDIR",
        "XDG_RUNTIME_DIR",
    }
)


def stage_worker_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return True only when the stage worker is explicitly enabled."""
    env = environ if environ is not None else os.environ
    return env.get(STAGE_WORKER_ENV, "").strip() == "1"


@dataclass(frozen=True)
class StageWorkOrderBinding:
    """Closed binding for one admitted work order."""

    order_id: str
    argv: tuple[str, ...]
    worktree: Path
    env_allowlist: frozenset[str]
    allow_network: bool
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    sandbox_backend: str
    trusted_probe_script: Path
    gate_receipt_order_id: str

    @staticmethod
    def internal_stage_default(
        *,
        worktree: Path,
        python_executable: Path,
        gate_receipt_order_id: str = INTERNAL_STAGE_ORDER_ID,
    ) -> StageWorkOrderBinding:
        """Default binding: positive control only, trusted probe outside the worktree."""
        trusted = child_script_path().resolve(strict=False)
        argv = (str(python_executable.resolve(strict=False)), str(trusted), "positive")
        return StageWorkOrderBinding(
            order_id=INTERNAL_STAGE_ORDER_ID,
            argv=argv,
            worktree=worktree,
            env_allowlist=DEFAULT_LAUNCHER_ENV_ALLOWLIST,
            allow_network=False,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            max_stdout_bytes=DEFAULT_MAX_STDOUT_BYTES,
            max_stderr_bytes=DEFAULT_MAX_STDERR_BYTES,
            sandbox_backend=DEFAULT_SANDBOX_BACKEND,
            trusted_probe_script=trusted,
            gate_receipt_order_id=gate_receipt_order_id,
        )


def minimum_gate_settle_seconds() -> float:
    return GATE_A_MIN_SETTLE_SECONDS
