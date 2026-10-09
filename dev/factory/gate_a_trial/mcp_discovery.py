"""Parse and gate Cursor CLI ``agent mcp`` discovery for the Gate A trial."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_trial.config_hashes import trial_private_config_fingerprints
from dev.factory.gate_a_trial.cursor_cli_sandbox import GateACursorCliSandbox
from dev.factory.gate_a_trial.secret_redact import redact_mapping_strings
from dev.factory.gate_a_trial.subprocess_session import run_in_new_session
from dev.factory.gate_a_trial.trial_env import pre_enable_home_must_be_pristine

_NO_SERVERS_PREFIX = "No MCP servers configured"
_UNUSABLE_STATUS_MARKERS = ("needs approval", "not loaded", "disabled")
_POST_ENABLE_REQUIRED_STATUS = "ready"


def parse_mcp_list_server_names(stdout: str) -> list[str]:
    """Extract server identifiers from ``agent mcp list`` stdout."""
    names: list[str] = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_NO_SERVERS_PREFIX):
            continue
        if ":" in stripped:
            names.append(stripped.split(":", 1)[0].strip())
    return names


def parse_mcp_list_server_status(stdout: str, server_name: str) -> str | None:
    """Return the status suffix after ``server_name:`` on a matching list line."""
    prefix = f"{server_name}:"
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return None


def mcp_server_listing_usable(status_suffix: str | None) -> bool:
    """True when post-enable ``mcp list`` reports the exact ``ready`` status."""
    if not status_suffix:
        return False
    normalized = status_suffix.strip().lower()
    if normalized != _POST_ENABLE_REQUIRED_STATUS:
        return False
    lower = status_suffix.lower()
    return not any(marker in lower for marker in _UNUSABLE_STATUS_MARKERS)


def parse_mcp_list_tool_names(stdout: str) -> list[str]:
    """Extract tool names from ``agent mcp list-tools <id>`` stdout."""
    tools: list[str] = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:].strip()
        name = re.split(r"\s*\(", body, maxsplit=1)[0].strip()
        if name:
            tools.append(name)
    return tools


def _cli_returncode(step: dict[str, Any]) -> int:
    rc = step.get("returncode")
    if rc is None:
        return 1
    return int(rc)


def _run_mcp_cli(
    cursor_executable: str,
    workspace_cwd: str,
    env: dict[str, str],
    *mcp_args: str,
    timeout_seconds: float = 180.0,
    gate_a_sandbox: GateACursorCliSandbox | None = None,
) -> dict[str, Any]:
    argv = [cursor_executable, "mcp", *mcp_args]
    if gate_a_sandbox is not None:
        argv = gate_a_sandbox.wrap_argv(argv)
    proc = run_in_new_session(
        argv,
        cwd=workspace_cwd,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    if proc.error:
        return redact_mapping_strings(
            {"argv": argv, "error": proc.error},
            env=env,
        )
    payload = {
        "argv": argv,
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }
    if proc.timed_out:
        payload["timed_out"] = True
    return redact_mapping_strings(payload, env=env)


def validate_zero_mcp_servers_discovery(
    *,
    list_stdout: str,
    list_returncode: int,
) -> dict[str, Any]:
    """Fail-closed: workspace MCP removed — CLI must report zero configured servers."""
    servers = parse_mcp_list_server_names(list_stdout)
    reasons: list[str] = []
    if list_returncode != 0:
        reasons.append(f"mcp list exit {list_returncode} (required 0)")
    if servers:
        reasons.append(
            "expected 0 MCP servers after workspace mcp.json removal, "
            f"got {len(servers)}: {servers!r}"
        )
    if _NO_SERVERS_PREFIX not in (list_stdout or "") and not servers:
        # Some CLI builds may omit the prefix when the list is empty; servers==0 is enough.
        pass
    gate_passed = not reasons
    return {
        "gate_passed": gate_passed,
        "gate_failure_reasons": reasons,
        "configured_servers": servers,
        "expected_server_count": 0,
    }


def discover_and_assert_zero_mcp_servers(
    cursor_executable: str,
    workspace_cwd: str,
    env: dict[str, str],
    *,
    gate_a_sandbox: GateACursorCliSandbox | None = None,
) -> dict[str, Any]:
    """Run ``mcp list`` only; require zero servers before without-MCP negatives."""
    listing = _run_mcp_cli(
        cursor_executable,
        workspace_cwd,
        env,
        "list",
        gate_a_sandbox=gate_a_sandbox,
    )
    out: dict[str, Any] = {"list": listing}
    if listing.get("error"):
        validation = validate_zero_mcp_servers_discovery(
            list_stdout="",
            list_returncode=1,
        )
        out.update(validation)
        out["gate_passed"] = False
        out["gate_failure_reasons"] = [
            str(listing["error"]),
            *list(out.get("gate_failure_reasons") or []),
        ]
        return out
    validation = validate_zero_mcp_servers_discovery(
        list_stdout=str(listing.get("stdout") or ""),
        list_returncode=_cli_returncode(listing),
    )
    out.update(validation)
    return out


def validate_gate_a_discovery(
    *,
    list_stdout: str,
    list_tools_stdout: str,
    list_returncode: int,
    list_tools_returncode: int,
    enable_returncode: int | None = None,
    post_enable_list_stdout: str | None = None,
    post_enable_list_returncode: int | None = None,
) -> dict[str, Any]:
    """Fail-closed checks: zero CLI exits, one server/tool, server usable after enable."""
    servers = parse_mcp_list_server_names(list_stdout)
    tools = parse_mcp_list_tool_names(list_tools_stdout)
    reasons: list[str] = []

    if list_returncode != 0:
        reasons.append(f"mcp list exit {list_returncode} (required 0)")
    if enable_returncode is not None and enable_returncode != 0:
        reasons.append(f"mcp enable exit {enable_returncode} (required 0)")
    if post_enable_list_returncode is not None and post_enable_list_returncode != 0:
        reasons.append(f"mcp list (post-enable) exit {post_enable_list_returncode} (required 0)")
    if list_tools_returncode != 0:
        reasons.append(f"mcp list-tools exit {list_tools_returncode} (required 0)")

    if _NO_SERVERS_PREFIX in (list_stdout or ""):
        reasons.append("mcp list reported no servers (project root likely wrong)")
    if len(servers) != 1:
        reasons.append(f"expected 1 MCP server, got {len(servers)}: {servers!r}")
    elif servers[0] != MCP_SERVER_NAME:
        reasons.append(f"expected server {MCP_SERVER_NAME!r}, got {servers[0]!r}")

    if post_enable_list_stdout is not None:
        post_servers = parse_mcp_list_server_names(post_enable_list_stdout)
        if len(post_servers) != 1 or post_servers[0] != MCP_SERVER_NAME:
            reasons.append(
                f"post-enable mcp list expected only {MCP_SERVER_NAME!r}, got {post_servers!r}"
            )
        status = parse_mcp_list_server_status(post_enable_list_stdout, MCP_SERVER_NAME)
        if not mcp_server_listing_usable(status):
            reasons.append(
                "post-enable server status must be "
                f"{_POST_ENABLE_REQUIRED_STATUS!r}, got {status!r}"
            )

    if len(tools) != 1:
        reasons.append(f"expected 1 MCP tool, got {len(tools)}: {tools!r}")
    elif tools[0] != TOOL_NAME:
        reasons.append(f"expected tool {TOOL_NAME!r}, got {tools[0]!r}")

    gate_passed = not reasons
    return {
        "gate_passed": gate_passed,
        "gate_failure_reasons": reasons,
        "configured_servers": servers,
        "configured_tools": tools,
        "expected_server": MCP_SERVER_NAME,
        "expected_tool": TOOL_NAME,
        "post_enable_server_status": (
            parse_mcp_list_server_status(post_enable_list_stdout or "", MCP_SERVER_NAME)
            if post_enable_list_stdout is not None
            else None
        ),
    }


def discover_and_gate_gate_a_mcp(
    cursor_executable: str,
    workspace_cwd: str,
    env: dict[str, str],
    *,
    cursor_config_dir: str | Path | None = None,
    workspace_mcp_config: str | Path | None = None,
    gate_a_sandbox: GateACursorCliSandbox | None = None,
) -> dict[str, Any]:
    """
    Run ``mcp list``, approve the trial server locally, then ``mcp list-tools``.

    No API key required; spawns the stdio MCP child only for list-tools after enable.
    """
    config_dir = Path(cursor_config_dir) if cursor_config_dir else None
    workspace_mcp = Path(workspace_mcp_config) if workspace_mcp_config else None
    home_dir = Path(env["HOME"]) if env.get("HOME") else None

    pre_hashes: dict[str, str] = {}
    isolated_home_proof: dict[str, object] = {}
    pristine_reasons: list[str] = []
    if home_dir is not None:
        from dev.factory.gate_a_trial.trial_env import prove_isolated_home_empty

        isolated_home_proof = prove_isolated_home_empty(home_dir)
        pristine_reasons = pre_enable_home_must_be_pristine(home_dir)
    if config_dir is not None:
        pre_hashes = trial_private_config_fingerprints(
            cursor_config_dir=config_dir,
            workspace_mcp_config=workspace_mcp,
            home_dir=home_dir,
        )

    if pristine_reasons:
        return {
            "effective_config_hashes_pre_enable": pre_hashes,
            "isolated_home_initial_proof": isolated_home_proof,
            "gate_passed": False,
            "gate_failure_reasons": pristine_reasons,
            "enable": {"skipped": True, "reason": "home inventory not pristine"},
            "list": {"skipped": True},
            "list_tools": {"skipped": True},
            "list_post_enable": {"skipped": True},
        }

    if gate_a_sandbox is None:
        return {
            "effective_config_hashes_pre_enable": pre_hashes,
            "isolated_home_initial_proof": isolated_home_proof,
            "gate_passed": False,
            "gate_failure_reasons": [
                "Gate A MCP discovery requires sandbox-exec wrapper (gate_a_sandbox missing)",
            ],
            "enable": {"skipped": True, "reason": "sandbox missing"},
            "list": {"skipped": True},
            "list_tools": {"skipped": True},
            "list_post_enable": {"skipped": True},
        }

    listing = _run_mcp_cli(
        cursor_executable,
        workspace_cwd,
        env,
        "list",
        gate_a_sandbox=gate_a_sandbox,
    )
    out: dict[str, Any] = {
        "list": listing,
        "effective_config_hashes_pre_enable": pre_hashes,
        "isolated_home_initial_proof": isolated_home_proof,
    }
    if listing.get("error"):
        out.update(
            validate_gate_a_discovery(
                list_stdout="",
                list_tools_stdout="",
                list_returncode=1,
                list_tools_returncode=1,
                enable_returncode=1,
                post_enable_list_stdout="",
                post_enable_list_returncode=1,
            )
        )
        out["gate_passed"] = False
        out["gate_failure_reasons"] = [
            str(listing["error"]),
            *list(out.get("gate_failure_reasons") or []),
        ]
        return out

    servers = parse_mcp_list_server_names(str(listing.get("stdout") or ""))
    enable_returncode = 1
    post_enable_list: dict[str, Any] = {
        "argv": [],
        "returncode": 1,
        "stdout": "",
        "stderr": "",
        "skipped": True,
    }
    if len(servers) == 1 and servers[0] == MCP_SERVER_NAME:
        enable = _run_mcp_cli(
            cursor_executable,
            workspace_cwd,
            env,
            "enable",
            MCP_SERVER_NAME,
            gate_a_sandbox=gate_a_sandbox,
        )
        out["enable"] = enable
        enable_returncode = _cli_returncode(enable)
        post_enable_list = _run_mcp_cli(
            cursor_executable,
            workspace_cwd,
            env,
            "list",
            gate_a_sandbox=gate_a_sandbox,
        )
        post_enable_list.pop("skipped", None)
    else:
        out["enable"] = {
            "argv": [cursor_executable, "mcp", "enable", MCP_SERVER_NAME],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "skipped": True,
        }

    out["list_post_enable"] = post_enable_list

    tools_listing = _run_mcp_cli(
        cursor_executable,
        workspace_cwd,
        env,
        "list-tools",
        MCP_SERVER_NAME,
        gate_a_sandbox=gate_a_sandbox,
    )
    out["list_tools"] = tools_listing

    post_hashes: dict[str, str] = {}
    if config_dir is not None:
        post_hashes = trial_private_config_fingerprints(
            cursor_config_dir=config_dir,
            workspace_mcp_config=workspace_mcp,
            home_dir=home_dir,
        )
    out["effective_config_hashes_post_enable"] = post_hashes

    validation = validate_gate_a_discovery(
        list_stdout=str(listing.get("stdout") or ""),
        list_tools_stdout=str(tools_listing.get("stdout") or ""),
        list_returncode=_cli_returncode(listing),
        list_tools_returncode=_cli_returncode(tools_listing),
        enable_returncode=enable_returncode,
        post_enable_list_stdout=str(post_enable_list.get("stdout") or ""),
        post_enable_list_returncode=_cli_returncode(post_enable_list),
    )
    out.update(validation)
    return out
