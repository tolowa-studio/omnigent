"""
Build a private ``CURSOR_CONFIG_DIR`` for the disposable Gate A trial.

Materialization writes only ``version``, ``editor``, ``approvalMode``, and
``permissions``. The installed Cursor CLI may add runtime cache fields
(``model``, ``authInfo``, ``privacyCache``, …) and may initialize steering toggles
(``steering``, ``rewind`` as boolean true) on the first no-tool warmup when absent;
see ``CLI_CONFIG_RUNTIME_ADDED_KEY_CATEGORIES`` in ``config_hashes.py``.
Post-positive integrity pins ``approvalMode``/``permissions`` and the post-warmup
steering slice; cosmetic cache fields are accepted when policy is stable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_mcp.stdio_launch import gate_a_stdio_mcp_launch
from dev.factory.gate_a_trial.prestarted_mcp import PrestartedGateAMcp
from dev.factory.gate_a_trial.config_hashes import effective_config_hashes
from dev.factory.gate_a_trial.constants import (
    MANAGED_CURSOR_AGENT_EXECUTABLE,
    TRIAL_ROOT,
    WORKSPACE,
)

_NATIVE_DENY_TOOLS = (
    "Shell",
    "Shell(*)",
    "Write",
    "Write(*)",
    "Read",
    "Read(*)",
    "Grep",
    "Grep(*)",
    "Glob",
    "Glob(*)",
    "Delete",
    "Delete(*)",
    "WebFetch",
    "WebFetch(*)",
)


def _read_global_mcp_server_names() -> list[str]:
    path = Path.home() / ".cursor" / "mcp.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    return sorted(name for name in servers if isinstance(name, str))


def resolve_trial_cursor_executable() -> str:
    """Pin the managed ``~/.local/bin/agent`` binary when present."""
    if MANAGED_CURSOR_AGENT_EXECUTABLE.is_file():
        return str(MANAGED_CURSOR_AGENT_EXECUTABLE.resolve())
    fallback = shutil.which("agent") or shutil.which("cursor-agent") or shutil.which("cursor")
    if fallback:
        return fallback
    raise FileNotFoundError(
        f"Cursor agent CLI not found (expected {MANAGED_CURSOR_AGENT_EXECUTABLE} or agent on PATH)"
    )


def _cursor_cli_version(cursor_executable: str) -> str:
    try:
        proc = subprocess.run(
            [cursor_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or proc.stderr or "").strip()


def _allow_only_gate_a_tool() -> list[str]:
    return [f"Mcp({MCP_SERVER_NAME}:{TOOL_NAME})"]


def _deny_rules(global_mcp_names: list[str]) -> list[str]:
    deny = list(_NATIVE_DENY_TOOLS)
    for name in global_mcp_names:
        if name == MCP_SERVER_NAME:
            continue
        deny.append(f"Mcp({name})")
        deny.append(f"Mcp({name}:*)")
    return deny


def write_workspace_mcp_config(
    *,
    python_executable: str,
    workspace: Path = WORKSPACE,
    mcp_disposable_home: str | Path | None = None,
    prestarted_mcp: PrestartedGateAMcp | None = None,
) -> Path:
    """Project-local MCP registration (trial server only)."""
    cursor_dir = workspace / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    if prestarted_mcp is not None:
        server_entry: dict[str, object] = {
            "url": prestarted_mcp.mcp_url,
            "headers": {
                "Authorization": f"Bearer {prestarted_mcp.capability_token}",
            },
        }
    else:
        command, args, server_env = gate_a_stdio_mcp_launch(
            python_executable=python_executable,
            disposable_home=mcp_disposable_home,
        )
        server_entry = {
            "command": command,
            "args": args,
            "env": server_env,
        }
    payload = {
        "mcpServers": {
            MCP_SERVER_NAME: server_entry,
        }
    }
    path = cursor_dir / "mcp.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def clear_workspace_mcp_config(workspace: Path = WORKSPACE) -> bool:
    """Remove only the project MCP registration (cli-config allowlist unchanged)."""
    path = workspace / ".cursor" / "mcp.json"
    if not path.is_file():
        return False
    path.unlink()
    return True


def materialize_cursor_config_dir(
    target: Path,
    *,
    cursor_executable: str,
    python_executable: str,
    workspace: Path = WORKSPACE,
    mcp_disposable_home: str | Path | None = None,
    prestarted_mcp: PrestartedGateAMcp | None = None,
) -> dict[str, Any]:
    """
    Populate *target* with ``cli-config.json`` (allowlist) and empty global MCP map.

    Global MCP discovery can persist alongside ``CURSOR_CONFIG_DIR``; deny each
    visible global server by name in permissions.
    """
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    global_names = _read_global_mcp_server_names()
    cli_config = {
        "version": 1,
        "editor": {"vimMode": False},
        "approvalMode": "allowlist",
        "permissions": {
            "allow": _allow_only_gate_a_tool(),
            "deny": _deny_rules(global_names),
        },
    }
    (target / "cli-config.json").write_text(json.dumps(cli_config, indent=2) + "\n", encoding="utf-8")
    (target / "mcp.json").write_text(json.dumps({"mcpServers": {}}, indent=2) + "\n", encoding="utf-8")

    workspace_mcp = write_workspace_mcp_config(
        python_executable=python_executable,
        workspace=workspace,
        mcp_disposable_home=mcp_disposable_home,
        prestarted_mcp=prestarted_mcp,
    )
    config_hashes = effective_config_hashes(
        cursor_config_dir=target,
        workspace_mcp_config=workspace_mcp,
    )

    return {
        "cursor_config_dir": str(target),
        "cursor_cli_executable": cursor_executable,
        "cursor_cli_version": _cursor_cli_version(cursor_executable),
        "global_mcp_servers_denied": global_names,
        "workspace_mcp_config": str(workspace_mcp),
        "allowed_mcp_tools": _allow_only_gate_a_tool(),
        "denied_native_tools": list(_NATIVE_DENY_TOOLS),
        "effective_config_hashes": config_hashes,
        "approval_mode": "allowlist",
        "credential_store": "memory",
        "prestarted_mcp": prestarted_mcp.witness_proof() if prestarted_mcp else None,
    }


def default_config_dir() -> Path:
    return TRIAL_ROOT / ".cursor-config"
