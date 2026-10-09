"""Focused tests for Gate A real-task status receipt snapshot checks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev.factory.gate_a_real.deliverables import collect_deliverables
from dev.factory.gate_a_real.profile import (
    materialize_real_task_cursor_config_dir,
    materialize_real_task_review_config_dir,
)
from dev.factory.gate_a_real.receipt import RealTaskReceipt, utc_now_iso
from dev.factory.gate_a_real.spec import canonical_spec_sha256, load_real_task_spec
from omnigent.factory.gate_a.real_chat import RECEIPT_SCHEMA, read_status_summary


def _write_spec(tmp_path: Path, workspace: Path, task_id: str = "unit-task") -> Path:
    profile_dir = tmp_path / "profile"
    profile = materialize_real_task_cursor_config_dir(profile_dir)
    review_profile = materialize_real_task_review_config_dir(tmp_path / "review-profile")
    spec: dict[str, object] = {
        "task_id": task_id,
        "workspace": str(workspace.resolve()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "prompt": "do the thing",
        "deliverable_paths": ["out.txt"],
        "verify_command": ["test", "-f", "out.txt"],
        "config_hashes": profile["effective_config_hashes"],
        "review_config_hashes": review_profile["effective_config_hashes"],
    }
    spec["spec_sha256"] = canonical_spec_sha256(spec)
    path = tmp_path / "task.spec.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _valid_ok_receipt(spec_path: Path, workspace: Path) -> RealTaskReceipt:
    spec = load_real_task_spec(spec_path)
    inventory = collect_deliverables(workspace, spec.deliverable_paths)
    manifest_sha = inventory.manifest_sha256
    return RealTaskReceipt(
        ok=True,
        task_id=spec.task_id,
        spec_sha256=spec.spec_sha256,
        workspace=str(spec.workspace),
        problems=[],
        builder_session_ids=["build-1"],
        review_session_ids=["review-1"],
        builder_exit_code=0,
        review_exit_code=0,
        verify_exit_code=0,
        deliverable_manifest_sha256=manifest_sha,
        post_review_manifest_sha256=manifest_sha,
        freeze_manifest_path="/tmp/artifacts/freeze/manifest.json",
        review_pass=True,
        completed_at=utc_now_iso(),
    )


def _status_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("deliverable body\n", encoding="utf-8")
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return spec_path, artifacts, workspace


def test_read_status_accepts_consistent_ok_receipt(tmp_path: Path) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    _valid_ok_receipt(spec_path, workspace).write(artifacts / "receipt.json")
    summary = read_status_summary(
        spec_path=spec_path,
        artifacts_dir=artifacts,
        task_id="unit-task",
    )
    assert "receipt_ok: True" in summary
    assert "rehashes workspace deliverables" in summary


def test_read_status_v1_receipt_without_omnigent_session_id(tmp_path: Path) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    receipt = _valid_ok_receipt(spec_path, workspace)
    payload = receipt.to_dict()
    assert "omnigent_session_id" not in payload
    (artifacts / "receipt.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    summary = read_status_summary(
        spec_path=spec_path,
        artifacts_dir=artifacts,
        task_id="unit-task",
    )
    assert "receipt_ok: True" in summary
    assert "omnigent_session_id: (none)" in summary


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("problems", ["leftover"], "problems to be empty"),
        ("review_pass", False, "review_pass true"),
        ("builder_exit_code", 1, "builder_exit_code 0"),
        ("builder_exit_code", None, "builder_exit_code 0"),
        ("review_exit_code", 1, "review_exit_code 0"),
        ("review_exit_code", None, "review_exit_code 0"),
        ("verify_exit_code", 1, "verify_exit_code 0"),
        ("verify_exit_code", None, "verify_exit_code 0"),
        ("builder_session_ids", [], "nonempty builder_session_ids"),
        ("review_session_ids", [], "nonempty review_session_ids"),
        ("freeze_manifest_path", None, "freeze_manifest_path"),
        ("deliverable_manifest_sha256", None, "deliverable_manifest_sha256"),
        ("post_review_manifest_sha256", None, "post_review_manifest_sha256"),
    ],
)
def test_read_status_rejects_ok_with_inconsistent_evidence(
    tmp_path: Path,
    field: str,
    value: object,
    match: str,
) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    receipt = _valid_ok_receipt(spec_path, workspace)
    setattr(receipt, field, value)
    receipt.write(artifacts / "receipt.json")
    with pytest.raises(ValueError, match=match):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="unit-task",
        )


def test_read_status_rejects_ok_with_invalid_manifest_hex(tmp_path: Path) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    receipt = _valid_ok_receipt(spec_path, workspace)
    receipt.deliverable_manifest_sha256 = "not-hex"
    receipt.post_review_manifest_sha256 = "not-hex"
    receipt.write(artifacts / "receipt.json")
    with pytest.raises(ValueError, match="64 lowercase hex"):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="unit-task",
        )


def test_read_status_rejects_ok_with_mismatched_manifest_hashes(tmp_path: Path) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    receipt = _valid_ok_receipt(spec_path, workspace)
    receipt.deliverable_manifest_sha256 = "a" * 64
    receipt.post_review_manifest_sha256 = "b" * 64
    receipt.write(artifacts / "receipt.json")
    with pytest.raises(ValueError, match="to match"):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="unit-task",
        )


def test_read_status_failed_receipt_readable_with_partial_fields(tmp_path: Path) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    (artifacts / "receipt.json").write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "ok": False,
                "task_id": "unit-task",
                "spec_sha256": load_real_task_spec(spec_path).spec_sha256,
                "workspace": str(workspace.resolve()),
                "problems": ["verify failed"],
                "builder_session_ids": [],
                "review_session_ids": [],
                "builder_exit_code": None,
                "review_exit_code": None,
                "verify_exit_code": 2,
                "deliverable_manifest_sha256": None,
                "post_review_manifest_sha256": None,
                "freeze_manifest_path": None,
                "review_pass": False,
                "completed_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = read_status_summary(
        spec_path=spec_path,
        artifacts_dir=artifacts,
        task_id="unit-task",
    )
    assert "receipt_ok: False" in summary
    assert "problem_count: 1" in summary
    assert "verify_exit_code: 2" in summary
    assert "builder_session_ids: (none)" in summary


def test_read_status_failed_receipt_allows_contradictory_success_fields(tmp_path: Path) -> None:
    """ok=false snapshots are not subject to success-evidence cross-checks."""
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    (artifacts / "receipt.json").write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "ok": False,
                "task_id": "unit-task",
                "spec_sha256": load_real_task_spec(spec_path).spec_sha256,
                "workspace": str(workspace.resolve()),
                "problems": ["stalled"],
                "builder_session_ids": ["build-1"],
                "review_session_ids": [],
                "builder_exit_code": 0,
                "review_exit_code": 0,
                "verify_exit_code": 0,
                "deliverable_manifest_sha256": "a" * 64,
                "post_review_manifest_sha256": "b" * 64,
                "freeze_manifest_path": "/tmp/freeze/manifest.json",
                "review_pass": True,
                "completed_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = read_status_summary(
        spec_path=spec_path,
        artifacts_dir=artifacts,
        task_id="unit-task",
    )
    assert "receipt_ok: False" in summary
    assert "review_pass: True" in summary
    assert "no live deliverable verification" in summary


def test_read_status_rejects_ok_when_deliverable_changed(tmp_path: Path) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    _valid_ok_receipt(spec_path, workspace).write(artifacts / "receipt.json")
    (workspace / "out.txt").write_text("changed after receipt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_sha256 does not match"):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="unit-task",
        )


def test_read_status_rejects_ok_when_deliverable_deleted(tmp_path: Path) -> None:
    spec_path, artifacts, workspace = _status_paths(tmp_path)
    _valid_ok_receipt(spec_path, workspace).write(artifacts / "receipt.json")
    (workspace / "out.txt").unlink()
    with pytest.raises(ValueError, match="deliverable missing"):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="unit-task",
        )
