"""V7 Gate A HOME cursor inventory and sandbox deny checks."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from dev.factory.gate_a_trial.config_hashes import (
    cli_config_security_policy_fingerprint,
    compare_post_positive_gate_a_security,
    compare_post_warmup_gate_a_security,
    is_allowed_home_cursor_session_relpath,
    scan_forbidden_home_cursor_paths,
    security_config_fingerprints,
)
from dev.factory.gate_a_trial.cursor_cli_sandbox import (
    GateACursorCliSandboxError,
    build_gate_a_isolated_home_deny_profile,
    denied_home_trees_have_content,
    gate_a_denied_home_subpaths,
    prepare_gate_a_cursor_cli_sandbox,
    seatbelt_canonical_subpath,
)
from dev.factory.gate_a_trial.cursor_profile import materialize_cursor_config_dir
from dev.factory.gate_a_trial.stream_json import GateAPositiveMcpPayloadMode


def test_policy_fingerprint_ignores_auth_model_cache(tmp_path: Path) -> None:
    path = tmp_path / "cli-config.json"
    base = {
        "version": 1,
        "approvalMode": "allowlist",
        "permissions": {"allow": ["Mcp(x:y)"], "deny": ["Shell"]},
    }
    path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    first = cli_config_security_policy_fingerprint(path)
    enriched = {**base, "authInfo": {"email": "a@b.c"}, "model": {"modelId": "composer-2.5"}}
    path.write_text(json.dumps(enriched) + "\n", encoding="utf-8")
    assert cli_config_security_policy_fingerprint(path) == first


def test_policy_fingerprint_detects_shell_allow_mutation(tmp_path: Path) -> None:
    path = tmp_path / "cli-config.json"
    path.write_text(
        json.dumps(
            {
                "approvalMode": "allowlist",
                "permissions": {"allow": [], "deny": ["Shell"]},
            },
        )
        + "\n",
        encoding="utf-8",
    )
    before = cli_config_security_policy_fingerprint(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["permissions"]["allow"] = ["Shell"]
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    assert cli_config_security_policy_fingerprint(path) != before


def test_scan_forbidden_rejects_plugin_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin_file = home / ".cursor" / "plugins" / "cache" / "evil" / "run.js"
    plugin_file.parent.mkdir(parents=True)
    plugin_file.write_text("console.log(1)\n", encoding="utf-8")
    problems = scan_forbidden_home_cursor_paths(home)
    assert any("forbidden home cursor path" in p and "plugins" in p for p in problems)


def test_allowed_session_transcript_shape() -> None:
    uid = "59ceca6c-8e20-4b9e-9f7e-c6a9d46d512e"
    rel = f".cursor/projects/slug/agent-transcripts/{uid}/{uid}.jsonl"
    assert is_allowed_home_cursor_session_relpath(rel)
    assert not is_allowed_home_cursor_session_relpath(
        f".cursor/projects/slug/agent-transcripts/{uid}/other.jsonl",
    )


def test_malformed_transcript_path_rejected() -> None:
    assert not is_allowed_home_cursor_session_relpath(
        ".cursor/projects/slug/agent-transcripts/not-a-uuid/x.jsonl",
    )


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS /var symlink resolution")
def test_seatbelt_denied_subpath_resolves_private_var(tmp_path: Path) -> None:
    resolved = tmp_path.resolve().as_posix()
    if not resolved.startswith("/private/var/"):
        pytest.skip("runner temp not under /var")
    symlink_form = Path("/" + resolved.removeprefix("/private/"))
    assert seatbelt_canonical_subpath(symlink_form) == resolved


def test_sandbox_profile_uses_canonical_denied_subpaths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    profile = build_gate_a_isolated_home_deny_profile(home)
    for subpath in gate_a_denied_home_subpaths(home):
        assert f'(subpath "{subpath}")' in profile


def test_denied_home_trees_have_content_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skill = home / ".cursor" / "skills-cursor" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("x", encoding="utf-8")
    problems = denied_home_trees_have_content(home)
    assert problems


def test_prepare_sandbox_unavailable_fails_closed() -> None:
    with patch("dev.factory.gate_a_trial.cursor_cli_sandbox.shutil.which", return_value=None):
        with pytest.raises(GateACursorCliSandboxError, match="sandbox-exec missing"):
            prepare_gate_a_cursor_cli_sandbox(Path("/tmp/home"))


def _write_minimal_gate_a_cli_config(cli: Path) -> None:
    cli.write_text(
        json.dumps(
            {
                "version": 1,
                "editor": {"vimMode": False},
                "approvalMode": "allowlist",
                "permissions": {
                    "allow": ["Mcp(gate-a:factory_gate_a_tool)"],
                    "deny": ["Shell", "Write"],
                },
            },
        )
        + "\n",
        encoding="utf-8",
    )


def test_compare_post_warmup_allows_absent_to_both_steering_true(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wmc = workspace / ".cursor" / "mcp.json"
    wmc.parent.mkdir(parents=True)
    wmc.write_text("{}\n", encoding="utf-8")
    cli = config_dir / "cli-config.json"
    _write_minimal_gate_a_cli_config(cli)
    (config_dir / "mcp.json").write_text("{}\n", encoding="utf-8")
    post_discovery = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["steering"] = True
    data["rewind"] = True
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    assert not compare_post_warmup_gate_a_security(
        post_discovery,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )


@pytest.mark.parametrize(
    ("steering", "rewind"),
    [
        (False, True),
        (True, False),
        ("true", "true"),
        ({"enabled": True}, {"enabled": True}),
        (True, None),
    ],
)
def test_compare_post_warmup_rejects_invalid_steering_init(
    tmp_path: Path,
    steering: object,
    rewind: object,
) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wmc = workspace / ".cursor" / "mcp.json"
    wmc.parent.mkdir(parents=True)
    wmc.write_text("{}\n", encoding="utf-8")
    cli = config_dir / "cli-config.json"
    _write_minimal_gate_a_cli_config(cli)
    (config_dir / "mcp.json").write_text("{}\n", encoding="utf-8")
    post_discovery = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    data = json.loads(cli.read_text(encoding="utf-8"))
    if steering is not None:
        data["steering"] = steering
    if rewind is not None:
        data["rewind"] = rewind
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    problems = compare_post_warmup_gate_a_security(
        post_discovery,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    assert any("steering" in p for p in problems)


def test_compare_post_warmup_rejects_preexisting_steering_mutation(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wmc = workspace / ".cursor" / "mcp.json"
    wmc.parent.mkdir(parents=True)
    wmc.write_text("{}\n", encoding="utf-8")
    cli = config_dir / "cli-config.json"
    _write_minimal_gate_a_cli_config(cli)
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["steering"] = False
    data["rewind"] = False
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    (config_dir / "mcp.json").write_text("{}\n", encoding="utf-8")
    post_discovery = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    data["steering"] = True
    data["rewind"] = True
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    problems = compare_post_warmup_gate_a_security(
        post_discovery,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    assert any("steering" in p for p in problems)


def test_compare_post_positive_rejects_steering_drift_from_warmup_baseline(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wmc = workspace / ".cursor" / "mcp.json"
    wmc.parent.mkdir(parents=True)
    wmc.write_text("{}\n", encoding="utf-8")
    cli = config_dir / "cli-config.json"
    _write_minimal_gate_a_cli_config(cli)
    (config_dir / "mcp.json").write_text("{}\n", encoding="utf-8")
    post_discovery = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["steering"] = True
    data["rewind"] = True
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    assert not compare_post_warmup_gate_a_security(
        post_discovery,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    post_warmup = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    data["steering"] = False
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    problems = compare_post_positive_gate_a_security(
        post_warmup,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
        home_dir=home,
    )
    assert any("steering" in p for p in problems)


def test_compare_post_positive_rejects_steering_change(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=workspace,
    )
    baseline = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
    )
    cli = config_dir / "cli-config.json"
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["steering"] = not bool(data.get("steering", False))
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    problems = compare_post_positive_gate_a_security(
        baseline,
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
        home_dir=home,
    )
    assert any("steering" in p for p in problems)


def test_legacy_v34_payload_mode_constant() -> None:
    assert GateAPositiveMcpPayloadMode.LEGACY_V34.value == "legacy_v34"


def test_headless_argv_omits_unsupported_setting_sources() -> None:
    from unittest.mock import MagicMock, patch

    from dev.factory.gate_a_trial.orchestration import run_headless_stream_json

    with patch("dev.factory.gate_a_trial.orchestration.run_in_new_session") as run:
        run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            error=None,
        )
        run_headless_stream_json(
            "agent",
            "hello",
            workspace=Path("/ws"),
            env={},
            timeout_seconds=1.0,
        )
        argv = run.call_args[0][0]
        assert argv[1] == "--print"
        assert "--setting-sources" not in argv


def _warmup_stream_stdout(*, tool_line: str | None = None) -> str:
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "composer-2.5",
                "apiKeySource": "env",
            },
        ),
    ]
    if tool_line is not None:
        lines.append(tool_line)
    lines.append(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "OK",
            },
        ),
    )
    return "\n".join(lines)


def test_no_tool_warmup_rejects_parsed_tool_call() -> None:
    from dev.factory.gate_a_trial.orchestration import validate_no_tool_warmup_stream_result

    tool_line = json.dumps(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "c1",
            "tool_call": {
                "shellToolCall": {
                    "args": {"command": "true"},
                    "result": {"rejected": {"reason": "allowlist"}},
                },
            },
        },
    )
    stream = {
        "stdout": _warmup_stream_stdout(tool_line=tool_line),
        "stream_init_acceptable": True,
        "stream_final_result_acceptable": True,
        "returncode": 0,
        "parsed_tool_calls": [{"name": "Shell", "status": "completed", "mcp_server": None}],
    }
    problems = validate_no_tool_warmup_stream_result(stream)
    assert any("tool call" in p for p in problems)


def test_no_tool_warmup_rejects_missing_final_result() -> None:
    from dev.factory.gate_a_trial.orchestration import validate_no_tool_warmup_stream_result

    stdout = json.dumps(
        {"type": "system", "subtype": "init", "model": "x", "apiKeySource": "env"},
    )
    problems = validate_no_tool_warmup_stream_result(
        {
            "stdout": stdout,
            "stream_init_acceptable": True,
            "returncode": 0,
            "parsed_tool_calls": [],
        },
    )
    assert any("final result" in p for p in problems)


def test_no_tool_warmup_accepts_clean_stream() -> None:
    from dev.factory.gate_a_trial.orchestration import validate_no_tool_warmup_stream_result

    assert not validate_no_tool_warmup_stream_result(
        {
            "stdout": _warmup_stream_stdout(),
            "stream_init_acceptable": True,
            "stream_final_result_acceptable": True,
            "returncode": 0,
            "parsed_tool_calls": [],
        },
    )


def test_post_positive_baseline_is_post_warmup_not_discovery(tmp_path: Path) -> None:
    """Regression: model/authInfo added during warmup must not fail post-positive drift."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wmc = workspace / ".cursor" / "mcp.json"
    wmc.parent.mkdir(parents=True)
    wmc.write_text("{}\n", encoding="utf-8")
    cli = config_dir / "cli-config.json"
    pre_warmup = {
        "version": 1,
        "editor": {"vimMode": False},
        "approvalMode": "allowlist",
        "permissions": {"allow": ["Mcp(gate-a:factory_gate_a_tool)"], "deny": ["Shell", "Write"]},
    }
    cli.write_text(json.dumps(pre_warmup) + "\n", encoding="utf-8")
    (config_dir / "mcp.json").write_text("{}\n", encoding="utf-8")

    post_discovery = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    post_warmup_body = {
        **pre_warmup,
        "model": {"modelId": "composer-2.5"},
        "authInfo": {"email": "operator@example.com"},
    }
    cli.write_text(json.dumps(post_warmup_body) + "\n", encoding="utf-8")
    post_warmup = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    home = tmp_path / "home"
    home.mkdir()

    assert not compare_post_warmup_gate_a_security(
        post_discovery,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
    )
    wrong_baseline_problems = compare_post_positive_gate_a_security(
        post_discovery,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
        home_dir=home,
    )
    assert any("top-level key set changed" in p for p in wrong_baseline_problems)

    assert not compare_post_positive_gate_a_security(
        post_warmup,
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc,
        home_dir=home,
    )


def test_compare_post_warmup_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=workspace,
    )
    baseline = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
    )
    cli = config_dir / "cli-config.json"
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["evilOperatorBackdoor"] = True
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    problems = compare_post_warmup_gate_a_security(
        baseline,
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
    )
    assert any("unexpected top-level keys" in p for p in problems)


def test_compare_post_positive_rejects_policy_drift_from_warmup_baseline(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=workspace,
    )
    post_warmup = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
    )
    cli = config_dir / "cli-config.json"
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["permissions"]["deny"] = []
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    problems = compare_post_positive_gate_a_security(
        post_warmup,
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
        home_dir=home,
    )
    assert any("drift" in p for p in problems)


def test_compare_post_warmup_rejects_policy_mutation(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=workspace,
    )
    baseline = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
    )
    cli = config_dir / "cli-config.json"
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["permissions"]["allow"] = ["Shell"]
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    problems = compare_post_warmup_gate_a_security(
        baseline,
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
    )
    assert any("drift" in p for p in problems)


def test_compare_post_positive_rejects_new_top_level_key_after_positive(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=workspace,
    )
    post_warmup = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
    )
    cli = config_dir / "cli-config.json"
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["newRuntimeKey"] = {"x": 1}
    cli.write_text(json.dumps(data) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    problems = compare_post_positive_gate_a_security(
        post_warmup,
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace / ".cursor" / "mcp.json",
        home_dir=home,
    )
    assert any(
        "top-level key set changed" in p or "unexpected top-level keys" in p for p in problems
    )


def _write_project_mcp_approvals(project_dir: Path, digest: str) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "mcp-approvals.json").write_text(
        json.dumps([digest]) + "\n",
        encoding="utf-8",
    )


def test_v11_warmup_pins_and_positive_accepts_repo_json(tmp_path: Path) -> None:
    from dev.factory.gate_a_trial.project_files import (
        REPO_JSON_BASENAME,
        establish_project_files_baseline_after_warmup,
        expected_cursor_project_slug,
    )

    workspace = tmp_path / "trial-ws"
    workspace.mkdir()
    home = tmp_path / "home"
    slug = expected_cursor_project_slug(workspace)
    digest = "B" * 44
    _write_project_mcp_approvals(home / ".cursor" / "projects" / slug, digest)

    baseline, problems = establish_project_files_baseline_after_warmup(home, workspace)
    assert not problems
    assert baseline is not None

    repo_id = "59ceca6c-8e20-4b9e-9f7e-c6a9d46d512e"
    (home / ".cursor" / "projects" / slug / REPO_JSON_BASENAME).write_text(
        json.dumps({"id": repo_id}) + "\n",
        encoding="utf-8",
    )
    pins = {**baseline.as_fingerprint_map()}
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    _write_minimal_gate_a_cli_config(config_dir / "cli-config.json")
    (config_dir / "mcp.json").write_text("{}\n", encoding="utf-8")
    workspace_mcp = workspace / ".cursor" / "mcp.json"
    workspace_mcp.parent.mkdir(parents=True)
    workspace_mcp.write_text("{}\n", encoding="utf-8")
    post_warmup = security_config_fingerprints(
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace_mcp,
    )
    post_warmup.update(pins)
    assert not compare_post_positive_gate_a_security(
        post_warmup,
        cursor_config_dir=config_dir,
        workspace_mcp_config=workspace_mcp,
        home_dir=home,
        trial_workspace=workspace,
    )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda home, ws, slug: (
            home / ".cursor" / "projects" / slug / "mcp-approvals.json"
        ).write_text(
            json.dumps(["C" * 44]) + "\n",
            encoding="utf-8",
        ),
        lambda home, ws, slug: (
            home / ".cursor" / "projects" / slug / "mcp-approvals.json"
        ).unlink(),
        lambda home, ws, slug: _write_project_mcp_approvals(
            home / ".cursor" / "projects" / f"{slug}-extra",
            "D" * 44,
        ),
    ],
)
def test_v11_positive_rejects_approval_digest_or_extra_project_dir(
    tmp_path: Path,
    mutator: object,
) -> None:
    from dev.factory.gate_a_trial.project_files import (
        establish_project_files_baseline_after_warmup,
        expected_cursor_project_slug,
        validate_project_files_after_positive,
    )

    workspace = tmp_path / "trial-ws"
    workspace.mkdir()
    home = tmp_path / "home"
    slug = expected_cursor_project_slug(workspace)
    _write_project_mcp_approvals(home / ".cursor" / "projects" / slug, "E" * 44)
    baseline, _ = establish_project_files_baseline_after_warmup(home, workspace)
    assert baseline is not None
    mutator(home, workspace, slug)
    problems = validate_project_files_after_positive(home, workspace, baseline)
    assert problems


def test_v11_rejects_malformed_repo_json_and_extra_keys(tmp_path: Path) -> None:
    from dev.factory.gate_a_trial.project_files import (
        REPO_JSON_BASENAME,
        establish_project_files_baseline_after_warmup,
        expected_cursor_project_slug,
        validate_project_files_after_positive,
    )

    workspace = tmp_path / "trial-ws"
    workspace.mkdir()
    home = tmp_path / "home"
    slug = expected_cursor_project_slug(workspace)
    _write_project_mcp_approvals(home / ".cursor" / "projects" / slug, "F" * 44)
    baseline, _ = establish_project_files_baseline_after_warmup(home, workspace)
    assert baseline is not None
    repo_path = home / ".cursor" / "projects" / slug / REPO_JSON_BASENAME
    repo_path.write_text(json.dumps({"id": "not-a-uuid", "extra": 1}) + "\n", encoding="utf-8")
    problems = validate_project_files_after_positive(home, workspace, baseline)
    assert any("repo.json" in p for p in problems)


def test_v11_rejects_symlink_project_mcp_approvals(tmp_path: Path) -> None:
    from dev.factory.gate_a_trial.project_files import (
        establish_project_files_baseline_after_warmup,
        expected_cursor_project_slug,
    )

    workspace = tmp_path / "trial-ws"
    workspace.mkdir()
    home = tmp_path / "home"
    slug = expected_cursor_project_slug(workspace)
    project_dir = home / ".cursor" / "projects" / slug
    project_dir.mkdir(parents=True)
    real = tmp_path / "real-approvals.json"
    real.write_text(json.dumps(["G" * 44]) + "\n", encoding="utf-8")
    (project_dir / "mcp-approvals.json").symlink_to(real)
    _, problems = establish_project_files_baseline_after_warmup(home, workspace)
    assert any("symlink" in p for p in problems)


def test_scan_forbidden_rejects_generic_home_mcp_approvals(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp-approvals.json").write_text("[]\n", encoding="utf-8")
    problems = scan_forbidden_home_cursor_paths(home)
    assert any("forbidden home cursor path" in p and "mcp-approvals" in p for p in problems)


def test_v12_cursor_project_slug_normalizes_mac_temp_underscores() -> None:
    from dev.factory.gate_a_trial.project_files import expected_cursor_project_slug
    from omnigent.harnesses.cursor_native.bridge import cursor_project_key

    workspace = Path(
        "/fixture/synthetic-gate-a-v12-mac-temp-shape/var/folders/"
        "demo_user_tmp_abcdef0123456789/T/"
        "omnigent-gate-a-trial-home/workspaces/omnigent-gate-a-mcp-demo01ab2",
    )
    slug = expected_cursor_project_slug(workspace)
    assert slug == (
        "fixture-synthetic-gate-a-v12-mac-temp-shape-var-folders-demo-user-tmp-"
        "abcdef0123456789-T-"
        "omnigent-gate-a-trial-home-workspaces-omnigent-gate-a-mcp-demo01ab2"
    )
    assert slug != cursor_project_key(workspace)


def test_v12_warmup_rejects_legacy_underscore_project_dir(tmp_path: Path) -> None:
    from dev.factory.gate_a_trial.project_files import (
        establish_project_files_baseline_after_warmup,
        expected_cursor_project_slug,
    )
    from omnigent.harnesses.cursor_native.bridge import cursor_project_key

    workspace = Path(
        "/fixture/synthetic-gate-a-v12-mac-temp-shape/var/folders/"
        "demo_user_tmp_abcdef0123456789/T/"
        "omnigent-gate-a-trial-home/workspaces/omnigent-gate-a-mcp-demo01ab2",
    )
    home = tmp_path / "home"
    legacy_slug = cursor_project_key(workspace)
    cli_slug = expected_cursor_project_slug(workspace)
    assert legacy_slug != cli_slug
    _write_project_mcp_approvals(home / ".cursor" / "projects" / legacy_slug, "H" * 44)

    baseline, problems = establish_project_files_baseline_after_warmup(home, workspace)
    assert baseline is None
    assert any("expected exactly one cursor project directory" in p for p in problems)
    assert cli_slug in problems[0]
