"""Gate A MCP discovery parsing and optional live ``agent mcp`` probe (no API key)."""

from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_trial.cursor_profile import (
    materialize_cursor_config_dir,
    resolve_trial_cursor_executable,
)
from dev.factory.gate_a_trial.cursor_cli_sandbox import (
    GateACursorCliSandbox,
    prepare_gate_a_cursor_cli_sandbox,
)
from dev.factory.gate_a_trial.mcp_discovery import (
    _run_mcp_cli,
    discover_and_gate_gate_a_mcp,
    mcp_server_listing_usable,
    parse_mcp_list_server_names,
    parse_mcp_list_server_status,
    parse_mcp_list_tool_names,
    validate_gate_a_discovery,
    validate_zero_mcp_servers_discovery,
)
from dev.factory.gate_a_trial.trial_env import (
    materialize_isolated_home,
    pre_enable_home_must_be_pristine,
    prove_isolated_home_empty,
    sanitized_cursor_cli_env,
)
from dev.factory.gate_a_trial.workspace_layout import (
    dispose_trial_workspace,
    materialize_disposable_trial_workspace,
)


def _fake_gate_a_sandbox(home: Path) -> GateACursorCliSandbox:
    profile = home / "gate-a-fake.sb"
    profile.write_text("(version 1)\n(allow default)\n", encoding="utf-8")
    return GateACursorCliSandbox(
        profile_path=profile,
        profile_sha256="0" * 64,
        home_dir=home,
    )


def test_parse_mcp_list_servers_and_tools() -> None:
    listing = f"{MCP_SERVER_NAME}: not loaded (needs approval)\n"
    post_enable = f"{MCP_SERVER_NAME}: ready\n"
    tools_out = f"Tools for {MCP_SERVER_NAME} (1):\n- {TOOL_NAME} ()\n"
    assert parse_mcp_list_server_names(listing) == [MCP_SERVER_NAME]
    assert parse_mcp_list_tool_names(tools_out) == [TOOL_NAME]
    assert mcp_server_listing_usable(parse_mcp_list_server_status(post_enable, MCP_SERVER_NAME))
    gate = validate_gate_a_discovery(
        list_stdout=listing,
        list_tools_stdout=tools_out,
        list_returncode=0,
        list_tools_returncode=0,
        enable_returncode=0,
        post_enable_list_stdout=post_enable,
        post_enable_list_returncode=0,
    )
    assert gate["gate_passed"] is True


@pytest.mark.parametrize(
    ("list_rc", "enable_rc", "list_tools_rc", "post_enable_rc"),
    [
        (1, 0, 0, 0),
        (0, 1, 0, 0),
        (0, 0, 1, 0),
        (0, 0, 0, 1),
    ],
)
def test_validate_gate_fails_on_nonzero_exit_despite_parseable_stdout(
    list_rc: int,
    enable_rc: int,
    list_tools_rc: int,
    post_enable_rc: int,
) -> None:
    listing = f"{MCP_SERVER_NAME}: not loaded (needs approval)\n"
    post_enable = f"{MCP_SERVER_NAME}: ready\n"
    tools_out = f"Tools for {MCP_SERVER_NAME} (1):\n- {TOOL_NAME} ()\n"
    gate = validate_gate_a_discovery(
        list_stdout=listing,
        list_tools_stdout=tools_out,
        list_returncode=list_rc,
        list_tools_returncode=list_tools_rc,
        enable_returncode=enable_rc,
        post_enable_list_stdout=post_enable,
        post_enable_list_returncode=post_enable_rc,
    )
    assert gate["gate_passed"] is False


def test_validate_gate_fails_when_post_enable_still_needs_approval() -> None:
    listing = f"{MCP_SERVER_NAME}: not loaded (needs approval)\n"
    tools_out = f"Tools for {MCP_SERVER_NAME} (1):\n- {TOOL_NAME} ()\n"
    gate = validate_gate_a_discovery(
        list_stdout=listing,
        list_tools_stdout=tools_out,
        list_returncode=0,
        list_tools_returncode=0,
        enable_returncode=0,
        post_enable_list_stdout=listing,
        post_enable_list_returncode=0,
    )
    assert gate["gate_passed"] is False
    assert any("ready" in r for r in gate["gate_failure_reasons"])


def test_validate_zero_mcp_servers_passes_on_empty_listing() -> None:
    empty = "No MCP servers configured (expected in .cursor/mcp.json or ~/.cursor/mcp.json)\n"
    gate = validate_zero_mcp_servers_discovery(list_stdout=empty, list_returncode=0)
    assert gate["gate_passed"] is True
    assert gate["configured_servers"] == []


def test_validate_zero_mcp_servers_fails_when_server_remains() -> None:
    listing = f"{MCP_SERVER_NAME}: loaded\n"
    gate = validate_zero_mcp_servers_discovery(list_stdout=listing, list_returncode=0)
    assert gate["gate_passed"] is False


def test_materialize_isolated_home_unique_and_no_cursor_state(tmp_path: Path) -> None:
    parent = tmp_path / "homes"
    first = materialize_isolated_home(parent)
    second = materialize_isolated_home(parent)
    assert first != second
    proof = prove_isolated_home_empty(second)
    assert proof["cursor_state_absent"] is True
    assert pre_enable_home_must_be_pristine(second) == []


def test_materialize_does_not_reuse_legacy_fixed_home_path(tmp_path: Path) -> None:
    parent = tmp_path / "homes"
    legacy = parent / "home"
    legacy.mkdir(parents=True)
    stale = legacy / ".cursor"
    stale.mkdir()
    (stale / "mcp-approvals.json").write_text('{"stale": true}\n', encoding="utf-8")
    fresh = materialize_isolated_home(parent)
    assert fresh != legacy
    assert prove_isolated_home_empty(fresh)["cursor_state_absent"] is True
    assert "mcp-approvals.json" not in prove_isolated_home_empty(fresh)["inventory_paths"]


def test_discover_gate_fails_before_mcp_cli_when_home_not_pristine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "stale-home"
    home.mkdir()
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "mcp-approvals.json").write_text('{"from": "prior-run"}\n', encoding="utf-8")
    config_dir = tmp_path / "cursor-config"
    ws = tmp_path / "ws"
    ws.mkdir()
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=ws,
    )
    env = sanitized_cursor_cli_env(cursor_config_dir=str(config_dir), home_dir=str(home))

    def _must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("mcp CLI must not run when HOME inventory is not pristine")

    monkeypatch.setattr(
        "dev.factory.gate_a_trial.mcp_discovery._run_mcp_cli",
        _must_not_run,
    )
    discovery = discover_and_gate_gate_a_mcp(
        sys.executable,
        str(ws),
        env,
        cursor_config_dir=str(config_dir),
        workspace_mcp_config=str(ws / ".cursor" / "mcp.json"),
    )
    assert discovery.get("gate_passed") is False
    assert any("mcp-approvals" in str(r) for r in discovery.get("gate_failure_reasons") or [])


def test_pre_enable_home_rejects_inherited_mcp_approvals(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cursor = home / ".cursor"
    cursor.mkdir()
    (cursor / "mcp-approvals.json").write_text("{}", encoding="utf-8")
    reasons = pre_enable_home_must_be_pristine(home)
    assert reasons
    assert any("mcp-approvals" in r for r in reasons)


def test_parse_mcp_list_no_servers_fails_gate() -> None:
    empty = "No MCP servers configured (expected in .cursor/mcp.json or ~/.cursor/mcp.json)\n"
    gate = validate_gate_a_discovery(
        list_stdout=empty,
        list_tools_stdout="",
        list_returncode=0,
        list_tools_returncode=1,
    )
    assert gate["gate_passed"] is False


def test_disposable_workspace_is_git_root(tmp_path: Path) -> None:
    ws = materialize_disposable_trial_workspace(parent=tmp_path)
    try:
        assert (ws / ".git").exists()
        materialize_cursor_config_dir(
            tmp_path / "cfg",
            cursor_executable=sys.executable,
            python_executable=sys.executable,
            workspace=ws,
        )
        assert (ws / ".cursor" / "mcp.json").is_file()
        data = json.loads((ws / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        assert set(data["mcpServers"]) == {MCP_SERVER_NAME}
    finally:
        dispose_trial_workspace(ws)


def test_run_mcp_cli_wraps_discovery_argv_with_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    sandbox = _fake_gate_a_sandbox(home)
    captured: list[list[str]] = []

    def _fake_run(argv: list[str], **_kwargs: object) -> object:
        captured.append(list(argv))
        from dev.factory.gate_a_trial.subprocess_session import SubprocessResult

        return SubprocessResult(argv=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "dev.factory.gate_a_trial.mcp_discovery.run_in_new_session",
        _fake_run,
    )
    for args in (("list",), ("enable", MCP_SERVER_NAME), ("list",), ("list-tools", MCP_SERVER_NAME)):
        _run_mcp_cli(
            "agent",
            str(tmp_path / "ws"),
            {"HOME": str(home)},
            *args,
            gate_a_sandbox=sandbox,
        )
    assert len(captured) == 4
    for argv in captured:
        assert argv[:3] == ["sandbox-exec", "-f", str(sandbox.profile_path)]
        assert argv[3] == "agent"
        assert argv[4] == "mcp"
        assert "--setting-sources" not in argv
    sandbox.cleanup()


def test_discover_gate_fails_closed_when_sandbox_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_dir = tmp_path / "cursor-config"
    ws = tmp_path / "ws"
    ws.mkdir()
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=ws,
    )
    env = sanitized_cursor_cli_env(cursor_config_dir=str(config_dir), home_dir=str(home))
    discovery = discover_and_gate_gate_a_mcp(
        sys.executable,
        str(ws),
        env,
        cursor_config_dir=str(config_dir),
        workspace_mcp_config=str(ws / ".cursor" / "mcp.json"),
        gate_a_sandbox=None,
    )
    assert discovery.get("gate_passed") is False
    assert any("gate_a_sandbox" in str(r) for r in discovery.get("gate_failure_reasons") or [])


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="macOS sandbox-exec required",
)
@pytest.mark.skipif(
    shutil.which("agent") is None and not Path.home().joinpath(".local/bin/agent").is_file(),
    reason="Cursor agent CLI not installed",
)
def test_unsandboxed_agent_mcp_list_hydrates_compile_cache_guard(tmp_path: Path) -> None:
    """Regression guard: bare ``agent mcp list`` writes compile-cache; sandboxed does not."""
    cursor = resolve_trial_cursor_executable()
    config_dir = tmp_path / "cursor-config"
    ws = materialize_disposable_trial_workspace(parent=tmp_path / "workspaces")
    try:
        materialize_cursor_config_dir(
            config_dir,
            cursor_executable=cursor,
            python_executable=sys.executable,
            workspace=ws,
        )
        home = materialize_isolated_home(tmp_path / "home")
        env = sanitized_cursor_cli_env(cursor_config_dir=str(config_dir), home_dir=str(home))
        cache_root = home / "Library" / "Caches" / "cursor-compile-cache"

        _run_mcp_cli(cursor, str(ws), env, "list", gate_a_sandbox=None)
        unsandboxed_files = list(cache_root.rglob("*")) if cache_root.exists() else []
        unsandboxed_count = sum(1 for p in unsandboxed_files if p.is_file())
        assert unsandboxed_count > 0, "expected unsandboxed discovery to hydrate compile-cache"

        home2 = materialize_isolated_home(tmp_path / "home2")
        env2 = sanitized_cursor_cli_env(cursor_config_dir=str(config_dir), home_dir=str(home2))
        sandbox = prepare_gate_a_cursor_cli_sandbox(home2)
        try:
            _run_mcp_cli(cursor, str(ws), env2, "list", gate_a_sandbox=sandbox)
        finally:
            sandbox.cleanup()
        cache2 = home2 / "Library" / "Caches" / "cursor-compile-cache"
        sandboxed_count = sum(1 for p in cache2.rglob("*") if p.is_file()) if cache2.exists() else 0
        assert sandboxed_count == 0
    finally:
        dispose_trial_workspace(ws)


def test_orchestration_cleans_sandbox_profile_when_discovery_fails(tmp_path: Path) -> None:
    from contextlib import ExitStack
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from dev.factory.gate_a_trial.config_hashes import MANDATORY_EFFECTIVE_CONFIG_KEYS
    from dev.factory.gate_a_trial.orchestration import ProfileBundle, run_admitted_gate_a_turn
    from dev.factory.order_scoped.binding import INTERNAL_STAGE_BRIEF_HASH, INTERNAL_STAGE_ORDER_ID

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".cursor").mkdir()
    (workspace / ".cursor" / "mcp.json").write_text("{}\n", encoding="utf-8")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    home = tmp_path / "isolated-home"
    home.mkdir()
    profile_paths: list[Path] = []

    def _capture_sandbox(isolated: Path) -> GateACursorCliSandbox:
        sandbox = _fake_gate_a_sandbox(isolated)
        profile_paths.append(sandbox.profile_path)
        return sandbox

    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}
    bundle = ProfileBundle(
        cursor_executable="/usr/bin/false",
        cursor_config_dir=config_dir,
        workspace=workspace,
        home_dir=str(home),
        mcp_server_home=str(home / "mcp-stdio-server-home"),
        profile={"workspace_mcp_config": str(workspace / ".cursor" / "mcp.json")},
        pre_enable_config_hashes=mandatory,
        discovery_env={},
        model_env={},
    )

    class _FakePrestarted:
        def cleanup(self) -> None:
            return None

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
                return_value=workspace,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
                return_value=config_dir,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
                return_value=home,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
                return_value={"cursor_state_absent": True},
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration._materialize_turn_evidence_root",
                return_value=tmp_path / "evidence",
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.start_bound_mcp",
                return_value=_FakePrestarted(),
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
                return_value=bundle,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.prepare_gate_a_cursor_cli_sandbox",
                side_effect=_capture_sandbox,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.discover_and_gate_gate_a_mcp",
                return_value={
                    "gate_passed": False,
                    "gate_failure_reasons": ["synthetic discovery fail"],
                },
            ),
        )
        stack.enter_context(
            patch("dev.factory.gate_a_trial.orchestration.dispose_trial_workspace", return_value=None),
        )
        stack.enter_context(
            patch("dev.factory.gate_a_trial.orchestration.dispose_trial_config_dir", return_value=None),
        )
        stack.enter_context(
            patch("dev.factory.gate_a_trial.orchestration.dispose_trial_path", return_value=None),
        )
        stack.enter_context(
            patch("dev.factory.gate_a_trial.orchestration.dispose_isolated_home", return_value=None),
        )
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )
    assert not result.ok
    assert profile_paths
    assert not profile_paths[0].exists()


@pytest.mark.skipif(
    shutil.which("agent") is None and not Path.home().joinpath(".local/bin/agent").is_file(),
    reason="Cursor agent CLI not installed",
)
def test_live_agent_mcp_discovery_gate_no_api_key(tmp_path: Path) -> None:
    cursor = resolve_trial_cursor_executable()
    config_dir = tmp_path / "cursor-config"
    ws = materialize_disposable_trial_workspace(parent=tmp_path / "workspaces")
    try:
        materialize_cursor_config_dir(
            config_dir,
            cursor_executable=cursor,
            python_executable=sys.executable,
            workspace=ws,
        )
        home = materialize_isolated_home(tmp_path / "home")
        env = sanitized_cursor_cli_env(cursor_config_dir=str(config_dir), home_dir=str(home))
        sandbox = prepare_gate_a_cursor_cli_sandbox(home)
        try:
            discovery = discover_and_gate_gate_a_mcp(
                cursor,
                str(ws),
                env,
                cursor_config_dir=str(config_dir),
                workspace_mcp_config=str(ws / ".cursor" / "mcp.json"),
                gate_a_sandbox=sandbox,
            )
        finally:
            sandbox.cleanup()
        assert discovery.get("gate_passed") is True
        assert discovery.get("effective_config_hashes_pre_enable")
        assert discovery.get("effective_config_hashes_post_enable")
        assert discovery.get("configured_servers") == [MCP_SERVER_NAME]
        assert discovery.get("configured_tools") == [TOOL_NAME]
    finally:
        dispose_trial_workspace(ws)
