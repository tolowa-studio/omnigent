"""Gate A Seatbelt receipt admission (requires qualified_for_gate_a, not exit 0 alone)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev.factory.seatbelt_fixture.manifest import (
    GATE_A_MIN_SETTLE_SECONDS,
    FixtureReceipt,
    ProbeRecord,
)
from dev.factory.seatbelt_fixture.runner import _qualified_for_gate_a
from dev.factory.seatbelt_fixture.trusted_admission import (
    TrustedGateAdmissionEvidence,
    gate_admission_evidence_for_receipt,
    receipt_admission_trusted,
)

# Exact probe name set emitted by ``run_seatbelt_fixture`` on success (no optional hygiene row).
GATE_A_EXACT_PROBE_NAMES: frozenset[str] = frozenset(
    {
        "positive",
        "probe_outside_write",
        "probe_outside_read",
        "probe_home_read",
        "probe_home_symlink",
        "probe_local_connect",
        "probe_env_secret",
        "outside_canary_unchanged",
        "home_sentinel_unchanged",
        "listener_no_connections",
    }
)


class ReceiptAdmissionError(ValueError):
    """Receipt cannot admit stage worker execution."""


@dataclass(frozen=True)
class AdmittedGateReceipt:
    order_id: str
    sandbox_backend: str
    qualified_for_gate_a: bool
    manifest_hash: str
    command_hash: str
    settle_observed_seconds: float


def _parse_probe_rows(raw: object) -> list[ProbeRecord]:
    if not isinstance(raw, list):
        raise ReceiptAdmissionError("probes must be a list")
    out: list[ProbeRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ReceiptAdmissionError("probe row must be an object")
        name = item.get("name")
        passed = item.get("passed")
        detail = item.get("detail", "")
        if not isinstance(name, str) or not isinstance(passed, bool):
            raise ReceiptAdmissionError("probe row missing name or passed")
        out.append(ProbeRecord(name, passed, str(detail)))
    return out


def receipt_from_mapping(data: dict[str, Any]) -> FixtureReceipt:
    return FixtureReceipt(
        passed=bool(data.get("passed")),
        qualified_for_gate_a=bool(data.get("qualified_for_gate_a")),
        failure_reason=data.get("failure_reason"),
        order_id=str(data.get("order_id", "")),
        manifest_hash=str(data.get("manifest_hash", "")),
        command_hash=str(data.get("command_hash", "")),
        platform=str(data.get("platform", "")),
        machine=str(data.get("machine", "")),
        sandbox_backend=str(data.get("sandbox_backend", "")),
        probes=_parse_probe_rows(data.get("probes", [])),
        started_at=str(data.get("started_at", "")),
        ended_at=str(data.get("ended_at", "")),
        settle_seconds=float(data.get("settle_seconds", GATE_A_MIN_SETTLE_SECONDS)),
        settle_observed_seconds=float(data.get("settle_observed_seconds", 0.0)),
    )


def assert_receipt_path_outside_worktree(receipt_path: Path, *, worktree: Path) -> None:
    """Reject gate receipt files stored inside the bound worktree (forgable by order code)."""
    worktree_real = Path(os.path.realpath(worktree))
    receipt_real = Path(os.path.realpath(receipt_path))
    try:
        receipt_real.relative_to(worktree_real)
    except ValueError:
        return
    raise ReceiptAdmissionError(f"gate receipt path must not live inside worktree: {receipt_path}")


def load_receipt_json(path: Path) -> FixtureReceipt:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptAdmissionError(f"cannot read receipt: {exc}") from exc
    if not isinstance(data, dict):
        raise ReceiptAdmissionError("receipt must be a JSON object")
    return receipt_from_mapping(data)


def admit_gate_receipt(
    receipt: FixtureReceipt,
    *,
    admission_evidence: TrustedGateAdmissionEvidence | None = None,
    expected_order_id: str,
    expected_sandbox_backend: str,
    cleanup_succeeded: bool = True,
) -> AdmittedGateReceipt:
    """
    Fail closed unless the receipt is a full qualified Gate A darwin_seatbelt run.

    ``passed`` alone is insufficient; ``qualified_for_gate_a`` must be true.
    """
    if admission_evidence is None:
        admission_evidence = gate_admission_evidence_for_receipt(receipt)
    if not receipt_admission_trusted(receipt, admission_evidence):
        raise ReceiptAdmissionError("gate receipt not minted by trusted fixture runner")
    if receipt.sandbox_backend != expected_sandbox_backend:
        raise ReceiptAdmissionError(
            "sandbox backend mismatch: "
            f"{receipt.sandbox_backend!r} != {expected_sandbox_backend!r}"
        )
    if receipt.order_id != expected_order_id:
        raise ReceiptAdmissionError(
            f"order_id mismatch: {receipt.order_id!r} != {expected_order_id!r}"
        )
    probe_names = {p.name for p in receipt.probes}
    if probe_names != GATE_A_EXACT_PROBE_NAMES:
        missing = GATE_A_EXACT_PROBE_NAMES - probe_names
        extra = probe_names - GATE_A_EXACT_PROBE_NAMES
        raise ReceiptAdmissionError(
            f"probe set mismatch: missing={sorted(missing)} extra={sorted(extra)}"
        )
    if not all(p.passed for p in receipt.probes):
        failed = [p.name for p in receipt.probes if not p.passed]
        raise ReceiptAdmissionError(f"probe failures in receipt: {failed}")

    if receipt.settle_observed_seconds < GATE_A_MIN_SETTLE_SECONDS:
        raise ReceiptAdmissionError(
            "settle window too short: "
            f"{receipt.settle_observed_seconds} < {GATE_A_MIN_SETTLE_SECONDS}"
        )

    if not receipt.qualified_for_gate_a:
        raise ReceiptAdmissionError("qualified_for_gate_a is false")
    if not _qualified_for_gate_a(receipt, cleanup_succeeded=cleanup_succeeded):
        raise ReceiptAdmissionError("receipt failed qualified_for_gate_a rules")

    return AdmittedGateReceipt(
        order_id=receipt.order_id,
        sandbox_backend=receipt.sandbox_backend,
        qualified_for_gate_a=True,
        manifest_hash=receipt.manifest_hash,
        command_hash=receipt.command_hash,
        settle_observed_seconds=receipt.settle_observed_seconds,
    )
