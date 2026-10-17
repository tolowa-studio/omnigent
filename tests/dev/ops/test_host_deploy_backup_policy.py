"""Tests for host rollback backup scope, size cap, retention, and dry-run planning."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dev.ops.host_deploy import backup_policy as policy
from dev.ops.host_deploy.backup_and_stop import build_backup_plan


def test_relative_path_is_excluded() -> None:
    assert policy.relative_path_is_excluded(Path("logs/server.log"))
    assert policy.relative_path_is_excluded(Path("omnigent/logs/2026/app.log"))
    assert policy.relative_path_is_excluded(Path("transcripts/session.jsonl"))
    assert policy.relative_path_is_excluded(Path("artifacts/run-1/output.txt"))
    assert policy.relative_path_is_excluded(Path("cache/embeddings.bin"))
    assert not policy.relative_path_is_excluded(Path("config.yaml"))
    assert not policy.relative_path_is_excluded(Path("chat.db"))


def test_planned_backup_bytes_skips_logs_and_artifacts(tmp_path: Path) -> None:
    data = tmp_path / "omnigent"
    data.mkdir()
    (data / "config.yaml").write_text("x")
    (data / "chat.db").write_bytes(b"\x00" * 100)
    (data / "logs").mkdir()
    (data / "logs" / "big.log").write_bytes(b"x" * 50_000)
    (data / "artifacts").mkdir()
    (data / "artifacts" / "blob").write_bytes(b"y" * 80_000)
    (data / "notes.jsonl").write_bytes(b"z" * 10_000)

    planned = policy.planned_backup_bytes(directory_roots=[data], file_paths=[])
    assert planned == 100 + len(b"x")


def test_assert_within_cap_raises_over_limit() -> None:
    with pytest.raises(policy.BackupSizeExceededError) as exc:
        policy.assert_within_cap(300, 200)
    assert exc.value.planned_bytes == 300
    assert exc.value.cap_bytes == 200


def test_prune_rollbacks_only_under_rollback_dir(tmp_path: Path) -> None:
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    for name in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (rollback / name).mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    symlink_escape = rollback / "escape"
    symlink_escape.symlink_to(outside, target_is_directory=True)

    removed = policy.prune_rollbacks(rollback, keep=3, dry_run=False)
    assert [p.name for p in removed] == ["20260101T000000Z"]
    remaining = {p.name for p in policy.list_rollback_stamps(rollback)}
    assert remaining == {"20260102T000000Z", "20260103T000000Z"}
    assert outside.exists()


def test_prune_refuses_path_outside_rollback_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    (rollback / "20260101T000000Z").mkdir()

    def _bad_plan(_rollback_dir: Path, keep: int = 3) -> list[Path]:
        return [tmp_path / "outside"]

    monkeypatch.setattr(policy, "plan_rollback_prune", _bad_plan)
    with pytest.raises(RuntimeError, match="refusing to prune"):
        policy.prune_rollbacks(rollback, dry_run=False)


def test_dry_run_prints_plan_and_byte_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    control = tmp_path / "control"
    control.mkdir()
    preflight = control / "preflight.json"
    preflight.write_text(
        json.dumps(
            {
                "installed_commit": "abc",
                "version": "0.1",
                "running": True,
                "daemon_attached": True,
                "session_inventory": {
                    "pages": 1,
                    "total": 0,
                    "unique": 0,
                    "running": 0,
                    "waiting": 0,
                    "idle": 0,
                    "failed": 0,
                    "id_status_sha256": "0",
                },
                "launch_agents": [],
                "critical_store_symlinks": 0,
                "external_harness_symlinks_preserved_as_links": [],
            }
        )
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text("ok")
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.DATA", data)
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.TOOL", tmp_path / "missing-tool")
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.PLISTS", ())
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.HOME", tmp_path)
    monkeypatch.setenv("OMNIGENT_HOST_DEPLOY_CONTROL", str(control))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup_and_stop.py",
            "--preflight-json",
            str(preflight),
            "--dry-run",
        ],
    )
    from dev.ops.host_deploy.backup_and_stop import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["backup_plan"]["planned_bytes"] == len("ok")
    assert str(data) in payload["backup_plan"]["directory_roots"]


def test_build_backup_plan_lists_excluded_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "omnigent"
    data.mkdir()
    (data / "config.yaml").write_text("c")
    (data / "logs").mkdir()
    (data / "logs" / "a.log").write_text("log")
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.DATA", data)
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.TOOL", tmp_path / "tool-missing")
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.PLISTS", ())
    monkeypatch.setattr("dev.ops.host_deploy.backup_and_stop.HOME", tmp_path)

    plan = build_backup_plan(200 * 1024 * 1024)
    assert plan["planned_bytes"] == 1
    assert "logs" in plan["excluded_sample_paths"][0]
