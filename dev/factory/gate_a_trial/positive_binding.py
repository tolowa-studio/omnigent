"""Process-bound positive MCP receipt checks for the Cursor CLI harness.

Seatbelt preflight (including the >=90s settle) runs at MCP server startup before any
``execute_internal_stage_order`` tool call. The MCP payload must report the same
``settle_observed_seconds`` as the live process witness (and optional HOME marker).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from dev.factory.gate_a_mcp.checkout import (
    artifact_path_under_canonical_evidence_root,
    is_real_evidence_archive_dir,
    is_regular_bound_artifact_file,
    list_evidence_archive_dir_names,
)
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME
from dev.factory.gate_a_mcp.process_witness import (
    ProcessWitnessError,
    QualifiedProcessWitness,
    validate_live_witness,
)
from dev.factory.gate_a_trial.prestarted_mcp import PrestartedGateAMcp
from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS

# Payload settle is stamped from the startup fixture receipt; allow float noise only.
SETTLE_PAYLOAD_WITNESS_TOLERANCE_SECONDS = 1e-3


def sha256_hex_of_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _preflight_marker_settle_seconds(
    mcp_server_home: Path,
    *,
    expected_server_pid: int,
) -> float | None:
    from dev.factory.gate_a_mcp.preflight import preflight_ready_marker_path

    marker = preflight_ready_marker_path(mcp_server_home)
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        import json

        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("qualified_for_gate_a") is not True:
        return None
    if data.get("server_pid") != expected_server_pid:
        return None
    settle = data.get("settle_observed_seconds")
    if isinstance(settle, bool) or not isinstance(settle, (int, float)):
        return None
    return float(settle)


def verify_positive_mcp_receipt(
    payload: dict[str, Any],
    *,
    artifact_path: Path,
    prestarted: PrestartedGateAMcp | None,
    witness: QualifiedProcessWitness | None,
    evidence_dirs_before: set[str],
    evidence_root_parent: Path | None = None,
    mcp_server_home: Path | None = None,
) -> tuple[bool, list[str]]:
    """Bind payload bytes, witness identity, and one new evidence archive dir."""
    problems: list[str] = []

    if payload.get("ok") is not True:
        problems.append("payload ok must be true")
    if payload.get("gate_qualified") is not True:
        problems.append("gate_qualified must be true")
    if payload.get("child_ok") is not True:
        problems.append("child_ok must be true")

    settle = payload.get("settle_observed_seconds")
    if isinstance(settle, bool) or not isinstance(settle, (int, float)):
        problems.append("settle_observed_seconds must be a number")
    elif float(settle) < GATE_A_MIN_SETTLE_SECONDS:
        problems.append(f"settle_observed_seconds must be >= {GATE_A_MIN_SETTLE_SECONDS}")

    artifact_name = payload.get("artifact")
    if artifact_name != BOUND_ARTIFACT_FILENAME:
        problems.append(f"artifact must be {BOUND_ARTIFACT_FILENAME!r}")

    expected_sha = payload.get("artifact_sha256")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        problems.append("artifact_sha256 must be a 64-char hex digest")
    else:
        regular, reg_reason = is_regular_bound_artifact_file(artifact_path)
        if not regular:
            problems.append(f"artifact_path not a regular file: {reg_reason}")
        else:
            actual_sha = sha256_hex_of_file(artifact_path)
            if actual_sha != expected_sha:
                problems.append("artifact_sha256 does not match allowed.txt bytes on disk")

    under, reason, _ = artifact_path_under_canonical_evidence_root(
        artifact_path,
        parent=evidence_root_parent,
    )
    if not under:
        problems.append(f"artifact_path outside evidence root: {reason}")

    archive_dir = artifact_path.parent
    archive_parent = archive_dir.name
    archive_ok, archive_reason = is_real_evidence_archive_dir(archive_dir)
    if not archive_ok:
        problems.append(f"evidence archive directory invalid: {archive_reason}")
    elif not archive_parent.startswith("evidence-"):
        problems.append("artifact_path parent must be an evidence-* archive directory")
    elif archive_parent in evidence_dirs_before:
        problems.append("artifact_path reuses a pre-trial evidence archive dir")
    elif archive_parent not in list_evidence_archive_dir_names(evidence_root_parent):
        problems.append("artifact_path parent is not under the canonical evidence root")

    payload_nonce = payload.get("witness_nonce")
    payload_pid = payload.get("server_pid")

    if prestarted is None:
        problems.append("missing prestarted MCP handle for positive binding")
    else:
        if not isinstance(payload_nonce, str) or not payload_nonce:
            problems.append("witness_nonce must be present in MCP payload")
        elif payload_nonce != prestarted.witness_nonce:
            problems.append("payload witness_nonce does not match prestarted handle")
        if isinstance(payload_pid, bool) or not isinstance(payload_pid, int):
            problems.append("server_pid must be an integer in MCP payload")
        elif payload_pid != prestarted.server_pid:
            problems.append("payload server_pid does not match prestarted handle")

    if prestarted is not None and witness is None:
        problems.append("witness record missing despite prestarted MCP handle")
    elif witness is not None:
        try:
            validate_live_witness(
                witness,
                expected_nonce=prestarted.witness_nonce if prestarted else None,
                expected_pid=prestarted.server_pid if prestarted else None,
            )
            witness_settle = float(witness.settle_observed_seconds)
            if witness_settle < GATE_A_MIN_SETTLE_SECONDS:
                problems.append(
                    f"witness settle_observed_seconds below minimum {GATE_A_MIN_SETTLE_SECONDS}"
                )
            if isinstance(settle, (int, float)):
                payload_settle = float(settle)
                if abs(payload_settle - witness_settle) > SETTLE_PAYLOAD_WITNESS_TOLERANCE_SECONDS:
                    problems.append(
                        "settle_observed_seconds does not match live witness "
                        "(preflight runs before MCP tool call)"
                    )
            order_id = payload.get("order_id")
            if order_id != witness.order_id:
                problems.append("payload order_id does not match live witness order_id")
            if isinstance(payload_nonce, str) and payload_nonce != witness.witness_nonce:
                problems.append("payload witness_nonce does not match live witness")
            if isinstance(payload_pid, int) and payload_pid != witness.server_pid:
                problems.append("payload server_pid does not match live witness")
            if mcp_server_home is not None and prestarted is not None:
                marker_settle = _preflight_marker_settle_seconds(
                    mcp_server_home,
                    expected_server_pid=prestarted.server_pid,
                )
                if marker_settle is None:
                    problems.append(
                        "MCP server HOME preflight marker missing or not bound to server pid"
                    )
                elif (
                    abs(marker_settle - witness_settle) > SETTLE_PAYLOAD_WITNESS_TOLERANCE_SECONDS
                ):
                    problems.append(
                        "preflight HOME marker settle_observed_seconds does not match witness"
                    )
        except ProcessWitnessError as exc:
            problems.append(f"live witness validation failed: {exc}")

    return len(problems) == 0, problems
