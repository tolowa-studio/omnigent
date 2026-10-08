"""Focused tests for the opt-in Gate A real-operator task path."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from dev.factory.gate_a_real.agent_run import (
    assert_argv_safe,
    extract_session_ids,
    review_stdout_passes,
    run_agent_capture,
    stream_terminal_result_text,
)
from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from dev.factory.gate_a_real.deliverables import collect_deliverables, write_freeze_candidate
from dev.factory.gate_a_real.orchestration import RealTaskRunOptions, run_real_task_gate
from dev.factory.gate_a_real.profile import (
    materialize_real_task_cursor_config_dir,
    materialize_real_task_review_config_dir,
    real_task_cursor_cli_env,
)
from dev.factory.gate_a_real.receipt import validate_review_only_resume
from dev.factory.gate_a_real.spec import (
    RealTaskSpecError,
    canonical_spec_sha256,
    load_real_task_spec,
)


def _write_spec(tmp_path: Path, workspace: Path, **overrides: object) -> Path:
    profile_dir = tmp_path / "profile"
    profile = materialize_real_task_cursor_config_dir(profile_dir)
    spec: dict[str, object] = {
        "task_id": "unit-task",
        "workspace": str(workspace.resolve()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "prompt": "do the thing",
        "deliverable_paths": ["out.txt"],
        "verify_command": ["test", "-f", "out.txt"],
        "config_hashes": profile["effective_config_hashes"],
    }
    spec.update(overrides)
    spec["spec_sha256"] = canonical_spec_sha256(spec)
    path = tmp_path / "task.spec.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _review_pass_stream() -> str:
    return "\n".join(
        [
            json.dumps({"type": "system", "session_id": "review-session"}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "REVIEW: PASS\n",
                }
            ),
        ]
    )


def test_canonical_spec_hash_rejects_tamper(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    loaded = load_real_task_spec(spec_path)
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    document["prompt"] = "tampered"
    document["spec_sha256"] = canonical_spec_sha256(document)
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    bad = load_real_task_spec(spec_path)
    assert bad.prompt == "tampered"
    assert loaded.spec_sha256 != bad.spec_sha256


def test_load_spec_requires_absolute_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("x", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile = materialize_real_task_cursor_config_dir(profile_dir)
    spec = {
        "task_id": "t",
        "workspace": "relative/ws",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "prompt": "p",
        "deliverable_paths": ["out.txt"],
        "verify_command": ["true"],
        "config_hashes": profile["effective_config_hashes"],
    }
    spec["spec_sha256"] = canonical_spec_sha256(spec)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(RealTaskSpecError, match="absolute"):
        load_real_task_spec(path)


def test_load_spec_rejects_absolute_deliverable_path(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(RealTaskSpecError, match="workspace-relative"):
        load_real_task_spec(_write_spec(tmp_path, workspace, deliverable_paths=["/etc/passwd"]))


def test_assert_argv_safe_rejects_yolo() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        assert_argv_safe(["agent", "--yolo", "hi"])


def test_extract_session_ids_from_stream() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "session_id": "sess-a"}),
            json.dumps({"type": "tool_call", "session_id": "sess-b", "subtype": "started"}),
        ]
    )
    assert extract_session_ids(stdout) == ("sess-a", "sess-b")


def test_review_stdout_passes_terminal_result_only() -> None:
    assert review_stdout_passes(_review_pass_stream())
    assert not review_stdout_passes(
        "\n".join(
            [
                json.dumps({"type": "system", "session_id": "s"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": 'quoted "REVIEW: PASS" is not valid\n',
                    }
                ),
            ]
        )
    )
    assert not review_stdout_passes(
        "\n".join(
            [
                json.dumps({"type": "system", "session_id": "s"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "prefix REVIEW: PASS\n",
                    }
                ),
            ]
        )
    )
    assert not review_stdout_passes(
        "\n".join(
            [
                json.dumps({"type": "system", "session_id": "s"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "REVIEW: FAIL\n",
                    }
                ),
            ]
        )
    )
    assert not review_stdout_passes(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "REVIEW: PASS\nREVIEW: FAIL\n",
            }
        )
    )


def test_review_profile_denies_mutation_and_mcp(tmp_path: Path) -> None:
    profile = materialize_real_task_review_config_dir(tmp_path / "review")
    config = json.loads((Path(profile["cursor_config_dir"]) / "cli-config.json").read_text())
    denied = set(config["permissions"]["deny"])
    assert {"Write(*)", "Edit(*)", "Delete(*)", "Shell(*)", "Mcp(*:*)"} <= denied


def test_stream_terminal_result_text_ignores_non_terminal() -> None:
    stdout = json.dumps({"type": "assistant", "message": {"content": [{"text": "REVIEW: PASS"}]}})
    assert stream_terminal_result_text(stdout) is None


def test_collect_deliverables_tracked_flag(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "out.txt").write_text("ok", encoding="utf-8")
    inv = collect_deliverables(workspace, ("out.txt",))
    assert inv.records[0].size_bytes == 2
    assert inv.manifest_sha256


def test_freeze_rejects_source_changed_after_inventory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "out.txt"
    output.write_text("before", encoding="utf-8")
    inventory = collect_deliverables(workspace, ("out.txt",))
    output.write_text("after", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen file hash differs"):
        write_freeze_candidate(inventory, workspace, tmp_path / "freeze")


def test_collect_deliverables_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        collect_deliverables(workspace, ("link.txt",))


def test_real_task_env_sets_cloudsdk_when_fleet_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = tmp_path / "fleet-gcloud"
    fleet.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setattr(
        "dev.factory.gate_a_real.profile.trusted_motion_cursor_gcloud_config",
        lambda: str(fleet),
    )
    env = real_task_cursor_cli_env(
        cursor_config_dir=str(tmp_path / "cfg"), home_dir=str(tmp_path / "cli-home")
    )
    assert env["CLOUDSDK_CONFIG"] == str(fleet)
    assert env["HOME"] == str(tmp_path / "cli-home")
    assert "CURSOR_API_KEY" not in env


def test_run_real_task_gate_requires_opt_in(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    spec = load_real_task_spec(spec_path)
    with pytest.raises(Exception, match=REAL_TASK_ENV):
        run_real_task_gate(
            spec,
            RealTaskRunOptions(artifacts_dir=tmp_path / "artifacts"),
        )


def test_run_real_task_gate_rejects_artifacts_inside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = load_real_task_spec(_write_spec(tmp_path, workspace))
    result = run_real_task_gate(spec, RealTaskRunOptions(artifacts_dir=workspace / "artifacts"))
    assert not result.receipt.ok
    assert any("outside" in p for p in result.problems)


@pytest.mark.parametrize("delete_during_review", [False, True])
def test_run_real_task_gate_happy_path_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_during_review: bool
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("ok", encoding="utf-8")
    spec_path = _write_spec(tmp_path, workspace)
    spec = load_real_task_spec(spec_path)
    artifacts = tmp_path / "artifacts"

    from dev.factory.gate_a_real.agent_run import AgentRunCapture

    def fake_capture(
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout_seconds: float,
        sandbox_argv_wrapper: list[str] | None = None,
        stdout_log: Path | None = None,
        stderr_log: Path | None = None,
    ) -> AgentRunCapture:
        del cwd, env, timeout_seconds, sandbox_argv_wrapper
        is_review = "--mode" in argv and "ask" in argv
        if is_review:
            stdout = _review_pass_stream()
            if delete_during_review:
                (workspace / "out.txt").unlink()
        else:
            stdout = json.dumps({"type": "system", "session_id": "build-session"}) + "\n"
            stdout += json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "done",
                }
            )
        assert stdout_log is not None and stderr_log is not None
        stdout_log.write_text(stdout, encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        session = ("review-session",) if is_review else ("build-session",)
        return AgentRunCapture(
            argv=argv,
            exit_code=0,
            stdout=stdout,
            stderr="",
            session_ids=session,
            timed_out=False,
            api_key_inherited=False,
            unexpected_mcp_activity=False,
            pid=111,
            pgid=111,
            stdout_log_path=str(stdout_log),
            stderr_log_path=str(stderr_log),
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
        )

    class _FakeSandbox:
        def wrap_argv(self, argv: list[str]) -> list[str]:
            return ["sandbox-exec", "-f", "/tmp/fake.sb", *argv]

        def cleanup(self) -> None:
            return None

    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.prepare_gate_a_cursor_cli_sandbox",
        lambda _home: _FakeSandbox(),
    )
    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.discover_and_assert_zero_mcp_servers",
        lambda *a, **k: {"gate_passed": True, "gate_failure_reasons": []},
    )
    monkeypatch.setattr("dev.factory.gate_a_real.orchestration.run_agent_capture", fake_capture)
    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.resolve_cursor_executable",
        lambda: "/fake/motion-cursor-agent",
    )

    result = run_real_task_gate(spec, RealTaskRunOptions(artifacts_dir=artifacts))
    assert result.receipt.ok is not delete_during_review
    assert result.receipt.review_pass is not delete_during_review
    assert (artifacts / "receipt.json").is_file()
    assert (artifacts / "freeze" / "manifest.json").is_file()
    captured_review = json.loads(
        (artifacts / "review-attempts" / "0001" / "meta.json").read_text(encoding="utf-8")
    )
    assert captured_review["session_ids"] == ["review-session"]


def test_verify_failure_skips_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = load_real_task_spec(
        _write_spec(tmp_path, workspace, verify_command=["false"], deliverable_paths=["out.txt"])
    )
    (workspace / "out.txt").write_text("ok", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    review_calls = 0

    from dev.factory.gate_a_real.agent_run import AgentRunCapture

    def fake_capture(*args, **kwargs) -> AgentRunCapture:
        nonlocal review_calls
        argv = args[0]
        if "--mode" in argv:
            review_calls += 1
        stdout_log = kwargs["stdout_log"]
        stderr_log = kwargs["stderr_log"]
        stdout = json.dumps({"type": "system", "session_id": "build-session"}) + "\n"
        stdout += json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
        )
        stdout_log.write_text(stdout, encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        return AgentRunCapture(
            argv=argv,
            exit_code=0,
            stdout=stdout,
            stderr="",
            session_ids=("build-session",),
            timed_out=False,
            api_key_inherited=False,
            unexpected_mcp_activity=False,
            stdout_log_path=str(stdout_log),
            stderr_log_path=str(stderr_log),
            stdout_sha256="c" * 64,
            stderr_sha256="d" * 64,
        )

    class _FakeSandbox:
        def wrap_argv(self, argv: list[str]) -> list[str]:
            return argv

        def cleanup(self) -> None:
            return None

    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.prepare_gate_a_cursor_cli_sandbox",
        lambda _home: _FakeSandbox(),
    )
    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.discover_and_assert_zero_mcp_servers",
        lambda *a, **k: {"gate_passed": True, "gate_failure_reasons": []},
    )
    monkeypatch.setattr("dev.factory.gate_a_real.orchestration.run_agent_capture", fake_capture)
    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.resolve_cursor_executable",
        lambda: "/fake/motion-cursor-agent",
    )

    result = run_real_task_gate(spec, RealTaskRunOptions(artifacts_dir=artifacts))
    assert review_calls == 0
    assert not result.receipt.ok
    assert (artifacts / "review-attempts").exists() is False


def test_review_only_without_builder_state_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("ok", encoding="utf-8")
    spec = load_real_task_spec(_write_spec(tmp_path, workspace))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    class _FakeSandbox:
        def wrap_argv(self, argv: list[str]) -> list[str]:
            return argv

        def cleanup(self) -> None:
            return None

    with (
        patch(
            "dev.factory.gate_a_real.orchestration.prepare_gate_a_cursor_cli_sandbox",
            return_value=_FakeSandbox(),
        ),
        patch(
            "dev.factory.gate_a_real.orchestration.discover_and_assert_zero_mcp_servers",
            return_value={"gate_passed": True, "gate_failure_reasons": []},
        ),
        patch(
            "dev.factory.gate_a_real.orchestration.resolve_cursor_executable",
            return_value="/fake/motion-cursor-agent",
        ),
    ):
        result = run_real_task_gate(
            spec,
            RealTaskRunOptions(artifacts_dir=artifacts, review_only=True),
        )
    assert not result.receipt.ok
    assert any("review-only" in p for p in result.problems)


def test_validate_review_only_resume_detects_spec_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = load_real_task_spec(_write_spec(tmp_path, workspace))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "freeze").mkdir()
    (artifacts / "freeze" / "manifest.json").write_text("[]\n", encoding="utf-8")
    (artifacts / "builder.meta.json").write_text(
        json.dumps({"stdout_sha256": "e" * 64}) + "\n",
        encoding="utf-8",
    )
    prior = {
        "builder_ok": True,
        "spec_sha256": "0" * 64,
        "workspace": str(workspace),
        "builder_exit_code": 0,
        "builder_session_ids": ["sess"],
        "deliverable_manifest_sha256": "f" * 64,
        "builder_stdout_sha256": "e" * 64,
    }
    problems = validate_review_only_resume(spec, artifacts, prior)
    assert any("spec_sha256" in p for p in problems)


def test_review_only_detects_builder_log_tamper(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = load_real_task_spec(_write_spec(tmp_path, workspace))
    artifacts = tmp_path / "artifacts"
    frozen = artifacts / "freeze" / "files" / "out.txt"
    frozen.parent.mkdir(parents=True)
    frozen.write_text("ok", encoding="utf-8")
    frozen_sha = hashlib.sha256(frozen.read_bytes()).hexdigest()
    manifest = artifacts / "freeze" / "manifest.json"
    manifest.write_text(json.dumps([{"relpath": "out.txt", "sha256": frozen_sha}]) + "\n")
    builder_log = artifacts / "builder.stdout.txt"
    builder_log.write_text("original", encoding="utf-8")
    log_sha = hashlib.sha256(builder_log.read_bytes()).hexdigest()
    (artifacts / "builder.meta.json").write_text(json.dumps({"stdout_sha256": log_sha}))
    prior = {
        "builder_ok": True,
        "spec_sha256": spec.spec_sha256,
        "workspace": str(spec.workspace),
        "builder_exit_code": 0,
        "builder_session_ids": ["session-1"],
        "deliverable_manifest_sha256": "a" * 64,
        "freeze_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "builder_stdout_sha256": log_sha,
    }
    assert validate_review_only_resume(spec, artifacts, prior) == []
    builder_log.write_text("tampered", encoding="utf-8")
    assert any(
        "builder stdout content hash drift" in problem
        for problem in validate_review_only_resume(spec, artifacts, prior)
    )


def test_run_agent_capture_rejects_inherited_api_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not inherit"):
        run_agent_capture(
            ["agent", "hi"],
            cwd=".",
            env={"CURSOR_API_KEY": "secret", "PATH": "/usr/bin"},
            timeout_seconds=1.0,
            stdout_log=tmp_path / "out.txt",
            stderr_log=tmp_path / "err.txt",
        )
