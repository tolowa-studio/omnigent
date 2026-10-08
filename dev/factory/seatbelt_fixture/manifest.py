"""Order-scoped execution manifest and receipt types."""

from __future__ import annotations

import errno
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_VERBS: frozenset[str] = frozenset(
    {
        "positive",
        "probe_outside_write",
        "probe_outside_read",
        "probe_local_connect",
        "probe_env_secret",
        "probe_home_read",
        "probe_home_symlink",
    }
)

CANARY_FILE_TOKEN = "SYNTHETIC_FIXTURE_CANARY_CONTENT"
HOME_PROBE_DIR_PREFIX = ".omnigent-seatbelt-probe-"
HOME_SENTINEL_FILENAME = "sentinel.txt"
# Synthetic-only token; never include in receipts or child output hygiene checks use this constant.
HOME_SENTINEL_TOKEN = "OMNIGENT_SEATBELT_PROBE_HOME_SYNTHETIC_TOKEN"
ENV_PROBE_NAME = "FIXTURE_PROBE_ENV_SECRET"
ENV_PROBE_TOKEN = "SYNTHETIC_FIXTURE_ENV_PROBE_VALUE"

# macOS Seatbelt file-access denials surface as EPERM (verified on darwin_seatbelt).
SEATBELT_FILE_DENIAL_ERRNOS: frozenset[int] = frozenset({errno.EPERM})
# Local TCP connect denials under allow_network=False (measured on darwin_seatbelt).
SEATBELT_CONNECT_DENIAL_ERRNOS: frozenset[int] = frozenset({errno.EPERM})

# Gate A observation window; shorter runs may pass probes but are not qualified.
GATE_A_MIN_SETTLE_SECONDS = 90.0


@dataclass(frozen=True)
class OrderManifest:
    """Closed manifest for one sandboxed child invocation."""

    order_id: str
    verb: str
    argv: tuple[str, ...]
    cwd: str

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "verb": self.verb,
            "argv": list(self.argv),
            "cwd": self.cwd,
        }

    def manifest_hash(self) -> str:
        payload = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def command_hash(self) -> str:
        joined = "\0".join(self.argv)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class ProbeRecord:
    name: str
    passed: bool
    detail: str

    def to_jsonable(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class FixtureReceipt:
    passed: bool
    failure_reason: str | None
    order_id: str
    manifest_hash: str
    command_hash: str
    platform: str
    machine: str
    sandbox_backend: str
    probes: list[ProbeRecord] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""
    settle_seconds: float = 90.0
    settle_observed_seconds: float = 0.0
    qualified_for_gate_a: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "qualified_for_gate_a": self.qualified_for_gate_a,
            "failure_reason": self.failure_reason,
            "order_id": self.order_id,
            "manifest_hash": self.manifest_hash,
            "command_hash": self.command_hash,
            "platform": self.platform,
            "machine": self.machine,
            "sandbox_backend": self.sandbox_backend,
            "probes": [p.to_jsonable() for p in self.probes],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "settle_seconds": self.settle_seconds,
            "settle_observed_seconds": self.settle_observed_seconds,
        }

    def emit_json_line(self) -> str:
        return json.dumps(self.to_jsonable(), sort_keys=True, separators=(",", ":"))


def child_script_path() -> Path:
    return Path(__file__).resolve().parent / "child_main.py"
