"""Disposable no-remote git checkouts for the Gate A MCP bridge."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from dev.factory.order_scoped.worktree_guard import _path_has_symlink_component


def _git_init_no_remote(checkout: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(checkout),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "gate-a-mcp@example.invalid"],
        cwd=str(checkout),
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "gate-a-mcp"],
        cwd=str(checkout),
        env=env,
        check=True,
        capture_output=True,
    )


def _canonical_parent(parent: Path | None) -> Path:
    """Real path for mkdtemp parent (macOS ``/var`` → ``/private/var`` spelling)."""
    if parent is None:
        return Path(os.path.realpath(tempfile.gettempdir()))
    return Path(os.path.realpath(parent))


def canonical_harness_temp_root() -> Path:
    """Temp root used by harness evidence checks (honours ``TMPDIR``, realpath spelling)."""
    return _canonical_parent(None)


def create_disposable_worktree(*, parent: Path | None = None) -> Path:
    """Create a temp directory with ``git init`` and no remotes."""
    real_parent = _canonical_parent(parent)
    real_parent.mkdir(parents=True, exist_ok=True)
    checkout = Path(tempfile.mkdtemp(prefix="omnigent-gate-a-mcp-", dir=str(real_parent)))
    _git_init_no_remote(checkout)
    return checkout


def dispose_worktree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _reject_symlink_evidence_path(path: Path) -> str | None:
    if path.is_symlink():
        return "artifact_path_is_symlink"
    if _path_has_symlink_component(path):
        return "artifact_path_contains_symlink_component"
    return None


def is_regular_bound_artifact_file(artifact_path: Path) -> tuple[bool, str]:
    """Regular-file check without following a symlinked ``allowed.txt``."""
    symlink_reason = _reject_symlink_evidence_path(artifact_path)
    if symlink_reason is not None:
        return False, symlink_reason
    try:
        mode = os.lstat(artifact_path).st_mode
    except OSError:
        return False, "artifact_path_not_found"
    if not stat.S_ISREG(mode):
        return False, "artifact_path_not_regular_file"
    return True, ""


def _copy_regular_file_fail_closed(source: Path, dest: Path) -> None:
    """
    Copy bytes from *source* to a fresh *dest* without following symlinks.

    Re-checks at ``open`` time so a swapped symlink cannot certify stale targets.
    """
    symlink_reason = _reject_symlink_evidence_path(source)
    if symlink_reason is not None:
        raise ValueError(f"refusing to copy source: {symlink_reason}")
    try:
        lst = os.lstat(source)
    except OSError as exc:
        raise ValueError("refusing to copy source: artifact_path_not_found") from exc
    if not stat.S_ISREG(lst.st_mode):
        raise ValueError("refusing to copy source: artifact_path_not_regular_file")

    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        src_fd = os.open(source, read_flags)
    except OSError as exc:
        raise ValueError("refusing to copy source: artifact_open_failed") from exc
    try:
        fst = os.fstat(src_fd)
        if not stat.S_ISREG(fst.st_mode):
            raise ValueError("refusing to copy source: artifact_path_not_regular_file")
        if (lst.st_dev, lst.st_ino) != (fst.st_dev, fst.st_ino):
            raise ValueError(
                "refusing to copy source: artifact_replaced_between_check_and_open"
            )

        write_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        )
        dest_fd: int | None = None
        dest_opened = False
        copy_ok = False
        try:
            try:
                dest_fd = os.open(dest, write_flags, 0o644)
            except FileExistsError as exc:
                raise ValueError("refusing to copy: destination_exists") from exc
            except OSError as exc:
                raise ValueError("refusing to copy: destination_open_failed") from exc
            dest_opened = True
            while True:
                chunk = os.read(src_fd, 1024 * 1024)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    written = os.write(dest_fd, chunk[offset:])
                    if written <= 0:
                        raise ValueError("refusing to copy: write_failed")
                    offset += written
            copy_ok = True
        finally:
            if dest_fd is not None:
                os.close(dest_fd)
            if dest_opened and not copy_ok:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
    finally:
        os.close(src_fd)


def artifact_path_under_canonical_evidence_root(
    artifact_path: str | Path,
    *,
    parent: Path | None = None,
) -> tuple[bool, str, Path | None]:
    """
    True when *artifact_path* is under the server-owned evidence root.

    Uses a realpath canonical root (macOS ``/var`` spelling) but rejects symlink
    hops on the artifact path so followed targets cannot certify.
    """
    root_resolved = canonical_evidence_root(parent)
    path = Path(artifact_path)
    symlink_reason = _reject_symlink_evidence_path(path)
    if symlink_reason is not None:
        return False, symlink_reason, root_resolved
    try:
        resolved = Path(os.path.realpath(str(path)))
    except OSError:
        return False, "artifact_path_not_resolvable", root_resolved
    if resolved == root_resolved:
        return False, "artifact_path_is_evidence_root", root_resolved
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return False, "artifact_path_outside_canonical_evidence_root", root_resolved
    return True, "", root_resolved


def is_real_evidence_archive_dir(archive_dir: Path) -> tuple[bool, str]:
    """Archive parent must be a newly created real directory, not a symlink alias."""
    if archive_dir.is_symlink():
        return False, "evidence_archive_dir_is_symlink"
    if _path_has_symlink_component(archive_dir):
        return False, "evidence_archive_dir_contains_symlink_component"
    if not archive_dir.is_dir():
        return False, "evidence_archive_dir_not_directory"
    return True, ""


def list_evidence_archive_dir_names(parent: Path | None = None) -> set[str]:
    """Basenames of real ``evidence-*`` directories under the canonical evidence root."""
    root = canonical_evidence_root(parent)
    names: set[str] = set()
    for entry in root.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir() and entry.name.startswith("evidence-"):
            names.add(entry.name)
    return names


def canonical_evidence_root(parent: Path | None = None) -> Path:
    """Realpath spelling for the server-owned Gate A evidence archive parent."""
    if parent is not None:
        base = Path(parent)
    else:
        env_root = os.environ.get("GATE_A_MCP_EVIDENCE_ROOT", "").strip()
        if env_root:
            base = Path(env_root)
        else:
            base = Path(tempfile.gettempdir()) / "omnigent-gate-a-mcp-evidence"
    base.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(base))


def archive_artifact_evidence(artifact: Path, *, parent: Path | None = None) -> Path:
    """Copy a bound worker artifact from a worker checkout into the evidence root."""
    from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME

    if artifact.name != BOUND_ARTIFACT_FILENAME:
        raise ValueError(
            f"refusing to archive {artifact.name!r}; expected basename {BOUND_ARTIFACT_FILENAME!r}"
        )
    root = canonical_evidence_root(parent)
    regular, reg_reason = is_regular_bound_artifact_file(artifact)
    if not regular:
        raise ValueError(f"refusing to archive {BOUND_ARTIFACT_FILENAME}: {reg_reason}")
    dest_dir = Path(tempfile.mkdtemp(prefix="evidence-", dir=str(root)))
    dest = dest_dir / BOUND_ARTIFACT_FILENAME
    _copy_regular_file_fail_closed(artifact, dest)
    return dest


def snapshot_allowed_txt_from_evidence_root(
    artifact_path: Path,
    *,
    parent: Path | None = None,
    transcript_parent: Path | None = None,
) -> Path:
    """
    Copy an existing ``allowed.txt`` that already lives under the evidence root.

    Refuses paths outside the canonical evidence root or with the wrong basename.
    """
    from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME

    if artifact_path.name != BOUND_ARTIFACT_FILENAME:
        raise ValueError(
            f"refusing to snapshot {artifact_path.name!r}; expected basename {BOUND_ARTIFACT_FILENAME!r}"
        )
    under, reason, root = artifact_path_under_canonical_evidence_root(artifact_path, parent=parent)
    if not under or root is None:
        raise ValueError(f"refusing to copy {BOUND_ARTIFACT_FILENAME}: {reason}")
    regular, reg_reason = is_regular_bound_artifact_file(artifact_path)
    if not regular:
        raise ValueError(f"refusing to snapshot {BOUND_ARTIFACT_FILENAME}: {reg_reason}")
    dest_parent = transcript_parent or root
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest_dir = Path(tempfile.mkdtemp(prefix="transcript-evidence-", dir=str(dest_parent)))
    dest = dest_dir / BOUND_ARTIFACT_FILENAME
    _copy_regular_file_fail_closed(artifact_path, dest)
    return dest
