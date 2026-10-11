"""Disposable Cursor profile for real-task Gate A (empty MCP, deny all Mcp)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from dev.factory.gate_a_real.constants import REAL_TASK_CONFIG_KEYS, resolve_cursor_executable
from dev.factory.gate_a_trial.config_hashes import effective_config_hashes
from dev.factory.gate_a_trial.trial_env import sanitized_cursor_cli_env

_MCP_DENY = (
    "Mcp",
    "Mcp(*)",
    "Mcp(*:*)",
    "GetMcpTools",
    "GetMcpTools(*)",
)

_REVIEW_MUTATION_DENY = (
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
)

_TRUSTED_CURSOR_BASH_LAUNCHER_SHA256 = (
    "2ccc9a8e167797641448b5e5c936f006ba137a2555f117f38c5eb76a5238a233"
)
_TRUSTED_CURSOR_NODE_SHA256 = "ebd2d552c7bebde593dd0390530963ad28de56bccde6ce387cdbe55fb0b6fb8e"
_TRUSTED_CURSOR_INDEX_JS_SHA256 = (
    "f6bd8dece34b56431ee74f1ac827e032d054381085e7da7088e85abdd1008330"
)


def _materialize_profile(target: Path, *, read_only: bool) -> dict[str, Any]:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    allowed = ["Read(*)", "Grep(*)", "Glob(*)"]
    denied = list(_MCP_DENY)
    if read_only:
        denied.extend(_REVIEW_MUTATION_DENY)
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


def fleet_operator_home() -> Path:
    """Operator HOME before CLI isolation (not the disposable child HOME)."""
    override = os.environ.get("OMNIGENT_FACTORY_OPERATOR_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home().resolve()


def _path_has_symlink_component(path: Path) -> bool:
    """Reject user-controlled symlink hops; allow macOS /var -> /private/var redirect."""
    try:
        current = path.expanduser()
    except OSError:
        return True
    while True:
        if current.is_symlink():
            if current == Path("/var"):
                break
            return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def _valid_config_directory(path: Path) -> bool:
    try:
        candidate = path.expanduser()
    except OSError:
        return False
    if _path_has_symlink_component(candidate):
        return False
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    return True


def trusted_motion_cursor_gcloud_config() -> str | None:
    """Fleet gcloud config route (same as motion-cursor-agent before HOME isolation)."""
    configured = os.environ.get("CLOUDSDK_CONFIG", "").strip()
    if configured:
        source = Path(configured)
    else:
        operator = fleet_operator_home()
        fleet = operator / ".config" / "motion-fleet-gcloud"
        default = operator / ".config" / "gcloud"
        source = fleet if _valid_config_directory(fleet) else default
    if not _valid_config_directory(source):
        return None
    return str(source.expanduser().resolve(strict=False))


def _cursor_agent_versions_root(operator_home: Path) -> Path:
    return operator_home / ".local/share/cursor-agent/versions"


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _bash_launcher_siblings_trusted(node: Path, entry: Path) -> bool:
    node_hash = _file_sha256(node)
    entry_hash = _file_sha256(entry)
    return (
        node_hash == _TRUSTED_CURSOR_NODE_SHA256 and entry_hash == _TRUSTED_CURSOR_INDEX_JS_SHA256
    )


def _resolved_under_versions_root(path: Path, versions_root: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=False)
        versions_resolved = versions_root.resolve(strict=False)
    except OSError:
        return None
    try:
        relative = resolved.relative_to(versions_resolved)
    except ValueError:
        return None
    if len(relative.parts) != 2 or relative.parts[1] != "cursor-agent":
        return None
    version_entry = versions_root / relative.parts[0]
    if version_entry.is_symlink():
        return None
    try:
        version_entry.resolve(strict=False).relative_to(versions_resolved)
    except ValueError:
        return None
    return resolved


def _wrapper_like_vendor_binary(path: Path) -> bool:
    try:
        payload = path.read_bytes()
    except OSError:
        return True
    head = payload[:16384]
    text = head.decode("utf-8", errors="replace")
    if "motion-cursor-agent" in text or "memory-injecting wrapper" in text:
        return True
    if head.startswith(b"#!"):
        if hashlib.sha256(payload).hexdigest() != _TRUSTED_CURSOR_BASH_LAUNCHER_SHA256:
            return True
        node = path.parent / "node"
        entry = path.parent / "index.js"
        exec_lines = [
            line.strip() for line in text.splitlines() if line.strip().startswith("exec ")
        ]
        return not (
            text.startswith("#!/usr/bin/env bash\n")
            and 'NODE_BIN="$SCRIPT_DIR/node"' in text
            and exec_lines
            and all(
                '"$NODE_BIN"' in line and '"$SCRIPT_DIR/index.js"' in line for line in exec_lines
            )
            and node.is_file()
            and not node.is_symlink()
            and os.access(node, os.X_OK)
            and entry.is_file()
            and not entry.is_symlink()
            and _bash_launcher_siblings_trusted(node, entry)
        )
    # Only the pinned Bash launcher chain is trusted; native binaries are not admissible.
    return True


def _installed_vendor_candidates(operator_home: Path) -> list[Path]:
    versions = _cursor_agent_versions_root(operator_home)
    candidates: list[Path] = []
    for path in versions.glob("*/cursor-agent"):
        if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
            continue
        if _wrapper_like_vendor_binary(path):
            continue
        resolved = _resolved_under_versions_root(path, versions)
        if resolved is None:
            continue
        candidates.append(resolved)
    return candidates


def _newest_vendor_under(operator_home: Path, *, wrapper_resolved: Path) -> Path | None:
    candidates: list[Path] = []
    for path in _installed_vendor_candidates(operator_home):
        trusted = _trusted_vendor_path(
            path,
            operator_home=operator_home,
            wrapper_resolved=wrapper_resolved,
        )
        if trusted is not None:
            candidates.append(trusted)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _trusted_vendor_path(
    path: Path, *, operator_home: Path, wrapper_resolved: Path
) -> Path | None:
    if _wrapper_like_vendor_binary(path):
        return None
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        return None
    versions_root = _cursor_agent_versions_root(operator_home)
    resolved = _resolved_under_versions_root(path, versions_root)
    if resolved is None:
        return None
    if resolved == wrapper_resolved:
        return None
    return resolved


def trusted_cursor_vendor_binary() -> str | None:
    """Resolve the installed vendor CLI before changing HOME for the wrapper."""
    env_bin = os.environ.get("CURSOR_AGENT_REAL_BIN", "").strip()
    operator_home = fleet_operator_home()
    try:
        wrapper = Path(resolve_cursor_executable()).expanduser()
    except (OSError, FileNotFoundError):
        wrapper = Path.home() / ".local/bin/motion-cursor-agent"
    if not wrapper.is_file():
        return None
    try:
        wrapper_resolved = wrapper.resolve(strict=False)
    except OSError:
        return None

    if env_bin:
        chosen = _trusted_vendor_path(
            Path(env_bin).expanduser(),
            operator_home=operator_home,
            wrapper_resolved=wrapper_resolved,
        )
        return str(chosen) if chosen is not None else None

    from_versions = _newest_vendor_under(operator_home, wrapper_resolved=wrapper_resolved)
    if from_versions is not None and from_versions != wrapper_resolved:
        return str(from_versions)
    return None


def real_task_cursor_cli_env(*, cursor_config_dir: str, home_dir: str) -> dict[str, str]:
    extra: dict[str, str] = {}
    cloudsdk = trusted_motion_cursor_gcloud_config()
    if cloudsdk:
        extra["CLOUDSDK_CONFIG"] = cloudsdk
    vendor = trusted_cursor_vendor_binary()
    if vendor is None:
        raise ValueError("trusted Cursor vendor binary pin is missing or invalid")
    extra["CURSOR_AGENT_REAL_BIN"] = vendor
    return sanitized_cursor_cli_env(
        cursor_config_dir=cursor_config_dir,
        home_dir=home_dir,
        pass_cursor_api_key=False,
        extra=extra,
    )
