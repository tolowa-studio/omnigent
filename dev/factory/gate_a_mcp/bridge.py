"""Deterministic Gate A MCP bridge (fixture admit → bound worker execute)."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev.factory.gate_a_mcp.checkout import (
    archive_artifact_evidence,
    create_disposable_worktree,
    dispose_worktree,
)
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME
from dev.factory.gate_a_mcp.preflight import GateAPreflightError, consume_preflight_receipt
from dev.factory.gate_a_mcp.process_witness import stamp_gate_a_mcp_success_payload
from dev.factory.order_scoped.adapter import (
    OrderScopedWorkerAdapter,
    OrderScopedWorkerError,
    WorkerRunResult,
    decode_worker_child_payload,
)
from dev.factory.order_scoped.binding import (
    INTERNAL_STAGE_ORDER_ID,
    STAGE_WORKER_ENV,
    StageWorkOrderBinding,
)
from dev.factory.order_scoped.probe_trust import fingerprint_probe_program
from dev.factory.seatbelt_fixture.manifest import (
    GATE_A_MIN_SETTLE_SECONDS,
    FixtureReceipt,
    child_script_path,
)


class GateAMcpBridgeError(RuntimeError):
    """Gate A MCP bridge refused or failed closed."""


class GateAMcpArgumentError(GateAMcpBridgeError):
    """Caller supplied disallowed MCP tool arguments."""


FORBIDDEN_ARGUMENT_KEYS: frozenset[str] = frozenset(
    {
        "command",
        "argv",
        "cwd",
        "worktree",
        "path",
        "order_id",
        "orderId",
        "receipt",
        "gate_receipt",
        "environment",
        "env",
        "allow_network",
        "network",
        "probe_sha256",
        "trusted_probe_sha256",
    }
)


def validate_tool_arguments(arguments: Mapping[str, object] | None) -> None:
    """Reject any caller-controlled execution surface (fail closed)."""
    if arguments is None:
        return
    if not isinstance(arguments, Mapping):
        raise GateAMcpArgumentError("tool arguments must be an object when present")
    if not arguments:
        return
    for key in arguments:
        if not isinstance(key, str):
            raise GateAMcpArgumentError("tool argument keys must be strings")
        normalized = key.strip()
        if not normalized:
            raise GateAMcpArgumentError("empty argument key")
        if normalized in FORBIDDEN_ARGUMENT_KEYS:
            raise GateAMcpArgumentError(f"forbidden argument: {normalized}")
        raise GateAMcpArgumentError(f"unexpected argument: {normalized}")


FixtureRunner = Callable[..., FixtureReceipt]


@dataclass(frozen=True)
class GateAMcpBridgeConfig:
    python_executable: Path
    settle_seconds: float = GATE_A_MIN_SETTLE_SECONDS
    fixture_order_id: str = INTERNAL_STAGE_ORDER_ID
    worker_environ: Mapping[str, str] | None = None


def _worker_environ(base: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env[STAGE_WORKER_ENV] = "1"
    return env


def run_bound_internal_stage_order(
    arguments: Mapping[str, object] | None = None,
    *,
    config: GateAMcpBridgeConfig | None = None,
    fixture_runner: FixtureRunner | None = None,
    worktree_factory: Callable[[], Path] | None = None,
) -> dict[str, Any]:
    """
    Admit a single-use qualified Gate A receipt, then execute the closed positive binding.

    The 90s Seatbelt fixture runs at MCP server startup (``preflight``), not inside this
    CallTool handler, so Cursor's MCP deadline can be met while the worker still admits
    a trusted runner-minted receipt.
    """
    validate_tool_arguments(arguments)

    if os.environ.get("GATE_A_MCP_FIXTURE_SENTINEL"):
        raise GateAMcpBridgeError(
            "GATE_A_MCP_FIXTURE_SENTINEL set — seatbelt fixture must not run",
        )

    cfg = config or GateAMcpBridgeConfig(python_executable=Path(sys.executable))
    make_worktree = worktree_factory or create_disposable_worktree

    if fixture_runner is not None:
        receipt = fixture_runner(
            settle_seconds=cfg.settle_seconds,
            order_id=cfg.fixture_order_id,
        )
        if not receipt.qualified_for_gate_a:
            reason = receipt.failure_reason or "fixture not qualified_for_gate_a"
            raise GateAMcpBridgeError(f"seatbelt fixture gate failed: {reason}")
    else:
        try:
            receipt = consume_preflight_receipt(cfg.fixture_order_id)
        except GateAPreflightError as exc:
            raise GateAMcpBridgeError(str(exc)) from exc

    trusted_probe = child_script_path()
    probe_pin = fingerprint_probe_program(trusted_probe)

    checkout = make_worktree()
    try:
        binding = StageWorkOrderBinding.internal_stage_default(
            worktree=checkout,
            python_executable=cfg.python_executable,
            gate_receipt_order_id=cfg.fixture_order_id,
        )
        if binding.argv[-1] != "positive":
            raise GateAMcpBridgeError("internal binding argv must end with positive")
        adapter = OrderScopedWorkerAdapter(
            binding,
            gate_receipt=receipt,
            trusted_probe_sha256=probe_pin,
            environ=_worker_environ(cfg.worker_environ),
        )
        try:
            admitted = adapter.admit()
            result: WorkerRunResult = adapter.execute()
        except OrderScopedWorkerError as exc:
            raise GateAMcpBridgeError(str(exc)) from exc

        payload = decode_worker_child_payload(result)
        artifact = checkout / BOUND_ARTIFACT_FILENAME
        if not artifact.is_file():
            raise GateAMcpBridgeError(f"expected artifact missing: {BOUND_ARTIFACT_FILENAME}")

        artifact_bytes = artifact.read_bytes()
        evidence_path = archive_artifact_evidence(artifact)
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

        success: dict[str, Any] = {
            "ok": True,
            "order_id": binding.order_id,
            "artifact": BOUND_ARTIFACT_FILENAME,
            "artifact_path": str(evidence_path),
            "artifact_sha256": artifact_sha256,
            "probe_sha256": result.probe_sha256,
            "gate_qualified": admitted.qualified_for_gate_a,
            "manifest_hash": admitted.manifest_hash,
            "command_hash": admitted.command_hash,
            "child_ok": payload.get("ok"),
            "settle_observed_seconds": receipt.settle_observed_seconds,
        }
        admitted_brief = os.environ.get("GATE_A_MCP_ADMITTED_BRIEF_HASH", "").strip()
        if admitted_brief:
            success["brief_hash"] = admitted_brief
        return stamp_gate_a_mcp_success_payload(success)
    finally:
        dispose_worktree(checkout)
