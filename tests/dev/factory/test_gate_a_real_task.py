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
    builder_stdout_acceptable,
    extract_session_ids,
    review_stdout_forbidden_tool_violations,
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
    trusted_cursor_vendor_binary,
    trusted_motion_cursor_gcloud_config,
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
    review_profile = materialize_real_task_review_config_dir(tmp_path / "review-profile")
    spec: dict[str, object] = {
        "task_id": "unit-task",
        "workspace": str(workspace.resolve()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "prompt": "do the thing",
        "deliverable_paths": ["out.txt"],
        "verify_command": ["test", "-f", "out.txt"],
        "config_hashes": profile["effective_config_hashes"],
        "review_config_hashes": review_profile["effective_config_hashes"],
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
    review_profile = materialize_real_task_review_config_dir(tmp_path / "review-profile")
    spec = {
        "task_id": "t",
        "workspace": "relative/ws",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "prompt": "p",
        "deliverable_paths": ["out.txt"],
        "verify_command": ["true"],
        "config_hashes": profile["effective_config_hashes"],
        "review_config_hashes": review_profile["effective_config_hashes"],
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
    assert {
        "Shell",
        "Shell(*)",
        "Write",
        "Write(*)",
        "Edit",
        "Edit(*)",
        "Delete",
        "Delete(*)",
        "WebFetch",
        "WebFetch(*)",
        "Mcp",
        "Mcp(*)",
        "Mcp(*:*)",
        "GetMcpTools",
        "GetMcpTools(*)",
    } <= denied


def test_builder_stdout_requires_terminal_success_result() -> None:
    ok_stream = (
        json.dumps({"type": "system", "session_id": "s"})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"})
    )
    assert builder_stdout_acceptable(ok_stream) == (True, "")
    session_only = json.dumps({"type": "system", "session_id": "s"})
    assert builder_stdout_acceptable(session_only) == (
        False,
        "stream missing successful final result event",
    )
    error_result = (
        ok_stream
        + "\n"
        + json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "boom"})
    )
    assert builder_stdout_acceptable(error_result)[0] is False


def test_review_forbidden_tool_call_rejects_completed_shell() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "session_id": "s"}),
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "tool_call": {"name": "Shell", "args": {}},
                }
            ),
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
    violations = review_stdout_forbidden_tool_violations(stdout)
    assert violations
    assert not review_stdout_passes(stdout) or violations


def test_review_forbidden_tool_call_rejects_unparsed_variant() -> None:
    stdout = _review_pass_stream() + "\n" + json.dumps({"type": "tool_call", "weird": True})
    assert review_stdout_forbidden_tool_violations(stdout)


@pytest.mark.parametrize(
    "hidden_event",
    [
        '{"type":"tool_call",',
        "[]",
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell"}]}}
        ),
        json.dumps({"type": "assistant", "message": {"tool_call": {"getMcpToolsToolCall": {}}}}),
    ],
)
def test_review_rejects_unparsed_or_hidden_tool_evidence(hidden_event: str) -> None:
    assert review_stdout_forbidden_tool_violations(hidden_event + "\n" + _review_pass_stream())


@pytest.mark.parametrize("field", ["stdout", "exitCode", "shellToolCall"])
def test_review_read_result_rejects_execution_evidence(field: str) -> None:
    stdout = _native_tool_pair("readToolCall", {"success": {"content": "ok", field: "x"}})
    assert review_stdout_forbidden_tool_violations(stdout)


@pytest.mark.parametrize("location", ["result", "event", "wrapper"])
def test_review_read_call_rejects_nested_tool_use(location: str) -> None:
    events = [
        json.loads(line)
        for line in _native_tool_pair("readToolCall", {"success": {}}).splitlines()
    ]
    hidden = {"type": "tool_use", "name": "Shell", "input": {"command": "true"}}
    if location == "result":
        events[1]["tool_call"]["readToolCall"]["result"]["success"]["content"] = [hidden]
    elif location == "event":
        events[1]["model_call_id"] = {"nested": hidden}
    else:
        events[1]["tool_call"]["hookAdditionalContexts"] = [hidden]
    assert review_stdout_forbidden_tool_violations("\n".join(map(json.dumps, events)))


def test_review_read_call_rejects_changed_args_on_completion() -> None:
    events = [
        json.loads(line)
        for line in _native_tool_pair("readToolCall", {"success": {}}).splitlines()
    ]
    events[0]["tool_call"]["readToolCall"]["args"] = {"path": "first"}
    events[1]["tool_call"]["readToolCall"]["args"] = {"path": "second"}
    assert review_stdout_forbidden_tool_violations("\n".join(map(json.dumps, events)))


@pytest.mark.parametrize(
    ("location", "payload"),
    [
        ("event", {"name": "Task", "input": {"command": "id"}}),
        ("event", {"spawnError": "ran"}),
        ("wrapper", {"name": "Task", "input": {"command": "id"}}),
        ("result", {"success": {"content": "ok"}, "failure": {"command": "id"}}),
    ],
)
def test_review_rejects_unknown_structured_execution_envelopes(
    location: str, payload: dict[str, object]
) -> None:
    events = [
        json.loads(line)
        for line in _native_tool_pair("readToolCall", {"success": {}}).splitlines()
    ]
    if location == "event":
        events[1]["model_call_id"] = payload
    elif location == "wrapper":
        events[1]["tool_call"]["hookAdditionalContexts"] = [payload]
    else:
        events[1]["tool_call"]["readToolCall"]["result"] = payload
    assert review_stdout_forbidden_tool_violations("\n".join(map(json.dumps, events)))


def test_review_read_string_content_is_inert() -> None:
    text = 'A source file can say {"name":"Task","input":{"command":"id"}}.'
    assert (
        review_stdout_forbidden_tool_violations(
            _native_tool_pair("readToolCall", {"success": {"content": text}})
        )
        == []
    )


def test_review_forbidden_tool_call_rejects_unknown_completed_tool() -> None:
    stdout = json.dumps(
        {"type": "tool_call", "subtype": "completed", "tool_call": {"name": "Task"}}
    )
    assert review_stdout_forbidden_tool_violations(stdout)


def _native_tool_pair(variant: str, result: dict[str, object]) -> str:
    call_id = "native-call"
    start = {
        "type": "tool_call",
        "subtype": "started",
        "call_id": call_id,
        "tool_call": {"toolCallId": call_id, variant: {"args": {}}},
    }
    complete = {
        "type": "tool_call",
        "subtype": "completed",
        "call_id": call_id,
        "tool_call": {"toolCallId": call_id, variant: {"args": {}, "result": result}},
    }
    return json.dumps(start) + "\n" + json.dumps(complete)


@pytest.mark.parametrize("variant", ["readToolCall", "grepToolCall", "globToolCall"])
def test_review_native_read_only_tool_pair_is_parsed(variant: str) -> None:
    assert (
        review_stdout_forbidden_tool_violations(_native_tool_pair(variant, {"success": {}})) == []
    )


@pytest.mark.parametrize(
    ("variant", "result"),
    [
        ("getMcpToolsToolCall", {"success": {"content": ""}}),
        ("shellToolCall", {"permissionDenied": {}}),
    ],
)
def test_review_native_forbidden_attempt_fails(variant: str, result: dict[str, object]) -> None:
    assert review_stdout_forbidden_tool_violations(_native_tool_pair(variant, result))


@pytest.mark.parametrize(
    "tool_name",
    ["Write", "Edit", "Delete", "WebFetch", "Mcp"],
)
def test_review_forbidden_tool_names(tool_name: str) -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "session_id": "s"}),
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "tool_call": {"name": tool_name, "args": {}},
                }
            ),
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
    assert review_stdout_forbidden_tool_violations(stdout)


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


def test_trusted_vendor_rejects_copied_wrapper_in_vendor_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator"
    version_dir = operator_home / ".local/share/cursor-agent/versions/2026.01.01"
    version_dir.mkdir(parents=True)
    copied = version_dir / "cursor-agent"
    copied.write_text("#!/bin/sh\nexec motion-cursor-agent\n", encoding="utf-8")
    copied.chmod(0o755)
    wrapper = operator_home / "motion-cursor-agent"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_FACTORY_OPERATOR_HOME", str(operator_home))
    monkeypatch.setenv("GATE_A_REAL_CURSOR_EXECUTABLE", str(wrapper))
    assert trusted_cursor_vendor_binary() is None


def test_trusted_vendor_accepts_installed_bash_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator"
    version_dir = operator_home / ".local/share/cursor-agent/versions/2026.10.01-test"
    version_dir.mkdir(parents=True)
    launcher = version_dir / "cursor-agent"
    installed_versions = Path.home() / ".local/share/cursor-agent/versions"
    source = next(
        (
            path
            for path in installed_versions.glob("*/cursor-agent")
            if hashlib.sha256(path.read_bytes()).hexdigest()
            == "2ccc9a8e167797641448b5e5c936f006ba137a2555f117f38c5eb76a5238a233"
        ),
        None,
    )
    if source is None:
        pytest.skip("pinned installed Cursor launcher unavailable")
    launcher.write_bytes(source.read_bytes())
    launcher.chmod(0o755)
    node = version_dir / "node"
    node.write_bytes(b"\x7fELF-test")
    node.chmod(0o755)
    (version_dir / "index.js").write_text("// test entry\n", encoding="utf-8")
    wrapper = operator_home / "motion-cursor-agent"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_FACTORY_OPERATOR_HOME", str(operator_home))
    monkeypatch.setenv("GATE_A_REAL_CURSOR_EXECUTABLE", str(wrapper))
    monkeypatch.delenv("CURSOR_AGENT_REAL_BIN", raising=False)
    assert trusted_cursor_vendor_binary() == str(launcher.resolve())


def test_trusted_vendor_explicit_invalid_override_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator"
    version_dir = operator_home / ".local/share/cursor-agent/versions/2026.01.01"
    version_dir.mkdir(parents=True)
    real_vendor = version_dir / "cursor-agent"
    real_vendor.write_bytes(b"\x7fELF-not-a-wrapper")
    real_vendor.chmod(0o755)
    bad_override = operator_home / "bad-bin"
    bad_override.write_text("#!/bin/sh\nexec motion-cursor-agent\n", encoding="utf-8")
    bad_override.chmod(0o755)
    wrapper = operator_home / "motion-cursor-agent"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_FACTORY_OPERATOR_HOME", str(operator_home))
    monkeypatch.setenv("GATE_A_REAL_CURSOR_EXECUTABLE", str(wrapper))
    monkeypatch.setenv("CURSOR_AGENT_REAL_BIN", str(bad_override))
    assert trusted_cursor_vendor_binary() is None


def test_trusted_vendor_rejects_non_shebang_exec_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator"
    version_dir = operator_home / ".local/share/cursor-agent/versions/2026.01.01"
    version_dir.mkdir(parents=True)
    fake_vendor = version_dir / "cursor-agent"
    fake_vendor.write_text("exec /usr/bin/true\n", encoding="utf-8")
    fake_vendor.chmod(0o755)
    wrapper = operator_home / "motion-cursor-agent"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_FACTORY_OPERATOR_HOME", str(operator_home))
    monkeypatch.setenv("GATE_A_REAL_CURSOR_EXECUTABLE", str(wrapper))
    monkeypatch.delenv("CURSOR_AGENT_REAL_BIN", raising=False)
    assert trusted_cursor_vendor_binary() is None
    monkeypatch.setenv("CURSOR_AGENT_REAL_BIN", str(fake_vendor))
    assert trusted_cursor_vendor_binary() is None


def test_trusted_vendor_rejects_reassigned_node_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator"
    version_dir = operator_home / ".local/share/cursor-agent/versions/2026.01.01"
    version_dir.mkdir(parents=True)
    launcher = version_dir / "cursor-agent"
    launcher.write_text(
        '#!/usr/bin/env bash\nNODE_BIN="$SCRIPT_DIR/node"\n'
        'NODE_BIN="/tmp/not-cursor"\n'
        'exec "$NODE_BIN" "$SCRIPT_DIR/index.js" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    node = version_dir / "node"
    node.write_bytes(b"\x7fELF-test")
    node.chmod(0o755)
    (version_dir / "index.js").write_text("// test entry\n", encoding="utf-8")
    wrapper = operator_home / "motion-cursor-agent"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_FACTORY_OPERATOR_HOME", str(operator_home))
    monkeypatch.setenv("GATE_A_REAL_CURSOR_EXECUTABLE", str(wrapper))
    monkeypatch.delenv("CURSOR_AGENT_REAL_BIN", raising=False)
    assert trusted_cursor_vendor_binary() is None


def test_trusted_motion_cursor_gcloud_prefers_fleet_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = tmp_path / "operator"
    fleet = operator / ".config" / "motion-fleet-gcloud"
    fleet.mkdir(parents=True)
    (operator / ".config" / "gcloud").mkdir(parents=True)
    monkeypatch.setenv("OMNIGENT_FACTORY_OPERATOR_HOME", str(operator))
    monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)
    assert trusted_motion_cursor_gcloud_config() == str(fleet.resolve())


def test_trusted_vendor_rejects_recursive_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    wrapper = operator_home / "motion-cursor-agent"
    wrapper.write_text("#!/bin/sh\nexec motion-cursor-agent\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_FACTORY_OPERATOR_HOME", str(operator_home))
    monkeypatch.setenv("GATE_A_REAL_CURSOR_EXECUTABLE", str(wrapper))
    monkeypatch.setenv("CURSOR_AGENT_REAL_BIN", str(wrapper))
    assert trusted_cursor_vendor_binary() is None


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
    monkeypatch.setattr(
        "dev.factory.gate_a_real.profile.trusted_cursor_vendor_binary", lambda: "/vendor/agent"
    )
    env = real_task_cursor_cli_env(
        cursor_config_dir=str(tmp_path / "cfg"), home_dir=str(tmp_path / "cli-home")
    )
    assert env["CLOUDSDK_CONFIG"] == str(fleet)
    assert env["HOME"] == str(tmp_path / "cli-home")
    assert "CURSOR_API_KEY" not in env


def test_real_task_env_rejects_missing_vendor_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "dev.factory.gate_a_real.profile.trusted_cursor_vendor_binary", lambda: None
    )
    with pytest.raises(ValueError, match="vendor binary pin"):
        real_task_cursor_cli_env(
            cursor_config_dir=str(tmp_path / "cfg"), home_dir=str(tmp_path / "cli-home")
        )


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


def test_run_real_task_gate_missing_vendor_pin_stops_before_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = load_real_task_spec(_write_spec(tmp_path, workspace))
    artifacts = tmp_path / "artifacts"

    class _FakeSandbox:
        def cleanup(self) -> None:
            return None

    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.prepare_gate_a_cursor_cli_sandbox",
        lambda _home: _FakeSandbox(),
    )
    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.resolve_cursor_executable",
        lambda: "/fake/motion-cursor-agent",
    )
    monkeypatch.setattr(
        "dev.factory.gate_a_real.profile.trusted_cursor_vendor_binary", lambda: None
    )
    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.discover_and_assert_zero_mcp_servers",
        lambda *a, **k: pytest.fail("preflight must not spawn without vendor pin"),
    )
    result = run_real_task_gate(spec, RealTaskRunOptions(artifacts_dir=artifacts))
    assert not result.receipt.ok
    assert any("vendor binary pin" in p for p in result.problems)
    assert json.loads((artifacts / "receipt.json").read_text(encoding="utf-8"))["ok"] is False


@pytest.mark.parametrize("remove_config", [False, True])
def test_run_real_task_gate_fails_when_builder_config_mutated_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remove_config: bool
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("ok", encoding="utf-8")
    spec = load_real_task_spec(_write_spec(tmp_path, workspace))
    artifacts = tmp_path / "artifacts"
    builder_config_dir: Path | None = None
    real_materialize = materialize_real_task_cursor_config_dir

    def tracking_materialize(target: Path) -> dict[str, object]:
        nonlocal builder_config_dir
        profile = real_materialize(target)
        builder_config_dir = Path(str(profile["cursor_config_dir"]))
        return profile

    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.materialize_real_task_cursor_config_dir",
        tracking_materialize,
    )

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
        if not is_review and builder_config_dir is not None:
            cli_config = builder_config_dir / "cli-config.json"
            if cli_config.is_file():
                if remove_config:
                    cli_config.unlink()
                else:
                    document = json.loads(cli_config.read_text(encoding="utf-8"))
                    perms = document.setdefault("permissions", {})
                    allow = list(perms.get("allow") or [])
                    allow.append("Edit(*)")
                    perms["allow"] = allow
                    cli_config.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        stdout = (
            _review_pass_stream()
            if is_review
            else (
                json.dumps({"type": "system", "session_id": "build-session"})
                + "\n"
                + json.dumps(
                    {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
                )
            )
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
            stdout_log_path=str(stdout_log),
            stderr_log_path=str(stderr_log),
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
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
    assert not result.receipt.ok
    assert any(
        "builder config hash drift" in p or "missing builder config fingerprint" in p
        for p in result.problems
    )
    receipt = json.loads((artifacts / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["ok"] is False


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
    review_profile = materialize_real_task_review_config_dir(tmp_path / "review-pin")
    (artifacts / "review.profile.json").write_text(
        json.dumps(review_profile["effective_config_hashes"], indent=2) + "\n",
        encoding="utf-8",
    )
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


def test_post_review_scan_failure_leaves_null_manifest_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("ok", encoding="utf-8")
    spec = load_real_task_spec(_write_spec(tmp_path, workspace))
    artifacts = tmp_path / "artifacts"
    call_count = 0

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
        stdout = (
            _review_pass_stream()
            if is_review
            else (
                json.dumps({"type": "system", "session_id": "build-session"})
                + "\n"
                + json.dumps(
                    {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
                )
            )
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
            stdout_log_path=str(stdout_log),
            stderr_log_path=str(stderr_log),
            stdout_sha256="e" * 64,
            stderr_sha256="f" * 64,
        )

    real_collect = collect_deliverables

    def flaky_collect(ws: Path, paths: tuple[str, ...]):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise ValueError("scan blew up")
        return real_collect(ws, paths)

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
    monkeypatch.setattr(
        "dev.factory.gate_a_real.orchestration.collect_deliverables",
        flaky_collect,
    )

    result = run_real_task_gate(spec, RealTaskRunOptions(artifacts_dir=artifacts))
    assert result.receipt.post_review_manifest_sha256 is None
    assert any("post-review deliverable scan failed" in p for p in result.problems)


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
