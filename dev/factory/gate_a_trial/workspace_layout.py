"""Disposable git-root trial workspace (Cursor resolves MCP from the repo root)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from dev.factory.gate_a_mcp.checkout import create_disposable_worktree, dispose_worktree
from dev.factory.gate_a_trial.trial_env import default_isolated_home_parent


def default_disposable_workspace_parent() -> Path:
    return default_isolated_home_parent() / "workspaces"


def default_disposable_config_parent() -> Path:
    return default_isolated_home_parent() / "cursor-configs"


def materialize_disposable_trial_config_dir(*, parent: Path | None = None) -> Path:
    """Create a unique empty directory for one adapter-owned ``CURSOR_CONFIG_DIR``."""
    root = parent or default_disposable_config_parent()
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="omnigent-gate-a-cursor-config-", dir=str(root)))


def dispose_trial_path(path: Path, *, label: str) -> str | None:
    """Remove a disposable path; return a problem string when disposal fails."""
    if not path.exists():
        return None
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return f"{label} dispose failed: {exc}"
    if path.exists():
        return f"{label} still present after dispose: {path}"
    return None


def dispose_trial_config_dir(path: Path) -> str | None:
    return dispose_trial_path(path, label="cursor config dir")


def materialize_disposable_trial_workspace(*, parent: Path | None = None) -> Path:
    """Create a no-remote git repo; ``.cursor/mcp.json`` must live at this root."""
    return create_disposable_worktree(parent=parent or default_disposable_workspace_parent())


def dispose_trial_workspace(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        dispose_worktree(path)
    except OSError as exc:
        return f"trial workspace dispose failed: {exc}"
    if path.exists():
        return f"trial workspace still present after dispose: {path}"
    return None
