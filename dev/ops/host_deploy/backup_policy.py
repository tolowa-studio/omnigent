"""Rollback backup scope, size cap, and retention helpers for host upgrades."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

DEFAULT_MAX_BACKUP_MIB = 200
ROLLBACK_RETENTION = 3

# Directory name segments excluded anywhere under a copied tree.
EXCLUDED_DIR_NAMES = frozenset(
    {
        "logs",
        "cache",
        "caches",
        "__pycache__",
        ".cache",
        "artifacts",
    }
)

EXCLUDED_FILE_SUFFIXES = (".log", ".jsonl")


class BackupSizeExceededError(RuntimeError):
    """Raised when a planned backup would exceed the configured size cap."""

    def __init__(self, planned_bytes: int, cap_bytes: int) -> None:
        self.planned_bytes = planned_bytes
        self.cap_bytes = cap_bytes
        cap_mib = cap_bytes // (1024 * 1024)
        super().__init__(
            f"planned backup size {planned_bytes} bytes exceeds cap {cap_bytes} bytes "
            f"({cap_mib} MiB); aborting before copy"
        )


def max_backup_bytes_from_env_or_flag(
    flag_mib: int | None,
    *,
    env_value: str | None = None,
) -> int:
    """Resolve the backup size cap from CLI (MiB) or ``OMNIGENT_HOST_BACKUP_MAX_MIB``."""
    if flag_mib is not None:
        return flag_mib * 1024 * 1024
    raw = env_value if env_value is not None else os.environ.get("OMNIGENT_HOST_BACKUP_MAX_MIB")
    if raw is None or raw == "":
        return DEFAULT_MAX_BACKUP_MIB * 1024 * 1024
    return int(raw) * 1024 * 1024


def relative_path_is_excluded(rel: Path) -> bool:
    if not rel.parts:
        return False
    for part in rel.parts:
        if part in EXCLUDED_DIR_NAMES:
            return True
    return rel.name.endswith(EXCLUDED_FILE_SUFFIXES)


def copytree_ignore(root: Path, directory: str, names: list[str]) -> set[str]:
    base = Path(directory)
    ignored: set[str] = set()
    for name in names:
        rel = (base / name).relative_to(root)
        if relative_path_is_excluded(rel):
            ignored.add(name)
    return ignored


def iter_included_files(root: Path) -> list[tuple[Path, int]]:
    """List regular files under ``root`` that would be included in a filtered copy."""
    root = root.resolve()
    if not root.is_dir():
        return []
    included: list[tuple[Path, int]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not relative_path_is_excluded((base / d).relative_to(root))
            and not (base / d).is_symlink()
        )
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink():
                continue
            rel = path.relative_to(root)
            if relative_path_is_excluded(rel):
                continue
            if path.is_file():
                included.append((rel, path.stat().st_size))
    return included


def file_size_if_included(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        rel = Path(path.name)
        if relative_path_is_excluded(rel):
            return 0
        return path.stat().st_size
    if path.is_dir():
        return sum(size for _, size in iter_included_files(path))
    return 0


def planned_backup_bytes(
    *,
    directory_roots: list[Path],
    file_paths: list[Path],
) -> int:
    total = 0
    for root in directory_roots:
        if root.is_file():
            total += file_size_if_included(root)
        elif root.is_dir():
            total += sum(size for _, size in iter_included_files(root))
    for path in file_paths:
        total += file_size_if_included(path)
    return total


def assert_within_cap(planned_bytes: int, cap_bytes: int) -> None:
    if planned_bytes > cap_bytes:
        raise BackupSizeExceededError(planned_bytes, cap_bytes)


def path_is_contained(child: Path, parent: Path) -> bool:
    child_res = child.resolve(strict=False)
    parent_res = parent.resolve(strict=False)
    try:
        child_res.relative_to(parent_res)
    except ValueError:
        return False
    return True


def list_rollback_stamps(rollback_dir: Path) -> list[Path]:
    rollback_resolved = rollback_dir.resolve(strict=False)
    if not rollback_resolved.is_dir():
        return []
    stamps: list[Path] = []
    for entry in rollback_dir.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        entry_resolved = entry.resolve(strict=False)
        if not path_is_contained(entry_resolved, rollback_resolved):
            continue
        stamps.append(entry)
    stamps.sort(key=lambda p: p.name)
    return stamps


def plan_rollback_prune(rollback_dir: Path, keep: int = ROLLBACK_RETENTION) -> list[Path]:
    """Return rollback stamp dirs to remove so a new stamp stays within ``keep`` total."""
    stamps = list_rollback_stamps(rollback_dir)
    if len(stamps) < keep:
        return []
    remove_count = len(stamps) - keep + 1
    return stamps[:remove_count]


def prune_rollbacks(
    rollback_dir: Path,
    *,
    keep: int = ROLLBACK_RETENTION,
    dry_run: bool = False,
) -> list[Path]:
    rollback_resolved = rollback_dir.resolve(strict=False)
    removed: list[Path] = []
    for path in plan_rollback_prune(rollback_dir, keep=keep):
        target = path.resolve(strict=False)
        if not path_is_contained(target, rollback_resolved):
            raise RuntimeError(f"refusing to prune path outside rollback dir: {path}")
        removed.append(path)
        if dry_run:
            continue
        shutil.rmtree(path)
    return removed


def filtered_copytree(src: Path, dst: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return copytree_ignore(src, directory, names)

    shutil.copytree(src, dst, symlinks=True, ignore=ignore, copy_function=shutil.copy2)
