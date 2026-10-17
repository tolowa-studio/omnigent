"""Tests for Gate A real-task factory artifact retention (TOL-1402)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev.factory.gate_a_real.artifact_retention import (
    ArtifactRetentionConfig,
    FactoryRunRecord,
    RUN_DIR_PREFIX,
    RUN_METADATA_SCHEMA,
    is_factory_run_dir,
    list_factory_run_dirs,
    prune_artifacts_root,
    resolve_artifacts_root,
    select_runs_to_delete,
)
from dev.factory.gate_a_real.constants import (
    REAL_TASK_ARTIFACTS_ENV,
    REAL_TASK_ARTIFACTS_ROOT_ENV,
)


def _touch_run(
    root: Path,
    name: str,
    *,
    started_at: datetime | None = None,
    status: str | None = None,
    pr_merged: bool | None = None,
    receipt_completed_at: str | None = None,
) -> Path:
    run_dir = root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {"schema": RUN_METADATA_SCHEMA}
    if started_at is not None:
        meta["started_at"] = started_at.isoformat()
    if status is not None:
        meta["status"] = status
    if pr_merged is not None:
        meta["pr_merged"] = pr_merged
    (run_dir / "run.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    if receipt_completed_at is not None:
        (run_dir / "receipt.json").write_text(
            json.dumps({"completed_at": receipt_completed_at}) + "\n",
            encoding="utf-8",
        )
    return run_dir


def test_is_factory_run_dir_matches_prefix_and_run_json(tmp_path: Path) -> None:
    factory = tmp_path / f"{RUN_DIR_PREFIX}abc"
    factory.mkdir()
    assert is_factory_run_dir(factory) is True
    other = tmp_path / "task-a"
    other.mkdir()
    assert is_factory_run_dir(other) is False
    with_meta = tmp_path / "custom-run"
    with_meta.mkdir()
    (with_meta / "run.json").write_text("{}\n", encoding="utf-8")
    assert is_factory_run_dir(with_meta) is True


def test_select_runs_to_delete_keeps_last_n_age_in_progress_and_unmerged(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    root.mkdir()
    old = _touch_run(
        root,
        f"{RUN_DIR_PREFIX}old",
        started_at=now - timedelta(days=5),
        receipt_completed_at=(now - timedelta(days=5)).isoformat(),
    )
    recent = _touch_run(
        root,
        f"{RUN_DIR_PREFIX}recent",
        started_at=now - timedelta(hours=2),
        receipt_completed_at=(now - timedelta(hours=2)).isoformat(),
    )
    in_progress = _touch_run(
        root,
        f"{RUN_DIR_PREFIX}running",
        started_at=now - timedelta(days=10),
        status="in_progress",
    )
    unmerged = _touch_run(
        root,
        f"{RUN_DIR_PREFIX}open-pr",
        started_at=now - timedelta(days=10),
        pr_merged=False,
        receipt_completed_at=(now - timedelta(days=10)).isoformat(),
    )
    records = list_factory_run_dirs(root)
    config = ArtifactRetentionConfig(keep_last=1, keep_hours=48)
    _keep, to_delete = select_runs_to_delete(records, config=config, now=now)
    delete_names = {record.name for record in to_delete}
    assert f"{RUN_DIR_PREFIX}old" in delete_names
    assert f"{RUN_DIR_PREFIX}recent" not in delete_names
    assert in_progress.name not in delete_names
    assert unmerged.name not in delete_names


def test_select_runs_to_delete_protects_current_run(tmp_path: Path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    root.mkdir()
    doomed = _touch_run(
        root,
        f"{RUN_DIR_PREFIX}doomed",
        started_at=now - timedelta(days=30),
        receipt_completed_at=(now - timedelta(days=30)).isoformat(),
    )
    records = list_factory_run_dirs(root)
    config = ArtifactRetentionConfig(keep_last=0, keep_hours=0)
    _keep, to_delete = select_runs_to_delete(
        records,
        config=config,
        now=now,
        protect_paths=frozenset({doomed.resolve()}),
    )
    assert to_delete == []


def test_prune_artifacts_root_deletes_and_respects_dry_run(tmp_path: Path) -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    root.mkdir()
    _touch_run(
        root,
        f"{RUN_DIR_PREFIX}stale",
        started_at=now - timedelta(days=10),
        receipt_completed_at=(now - timedelta(days=10)).isoformat(),
    )
    stale = root / f"{RUN_DIR_PREFIX}stale"
    config = ArtifactRetentionConfig(keep_last=0, keep_hours=0)
    dry = prune_artifacts_root(root, config=config, now=now, dry_run=True)
    assert stale.is_dir()
    assert dry.deleted == (f"{RUN_DIR_PREFIX}stale",)
    result = prune_artifacts_root(root, config=config, now=now, dry_run=False)
    assert not stale.exists()
    assert result.deleted == (f"{RUN_DIR_PREFIX}stale",)


def test_list_factory_run_dirs_ignores_symlink_children(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    real = tmp_path / "outside"
    real.mkdir()
    link = root / f"{RUN_DIR_PREFIX}link"
    link.symlink_to(real, target_is_directory=True)
    assert list_factory_run_dirs(root) == []
    assert real.is_dir()


def test_resolve_artifacts_root_from_env_and_factory_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    run = root / f"{RUN_DIR_PREFIX}run-1"
    run.mkdir()
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ROOT_ENV, str(root))
    assert resolve_artifacts_root() == root.resolve()
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ROOT_ENV, raising=False)
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(run))
    assert resolve_artifacts_root(current_run_dir=run) == root.resolve()
