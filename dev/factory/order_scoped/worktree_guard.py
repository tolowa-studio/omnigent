"""Worktree path binding: real paths only, no symlink escape."""

from __future__ import annotations

import os
from pathlib import Path


class WorktreeGuardError(ValueError):
    """Approved worktree path failed closed checks."""


def _path_has_symlink_component(path: Path) -> bool:
    current = path
    parts: list[Path] = []
    while True:
        parts.append(current)
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def assert_approved_worktree(worktree: Path, *, bound_worktree: Path) -> Path:
    """
    Require *worktree* to match the bound directory by realpath, as a directory, no symlink hops.
    """
    if not worktree.is_dir():
        raise WorktreeGuardError(f"worktree is not a directory: {worktree}")
    if _path_has_symlink_component(worktree):
        raise WorktreeGuardError(f"worktree path contains a symlink component: {worktree}")
    if _path_has_symlink_component(bound_worktree):
        raise WorktreeGuardError(
            f"bound worktree path contains a symlink component: {bound_worktree}"
        )
    resolved = Path(os.path.realpath(worktree))
    bound = Path(os.path.realpath(bound_worktree))
    if resolved != bound:
        raise WorktreeGuardError(f"worktree escape: {resolved} != {bound}")
    return resolved
