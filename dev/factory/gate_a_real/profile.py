"""Disposable Cursor profile for real-task Gate A (empty MCP, deny all Mcp)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from dev.factory.gate_a_real.constants import REAL_TASK_CONFIG_KEYS
from dev.factory.gate_a_trial.config_hashes import effective_config_hashes
from dev.factory.gate_a_trial.trial_env import sanitized_cursor_cli_env

_MCP_DENY = (
    "Mcp(*)",
    "Mcp(*:*)",
)


def _materialize_profile(target: Path, *, read_only: bool) -> dict[str, Any]:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    allowed = ["Read(*)", "Grep(*)", "Glob(*)"]
    denied = list(_MCP_DENY)
    if read_only:
        denied.extend(["Shell(*)", "Write(*)", "Edit(*)", "Delete(*)"])
    else:
        allowed.extend(["Shell(*)", "Write(*)", "Delete(*)"])
    cli_config = {
        "version": 1,
        "editor": {"vimMode": False},
        "approvalMode": "allowlist",
        "permissions": {
            "allow": allowed,
            "deny": denied,
        },
    }
    (target / "cli-config.json").write_text(
        json.dumps(cli_config, indent=2) + "\n",
        encoding="utf-8",
    )
    (target / "mcp.json").write_text(
        json.dumps({"mcpServers": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    hashes = effective_config_hashes(cursor_config_dir=target, workspace_mcp_config=None)
    missing = sorted(k for k in REAL_TASK_CONFIG_KEYS if k not in hashes)
    if missing:
        raise RuntimeError(f"real-task profile missing fingerprints: {missing}")
    return {
        "cursor_config_dir": str(target),
        "effective_config_hashes": {k: hashes[k] for k in sorted(REAL_TASK_CONFIG_KEYS)},
    }


def materialize_real_task_cursor_config_dir(target: Path) -> dict[str, Any]:
    """Write the builder profile with empty MCP and explicit MCP deny."""
    return _materialize_profile(target, read_only=False)


def materialize_real_task_review_config_dir(target: Path) -> dict[str, Any]:
    """Write a separate read-only profile for the independent reviewer."""
    return _materialize_profile(target, read_only=True)


def trusted_motion_cursor_gcloud_config() -> str | None:
    """Existing parent gcloud config for the wrapper, outside isolated CLI HOME."""
    configured = os.environ.get("CLOUDSDK_CONFIG")
    source = Path(configured).expanduser() if configured else Path.home() / ".config" / "gcloud"
    if source.is_dir():
        return str(source.resolve())
    return None


def trusted_cursor_vendor_binary() -> str | None:
    """Resolve the installed vendor CLI before changing HOME for the wrapper."""
    chosen = os.environ.get("CURSOR_AGENT_REAL_BIN", "").strip()
    if chosen:
        path = Path(chosen).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return None
    versions = Path.home() / ".local/share/cursor-agent/versions"
    candidates = [
        path
        for path in versions.glob("*/cursor-agent")
        if path.is_file() and os.access(path, os.X_OK)
    ]
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: path.stat().st_mtime).resolve())


def real_task_cursor_cli_env(*, cursor_config_dir: str, home_dir: str) -> dict[str, str]:
    extra: dict[str, str] = {}
    cloudsdk = trusted_motion_cursor_gcloud_config()
    if cloudsdk:
        extra["CLOUDSDK_CONFIG"] = cloudsdk
    vendor = trusted_cursor_vendor_binary()
    if vendor:
        extra["CURSOR_AGENT_REAL_BIN"] = vendor
    return sanitized_cursor_cli_env(
        cursor_config_dir=cursor_config_dir,
        home_dir=home_dir,
        pass_cursor_api_key=False,
        extra=extra,
    )
