"""Positive MCP receipt binding (witness + SHA + evidence dir)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.checkout import canonical_evidence_root, list_evidence_archive_dir_names
from dev.factory.gate_a_mcp.preflight import preflight_ready_marker_path
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME
from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness
from dev.factory.gate_a_trial.positive_binding import sha256_hex_of_file, verify_positive_mcp_receipt
from dev.factory.gate_a_trial.prestarted_mcp import PrestartedGateAMcp
from dev.factory.gate_a_trial.secret_redact import redact_secrets
from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID
from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS

_BIND_NONCE = "nonce-bind"
_BIND_PID = os.getpid()


def _witness(**overrides: object) -> QualifiedProcessWitness:
    base = {
        "witness_nonce": _BIND_NONCE,
        "server_pid": _BIND_PID,
        "listen_host": "127.0.0.1",
        "listen_port": 49152,
        "mcp_url_path": "/mcp",
        "qualified_for_gate_a": True,
        "minted_monotonic": time.monotonic(),
        "settle_observed_seconds": GATE_A_MIN_SETTLE_SECONDS,
        "order_id": INTERNAL_STAGE_ORDER_ID,
    }
    base.update(overrides)
    return QualifiedProcessWitness(**base)  # type: ignore[arg-type]


def _prestarted(
    *,
    witness_nonce: str = _BIND_NONCE,
    server_pid: int = _BIND_PID,
) -> PrestartedGateAMcp:
    return PrestartedGateAMcp(
        control_dir=Path("/tmp/gate-a-mcp-control-test-unused"),
        control_parent=Path("/tmp"),
        witness_nonce=witness_nonce,
        server_pid=server_pid,
        capability_token="capability-token-not-logged",
        mcp_url="http://127.0.0.1:1/mcp",
        _proc=None,
    )


def _base_payload(artifact: Path, digest: str) -> dict:
    return {
        "ok": True,
        "gate_qualified": True,
        "child_ok": True,
        "artifact": BOUND_ARTIFACT_FILENAME,
        "artifact_path": str(artifact),
        "artifact_sha256": digest,
        "order_id": INTERNAL_STAGE_ORDER_ID,
        "settle_observed_seconds": GATE_A_MIN_SETTLE_SECONDS,
        "probe_sha256": "a" * 64,
        "manifest_hash": "b" * 64,
        "command_hash": "c" * 64,
        "witness_nonce": _BIND_NONCE,
        "server_pid": _BIND_PID,
    }


def test_verify_positive_receipt_sha_and_new_evidence_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-newrun-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    digest = sha256_hex_of_file(artifact)
    payload = _base_payload(artifact, digest)
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert ok, problems


def test_verify_positive_receipt_rejects_sha_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-badsha-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    payload = _base_payload(artifact, "0" * 64)
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("artifact_sha256" in p for p in problems)


def test_verify_positive_receipt_rejects_missing_witness_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-miss-nonce-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    payload = _base_payload(artifact, sha256_hex_of_file(artifact))
    del payload["witness_nonce"]
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("witness_nonce" in p for p in problems)


def test_verify_positive_receipt_rejects_wrong_witness_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-wrong-nonce-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    payload = _base_payload(artifact, sha256_hex_of_file(artifact))
    payload["witness_nonce"] = "forged-nonce"
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("witness_nonce" in p for p in problems)


def test_verify_positive_receipt_rejects_wrong_server_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-wrong-pid-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    payload = _base_payload(artifact, sha256_hex_of_file(artifact))
    payload["server_pid"] = _BIND_PID + 1
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("server_pid" in p for p in problems)


def test_verify_positive_receipt_rejects_forged_archive_with_valid_sha_while_witness_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversary mints a new evidence-* dir + SHA but cannot bind the live prestarted witness."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    forged_dir = root / f"evidence-forged-{os.getpid()}"
    forged_dir.mkdir()
    forged_artifact = forged_dir / BOUND_ARTIFACT_FILENAME
    forged_artifact.write_text("allowed\n", encoding="utf-8")
    digest = sha256_hex_of_file(forged_artifact)
    payload = _base_payload(forged_artifact, digest)
    payload["witness_nonce"] = "stream-forged-not-from-server"
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=forged_artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("witness_nonce" in p for p in problems)


def test_verify_positive_receipt_rejects_symlinked_evidence_archive_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New evidence-* name pointing at a pre-trial archive must not certify."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    old_dir = root / f"evidence-pre-{os.getpid()}"
    old_dir.mkdir()
    (old_dir / BOUND_ARTIFACT_FILENAME).write_text("allowed\n", encoding="utf-8")
    alias = root / f"evidence-alias-{os.getpid()}"
    alias.symlink_to(old_dir, target_is_directory=True)
    artifact = alias / BOUND_ARTIFACT_FILENAME
    digest = sha256_hex_of_file(old_dir / BOUND_ARTIFACT_FILENAME)
    payload = _base_payload(artifact, digest)
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("symlink" in p.lower() or "archive" in p.lower() for p in problems)


def test_verify_positive_receipt_rejects_allowed_txt_symlink_in_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-symlink-file-{os.getpid()}"
    archive_dir.mkdir()
    real = archive_dir / "real-allowed.txt"
    real.write_text("allowed\n", encoding="utf-8")
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.symlink_to(real)
    digest = sha256_hex_of_file(real)
    payload = _base_payload(artifact, digest)
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=_witness(),
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("symlink" in p.lower() for p in problems)


def test_verify_positive_receipt_genuine_real_archive_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-genuine-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    digest = sha256_hex_of_file(artifact)
    witness = _witness()
    payload = _base_payload(artifact, digest)
    payload["settle_observed_seconds"] = witness.settle_observed_seconds
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=witness,
        evidence_dirs_before=before,
    )
    assert ok, problems


def test_verify_positive_receipt_rejects_settle_not_bound_to_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-settle-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    witness = _witness(settle_observed_seconds=90.017)
    payload = _base_payload(artifact, sha256_hex_of_file(artifact))
    payload["settle_observed_seconds"] = 90.0
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=witness,
        evidence_dirs_before=before,
    )
    assert not ok
    assert any("settle_observed_seconds" in p for p in problems)


def test_verify_positive_receipt_preflight_marker_must_match_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = canonical_evidence_root()
    before = list_evidence_archive_dir_names()
    archive_dir = root / f"evidence-marker-{os.getpid()}"
    archive_dir.mkdir()
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")
    witness = _witness(settle_observed_seconds=90.05)
    payload = _base_payload(artifact, sha256_hex_of_file(artifact))
    payload["settle_observed_seconds"] = witness.settle_observed_seconds
    home = tmp_path / "mcp-home"
    home.mkdir()
    marker = preflight_ready_marker_path(home)
    marker.write_text(
        json.dumps(
            {
                "qualified_for_gate_a": True,
                "server_pid": _BIND_PID,
                "settle_observed_seconds": witness.settle_observed_seconds,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ok, problems = verify_positive_mcp_receipt(
        payload,
        artifact_path=artifact,
        prestarted=_prestarted(),
        witness=witness,
        evidence_dirs_before=before,
        mcp_server_home=home,
    )
    assert ok, problems


def test_redact_gate_a_capability_bearer() -> None:
    token = "capability-token-value-12345678"
    text = f'Authorization: Bearer {token}'
    redacted = redact_secrets(text, env={"GATE_A_MCP_HTTP_CAPABILITY": token})
    assert token not in redacted
    assert "Bearer <redacted:gate-a-capability>" in redacted
    assert _BIND_NONCE in redact_secrets(f"witness_nonce={_BIND_NONCE}", env={})
