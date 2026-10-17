"""Tests for Gate A real-task factory artifact retention (TOL-1402)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from dev.factory.gate_a_real.artifact_retention import (
    RUN_LOCK_FILE,
    RUN_METADATA_SCHEMA,
    begin_artifact_run,
    finish_artifact_run,
    is_artifact_run_dir,
    list_factory_run_dirs,
    prune_after_real_task_run,
    prune_artifacts_root,
    resolve_artifacts_root,
    select_runs_to_delete,
)
from dev.factory.gate_a_real.constants import (
    REAL_TASK_ARTIFACTS_ENV,
    REAL_TASK_ARTIFACTS_ROOT_ENV,
    REAL_TASK_RUN_PR_NUMBER_ENV,
    REAL_TASK_RUN_PR_STATE_ENV,
)
from dev.factory.gate_a_real.receipt import RECEIPT_SCHEMA


def _touch_task_dir(
    root: Path,
    task_id: str,
    *,
    started_at: datetime | None = None,
    status: str | None = None,
    pr_merged: bool | None = None,
    pr_state: str | None = None,
    receipt: dict[str, object] | None = None,
    builder_stdout: bool = False,
    lock_pid: int | None = None,
) -> Path:
    run_dir = root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if started_at is not None or status is not None or pr_merged is not None or pr_state is not None:
        meta: dict[str, object] = {"schema": RUN_METADATA_SCHEMA, "task_id": task_id}
        if started_at is not None:
            meta["started_at"] = started_at.isoformat()
        if status is not None:
            meta["status"] = status
        if pr_merged is not None:
            meta["pr_merged"] = pr_merged
        if pr_state is not None:
            meta["pr_state"] = pr_state
        (run_dir / "run.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    if receipt is not None:
        body = {"schema": RECEIPT_SCHEMA, **receipt}
        (run_dir / "receipt.json").write_text(json.dumps(body) + "\n", encoding="utf-8")
    if builder_stdout:
        (run_dir / "builder.stdout.txt").write_text("partial\n", encoding="utf-8")
    if lock_pid is not None:
        (run_dir / RUN_LOCK_FILE).write_text(f"{lock_pid}\n", encoding="utf-8")
    return run_dir


def _set_mtime(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def test_is_artifact_run_dir_requires_task_id_and_marker(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    empty = root / "tol-1402-feature"
    empty.mkdir()
    assert is_artifact_run_dir(empty) is False
    marked = _touch_task_dir(
        root,
        "tol-1402-feature",
        receipt={"ok": True, "completed_at": "2026-10-17T12:00:00+00:00"},
    )
    assert is_artifact_run_dir(marked) is True
    other = root / "not a task!"
    other.mkdir()
    assert is_artifact_run_dir(other) is False
    logs = root / "logs"
    logs.mkdir()
    (logs / "factory.log").write_text("noise\n", encoding="utf-8")
    assert is_artifact_run_dir(logs) is False
    assert list_factory_run_dirs(root) == [] or all(r.name != "logs" for r in list_factory_run_dirs(root))


def test_invalid_receipt_does_not_block_abandoned_or_discovery(tmp_path: Path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    root.mkdir()
    run_dir = root / "tol-bad-receipt"
    run_dir.mkdir()
    (run_dir / "receipt.json").write_text('{"schema": "wrong"}\n', encoding="utf-8")
    (run_dir / "builder.stdout.txt").write_text("partial\n", encoding="utf-8")
    _set_mtime(run_dir, now - timedelta(days=20))
    assert is_artifact_run_dir(run_dir) is True
    records = list_factory_run_dirs(root, now=now)
    stale = next(record for record in records if record.name == "tol-bad-receipt")
    assert stale.in_progress is False


def test_resolve_artifacts_root_single_spec_without_factory_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifact-roots"
    root.mkdir()
    task_dir = _touch_task_dir(
        root,
        "tol-1402-feature",
        receipt={"ok": True, "completed_at": "2026-10-17T12:00:00+00:00"},
    )
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ROOT_ENV, raising=False)
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ENV, raising=False)
    assert resolve_artifacts_root(current_run_dir=task_dir) == root.resolve()
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(task_dir))
    assert resolve_artifacts_root(current_run_dir=task_dir) == root.resolve()


def test_abandoned_dir_without_receipt_is_not_in_progress(tmp_path: Path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    root.mkdir()
    abandoned = _touch_task_dir(
        root,
        "tol-stale-abandoned",
        started_at=now - timedelta(days=10),
        builder_stdout=True,
    )
    _set_mtime(abandoned, now - timedelta(days=10))
    records = list_factory_run_dirs(root, now=now)
    stale = next(record for record in records if record.name == "tol-stale-abandoned")
    assert stale.in_progress is False


def test_abandoned_dir_is_selected_for_deletion(tmp_path: Path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    root.mkdir()
    abandoned = _touch_task_dir(
        root,
        "tol-stale-abandoned",
        started_at=now - timedelta(days=10),
        builder_stdout=True,
    )
    _set_mtime(abandoned, now - timedelta(days=10))
    from dev.factory.gate_a_real.artifact_retention import ArtifactRetentionConfig

    config = ArtifactRetentionConfig(keep_last=0, keep_hours=48)
    records = list_factory_run_dirs(root, now=now, config=config)
    _keep, to_delete = select_runs_to_delete(records, config=config, now=now)
    assert "tol-stale-abandoned" in {record.name for record in to_delete}


def test_active_lock_keeps_abandoned_candidate_in_progress(tmp_path: Path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    root.mkdir()
    held = _touch_task_dir(
        root,
        "tol-held",
        started_at=now - timedelta(days=10),
        builder_stdout=True,
        lock_pid=os.getpid(),
    )
    records = list_factory_run_dirs(root, now=now)
    record = next(item for item in records if item.name == "tol-held")
    assert record.in_progress is True


def test_begin_and_finish_write_run_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "tol-1402-feature"
    artifacts.mkdir()
    monkeypatch.setenv(REAL_TASK_RUN_PR_NUMBER_ENV, "32")
    monkeypatch.setenv(REAL_TASK_RUN_PR_STATE_ENV, "open")
    begin_artifact_run(artifacts, "tol-1402-feature")
    assert (artifacts / RUN_LOCK_FILE).is_file()
    run_data = json.loads((artifacts / "run.json").read_text(encoding="utf-8"))
    assert run_data["schema"] == RUN_METADATA_SCHEMA
    assert run_data["status"] == "in_progress"
    assert run_data["task_id"] == "tol-1402-feature"
    (artifacts / "receipt.json").write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "ok": True,
                "task_id": "tol-1402-feature",
                "completed_at": "2026-10-17T12:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    finish_artifact_run(artifacts)
    assert not (artifacts / RUN_LOCK_FILE).exists()
    final = json.loads((artifacts / "run.json").read_text(encoding="utf-8"))
    assert final["status"] == "completed"
    assert final["pr_number"] == 32
    assert final["pr_state"] == "open"
    assert final["pr_merged"] is False


def test_prune_after_real_task_run_swallows_exceptions(tmp_path: Path) -> None:
    run_dir = tmp_path / "tol-current"
    run_dir.mkdir()
    with patch(
        "dev.factory.gate_a_real.artifact_retention.prune_artifacts_root",
        side_effect=RuntimeError("boom"),
    ):
        result = prune_after_real_task_run(run_dir)
    assert result is None


def test_m2_layout_fixture_dry_run_deletes(tmp_path: Path) -> None:
    """Mirrors ARTIFACTS_ROOT/<task_id>/ on M2; documents expected dry-run deletes."""
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact-roots"
    root.mkdir()
    _touch_task_dir(
        root,
        "tol-1402-feature-a",
        started_at=now - timedelta(hours=6),
        receipt={"ok": True, "completed_at": (now - timedelta(hours=6)).isoformat()},
    )
    _touch_task_dir(
        root,
        "tol-1402-feature-b",
        started_at=now - timedelta(days=14),
        receipt={"ok": True, "completed_at": (now - timedelta(days=14)).isoformat()},
    )
    abandoned = _touch_task_dir(
        root,
        "tol-1388-stale-abandoned",
        started_at=now - timedelta(days=20),
        builder_stdout=True,
    )
    _set_mtime(abandoned, now - timedelta(days=20))
    _touch_task_dir(
        root,
        "tol-1390-open-pr",
        started_at=now - timedelta(days=30),
        pr_state="open",
        pr_merged=False,
        receipt={"ok": True, "completed_at": (now - timedelta(days=30)).isoformat()},
    )
    _touch_task_dir(
        root,
        "tol-1399-active",
        started_at=now - timedelta(days=30),
        status="in_progress",
        lock_pid=os.getpid(),
    )
    from dev.factory.gate_a_real.artifact_retention import ArtifactRetentionConfig

    config = ArtifactRetentionConfig(keep_last=1, keep_hours=48)
    result = prune_artifacts_root(root, config=config, now=now, dry_run=True)
    assert set(result.deleted) == {
        "tol-1402-feature-b",
        "tol-1388-stale-abandoned",
    }
    assert "tol-1402-feature-a" in result.kept
    assert "tol-1390-open-pr" in result.kept
    assert "tol-1399-active" in result.kept


def test_list_factory_run_dirs_ignores_symlink_children(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    real = tmp_path / "outside"
    real.mkdir()
    link = root / "tol-symlink"
    link.symlink_to(real, target_is_directory=True)
    assert list_factory_run_dirs(root) == []
    assert real.is_dir()


def test_resolve_artifacts_root_from_registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    _touch_task_dir(root, "tol-task", receipt={"ok": False, "completed_at": "2026-01-01T00:00:00+00:00"})
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ROOT_ENV, str(root))
    assert resolve_artifacts_root() == root.resolve()


def test_resolve_artifacts_root_refuses_when_artifacts_env_points_at_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    root = tmp_path / "artifact-roots"
    root.mkdir()
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ROOT_ENV, raising=False)
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(root))
    assert resolve_artifacts_root() is None
    assert "artifacts root" in caplog.text.lower()
    run_dir = _touch_task_dir(
        root,
        "tol-1402-feature",
        receipt={"ok": True, "completed_at": "2026-10-17T12:00:00+00:00"},
    )
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(root))
    assert resolve_artifacts_root(current_run_dir=run_dir) == root.resolve()
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(root))
    assert resolve_artifacts_root(current_run_dir=root) is None


def test_prune_never_deletes_logs_sibling(tmp_path: Path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact-roots"
    root.mkdir()
    logs = root / "logs"
    logs.mkdir()
    (logs / "rollout.log").write_text("x" * 10_000 + "\n", encoding="utf-8")
    _touch_task_dir(
        root,
        "tol-stale",
        started_at=now - timedelta(days=30),
        receipt={"ok": True, "completed_at": (now - timedelta(days=30)).isoformat()},
    )
    from dev.factory.gate_a_real.artifact_retention import ArtifactRetentionConfig

    config = ArtifactRetentionConfig(keep_last=0, keep_hours=0)
    prune_artifacts_root(root, config=config, now=now, dry_run=False)
    assert logs.is_dir()
    assert not (root / "tol-stale").exists()
