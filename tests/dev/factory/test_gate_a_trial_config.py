"""Offline Gate A Cursor CLI profile/harness tests (no agent subprocess)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_trial.config_hashes import effective_config_hashes
from dev.factory.gate_a_trial.cursor_profile import (
    _allow_only_gate_a_tool,
    _deny_rules,
    clear_workspace_mcp_config,
    materialize_cursor_config_dir,
    write_workspace_mcp_config,
)
from dev.factory.gate_a_trial.prestarted_mcp import PrestartedGateAMcp
from dev.factory.gate_a_trial.trial_env import (
    materialize_isolated_home,
    prove_isolated_home_empty,
    sanitized_cursor_cli_env,
)


def test_cli_config_allowlist_exact_mcp_tool(tmp_path: Path) -> None:
    config_dir = tmp_path / "cursor-config"
    profile = materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
    )
    cli = json.loads((config_dir / "cli-config.json").read_text(encoding="utf-8"))
    assert cli["approvalMode"] == "allowlist"
    perms = cli["permissions"]
    assert perms["allow"] == [f"Mcp({MCP_SERVER_NAME}:{TOOL_NAME})"]
    deny = perms["deny"]
    assert "Shell" in deny and "Write" in deny and "Read" in deny and "WebFetch" in deny
    assert (config_dir / "mcp.json").read_text(encoding="utf-8").strip().startswith("{")
    assert profile["effective_config_hashes"]["cli-config.json"]
    assert profile["credential_store"] == "memory"


def test_workspace_mcp_prestarted_http_url(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    prestarted = PrestartedGateAMcp(
        control_dir=tmp_path / "control",
        control_parent=tmp_path,
        witness_nonce="n",
        server_pid=1,
        capability_token="cap",
        mcp_url="http://127.0.0.1:9/mcp",
        _proc=None,
    )
    path = write_workspace_mcp_config(
        python_executable=sys.executable,
        workspace=workspace,
        prestarted_mcp=prestarted,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["mcpServers"][MCP_SERVER_NAME]
    assert entry["url"] == "http://127.0.0.1:9/mcp"
    assert "command" not in entry


def test_workspace_mcp_only_gate_a_server(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = write_workspace_mcp_config(python_executable=sys.executable, workspace=workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {MCP_SERVER_NAME}


def test_clear_workspace_mcp_leaves_cli_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "cursor-config"
    workspace = tmp_path / "ws"
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=workspace,
    )
    mcp_path = workspace / ".cursor" / "mcp.json"
    assert mcp_path.is_file()
    assert clear_workspace_mcp_config(workspace)
    assert not mcp_path.is_file()
    cli = json.loads((config_dir / "cli-config.json").read_text(encoding="utf-8"))
    assert "Shell" in cli["permissions"]["deny"]


def test_sanitized_env_strips_api_key_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "must-not-forward")
    monkeypatch.setenv("MCP_CONFIG", "/etc/mcp.json")
    env = sanitized_cursor_cli_env(cursor_config_dir="/tmp/gate-a-config")
    assert "CURSOR_API_KEY" not in env
    assert "MCP_CONFIG" not in env
    assert env["AGENT_CLI_CREDENTIAL_STORE"] == "memory"
    assert env["CURSOR_CONFIG_DIR"] == "/tmp/gate-a-config"


def test_sanitized_env_opt_in_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "must-forward")
    env = sanitized_cursor_cli_env(
        cursor_config_dir="/tmp/gate-a-config",
        pass_cursor_api_key=True,
    )
    assert env.get("CURSOR_API_KEY") == "must-forward"


def test_sanitized_env_never_forwards_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "key")
    monkeypatch.setenv("CURSOR_SESSION_TOKEN", "session-must-not-forward")
    env = sanitized_cursor_cli_env(
        cursor_config_dir="/tmp/gate-a-config",
        pass_cursor_api_key=True,
    )
    assert env.get("CURSOR_API_KEY") == "key"
    assert "CURSOR_SESSION_TOKEN" not in env


def test_effective_config_hash_stable_for_permissions(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
    )
    workspace_mcp = tmp_path / "ws" / ".cursor" / "mcp.json"
    workspace_mcp.parent.mkdir(parents=True)
    workspace_mcp.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    first = effective_config_hashes(cursor_config_dir=config_dir, workspace_mcp_config=workspace_mcp)
    second = effective_config_hashes(cursor_config_dir=config_dir, workspace_mcp_config=workspace_mcp)
    assert first == second


def test_isolated_home_marker_and_empty_cursor_manifest(tmp_path: Path) -> None:
    home = materialize_isolated_home(tmp_path / "parent")
    proof = prove_isolated_home_empty(home)
    assert proof["cursor_state_absent"] is True
    assert proof["harness_marker_paths"] == [".gate-a-disposable-home"]
    assert proof["cursor_inventory_paths"] == []


def test_deny_rules_include_foreign_mcp() -> None:
    deny = _deny_rules(["other-mcp"])
    assert f"Mcp(other-mcp)" in deny
    assert _allow_only_gate_a_tool() == [f"Mcp({MCP_SERVER_NAME}:{TOOL_NAME})"]
