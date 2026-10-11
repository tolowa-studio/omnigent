"""Trusted probe program fingerprinting (re-hash before every spawn)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ProbeTrustError(ValueError):
    """Probe program failed trust checks."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_has_symlink_component(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def assert_probe_path_admissible(
    trusted_path: Path,
    *,
    worktree: Path,
) -> Path:
    """Probe must be pinned outside the worktree with no symlink hops on the path."""
    if _path_has_symlink_component(trusted_path):
        raise ProbeTrustError(f"trusted probe path contains a symlink component: {trusted_path}")
    resolved = trusted_path.resolve(strict=False)
    worktree_real = Path(os.path.realpath(worktree))
    try:
        resolved.relative_to(worktree_real)
    except ValueError:
        return resolved
    raise ProbeTrustError(f"trusted probe must not live inside worktree: {resolved}")


def assert_probe_program_trusted(
    *,
    trusted_path: Path,
    expected_sha256: str,
    argv_script_path: Path,
    worktree: Path,
) -> str:
    """
    Re-hash the trusted probe on disk and require argv to reference that path only.

    :returns: The verified sha256 hex digest.
    :raises ProbeTrustError: On mismatch, missing file, or argv pointing at a copy.
    """
    resolved_trusted = assert_probe_path_admissible(trusted_path, worktree=worktree)
    resolved_argv = argv_script_path.resolve(strict=False)
    if resolved_argv != resolved_trusted:
        raise ProbeTrustError(
            f"argv probe script must be trusted path {resolved_trusted}, got {resolved_argv}"
        )
    if not resolved_trusted.is_file():
        raise ProbeTrustError(f"trusted probe missing: {resolved_trusted}")
    digest = sha256_file(resolved_trusted)
    if digest != expected_sha256.lower():
        raise ProbeTrustError(
            f"probe program hash mismatch: expected {expected_sha256}, got {digest}"
        )
    return digest


def fingerprint_probe_program(trusted_path: Path) -> str:
    """Hash the trusted probe program (call once when sealing a binding)."""
    path = trusted_path.resolve(strict=False)
    if not path.is_file():
        raise ProbeTrustError(f"trusted probe missing: {path}")
    return sha256_file(path)
