"""Sanitized subprocess environments for the Gate A Cursor CLI trial."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

_TEMP_KEYS = frozenset({"TMPDIR", "TMP", "TEMP"})
_CURSOR_AUTH_KEYS = frozenset(
    {
        "CURSOR_API_KEY",
        "CURSOR_SESSION_TOKEN",
    }
)
_MCP_INHERIT_KEYS = frozenset(
    {
        "MCP_CONFIG",
        "CURSOR_MCP_CONFIG",
    }
)
_ALWAYS_KEEP = frozenset({"PATH", "USER", "SHELL", "TERM"})


def _keep_key(key: str) -> bool:
    if key in _ALWAYS_KEEP or key in _TEMP_KEYS:
        return True
    if key in _MCP_INHERIT_KEYS:
        return False
    if key == "LANG" or key == "TZ" or key.startswith("LC_"):
        return True
    return False


_DISPOSABLE_HOME_MARKER = ".gate-a-disposable-home"


def isolated_home_inventory_manifest(home: Path) -> list[str]:
    """Relative paths of every file under *home* (sorted, posix)."""
    if not home.is_dir():
        return []
    paths: list[str] = []
    for path in sorted(home.rglob("*")):
        if path.is_file():
            paths.append(path.relative_to(home).as_posix())
    return paths


def prove_isolated_home_empty(home: Path) -> dict[str, object]:
    """
    Evidence that *home* has no inherited Cursor state before CLI runs.

    Harness-owned ``.gate-a-disposable-home`` is excluded from the cursor manifest hash.
    """
    manifest = isolated_home_inventory_manifest(home)
    cursor_paths = [
        p for p in manifest if p == ".cursor" or p.startswith(".cursor/")
    ]
    harness_paths = [p for p in manifest if p == _DISPOSABLE_HOME_MARKER]
    cursor_manifest_bytes = json.dumps(cursor_paths, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "path": str(home.resolve()),
        "exists": home.is_dir(),
        "file_count": len(manifest),
        "harness_marker_paths": harness_paths,
        "cursor_inventory_paths": cursor_paths,
        "cursor_inventory_manifest_sha256": hashlib.sha256(cursor_manifest_bytes).hexdigest(),
        "cursor_state_absent": len(cursor_paths) == 0,
        "inventory_paths": manifest,
    }


def pre_enable_home_must_be_pristine(home: Path) -> list[str]:
    """Fail-closed: no inherited ``~/.cursor`` state (e.g. stale mcp-approvals.json)."""
    reasons: list[str] = []
    if not home.is_dir():
        reasons.append(f"isolated HOME does not exist: {home}")
        return reasons
    cursor_home = home / ".cursor"
    if not cursor_home.exists():
        return reasons
    for path in sorted(cursor_home.rglob("*")):
        if path.is_file():
            rel = path.relative_to(home).as_posix()
            reasons.append(
                f"pre-enable HOME must not contain Cursor state (found {rel})"
            )
    return reasons


def materialize_isolated_home(parent: Path) -> Path:
    """
    Create a new empty HOME directory for one trial invocation.

    Never reuses a fixed ``parent/home`` path — each call gets a unique sibling
    directory so prior ``mcp enable`` artifacts cannot leak into pre-enable inventory.
    """
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    home = parent / f"home-{stamp}-{uuid.uuid4().hex[:12]}"
    if home.exists():
        raise FileExistsError(f"refusing to reuse isolated HOME path: {home}")
    home.mkdir(parents=False, exist_ok=False)
    marker = home / _DISPOSABLE_HOME_MARKER
    marker.write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "purpose": "gate_a_trial_isolated_home",
                "dispose_after": "transcript_archived_or_manual_rm",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return home


def sanitized_cursor_cli_env(
    *,
    cursor_config_dir: str,
    home_dir: str | None = None,
    pass_cursor_api_key: bool = False,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    Child environment for ``agent`` headless runs.

    Drops inherited secrets and MCP config. ``CURSOR_API_KEY`` is included only
    when *pass_cursor_api_key* is true (caller must set it in the parent env).
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _CURSOR_AUTH_KEYS:
            if pass_cursor_api_key and key == "CURSOR_API_KEY":
                env[key] = value
            continue
        if _keep_key(key):
            env[key] = value
    env["CURSOR_CONFIG_DIR"] = cursor_config_dir
    env["AGENT_CLI_CREDENTIAL_STORE"] = "memory"
    if home_dir is not None:
        env["HOME"] = home_dir
    elif "HOME" not in env:
        env["HOME"] = str(Path.home())
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def default_isolated_home_parent() -> Path:
    return Path(tempfile.gettempdir()) / "omnigent-gate-a-trial-home"


def dispose_isolated_home(home: Path) -> str | None:
    """Remove one adapter-owned isolated HOME directory."""
    if not home.exists():
        return None
    try:
        shutil.rmtree(home)
    except OSError as exc:
        return f"isolated HOME dispose failed: {exc}"
    if home.exists():
        return f"isolated HOME still present after dispose: {home}"
    return None


def gate_a_mcp_server_env() -> dict[str, str]:
    """Minimal env for the stdio Gate A MCP server (no inherited credentials)."""
    from dev.factory.gate_a_mcp.stdio_launch import gate_a_stdio_mcp_launch

    _, _, env = gate_a_stdio_mcp_launch()
    return env
