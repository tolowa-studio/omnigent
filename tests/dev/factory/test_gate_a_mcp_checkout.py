"""Fast tests for Gate A MCP disposable checkouts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.checkout import (
    _copy_regular_file_fail_closed,
    archive_artifact_evidence,
    artifact_path_under_canonical_evidence_root,
    canonical_evidence_root,
    create_disposable_worktree,
    dispose_worktree,
    list_evidence_archive_dir_names,
    snapshot_allowed_txt_from_evidence_root,
)
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME
from dev.factory.order_scoped.worktree_guard import _path_has_symlink_component


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS default temp uses /var symlink")
def test_create_disposable_worktree_default_parent_has_no_symlink_component() -> None:
    checkout = create_disposable_worktree()
    try:
        assert checkout.is_dir()
        assert not _path_has_symlink_component(checkout)
    finally:
        dispose_worktree(checkout)


def test_list_evidence_archive_dir_names_ignores_symlink_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_parent = tmp_path / "evidence-root"
    evidence_parent.mkdir()
    monkeypatch.setenv("GATE_A_MCP_EVIDENCE_ROOT", str(evidence_parent))
    root = canonical_evidence_root()
    real = root / f"evidence-real-dir-{os.getpid()}"
    real.mkdir()
    (root / f"evidence-symlink-dir-{os.getpid()}").symlink_to(real, target_is_directory=True)
    names = list_evidence_archive_dir_names()
    assert real.name in names
    assert f"evidence-symlink-dir-{os.getpid()}" not in names


def test_artifact_path_rejects_symlink_allowed_txt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_parent = tmp_path / "evidence-root"
    evidence_parent.mkdir()
    monkeypatch.setenv("GATE_A_MCP_EVIDENCE_ROOT", str(evidence_parent))
    root = canonical_evidence_root()
    archive = root / f"evidence-checkout-test-{os.getpid()}"
    archive.mkdir()
    target = archive / "inner.txt"
    target.write_text("x", encoding="utf-8")
    link = archive / BOUND_ARTIFACT_FILENAME
    link.symlink_to(target)
    under, reason, _ = artifact_path_under_canonical_evidence_root(link)
    assert not under
    assert "symlink" in reason


def test_archive_artifact_evidence_copies_regular_file(tmp_path: Path) -> None:
    evidence_parent = tmp_path / "evidence-root"
    checkout = create_disposable_worktree(parent=tmp_path / "wt")
    try:
        artifact = checkout / BOUND_ARTIFACT_FILENAME
        artifact.write_text("bound-content\n", encoding="utf-8")
        archived = archive_artifact_evidence(artifact, parent=evidence_parent)
        assert archived.read_text(encoding="utf-8") == "bound-content\n"
        assert archived.is_file()
        assert not archived.is_symlink()
        under, _, _ = artifact_path_under_canonical_evidence_root(
            archived, parent=evidence_parent
        )
        assert under
    finally:
        dispose_worktree(checkout)


def test_archive_artifact_evidence_rejects_symlink_allowed_txt(tmp_path: Path) -> None:
    evidence_parent = tmp_path / "evidence-root"
    checkout = create_disposable_worktree(parent=tmp_path / "wt")
    try:
        seeded = checkout / "pre-seeded-target.txt"
        seeded.write_text("stale-certified-bytes\n", encoding="utf-8")
        link = checkout / BOUND_ARTIFACT_FILENAME
        link.symlink_to(seeded)
        with pytest.raises(ValueError, match="symlink"):
            archive_artifact_evidence(link, parent=evidence_parent)
    finally:
        dispose_worktree(checkout)


def test_archive_artifact_evidence_rejects_symlink_parent_component(tmp_path: Path) -> None:
    evidence_parent = tmp_path / "evidence-root"
    real_wt = tmp_path / "real-worker"
    real_wt.mkdir()
    link_wt = tmp_path / "linked-worker"
    link_wt.symlink_to(real_wt, target_is_directory=True)
    artifact = real_wt / BOUND_ARTIFACT_FILENAME
    artifact.write_text("ok\n", encoding="utf-8")
    artifact_via_link = link_wt / BOUND_ARTIFACT_FILENAME
    with pytest.raises(ValueError, match="symlink"):
        archive_artifact_evidence(artifact_via_link, parent=evidence_parent)


def test_copy_regular_file_handles_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    dest = tmp_path / "dest.txt"
    source.write_text("short-write-chunk-test\n", encoding="utf-8")
    real_write = os.write

    def write_at_most_three(fd: int, data: bytes | bytearray) -> int:
        if len(data) > 3:
            return real_write(fd, data[:3])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write_at_most_three)
    _copy_regular_file_fail_closed(source, dest)
    assert dest.read_text(encoding="utf-8") == "short-write-chunk-test\n"


def test_copy_regular_file_zero_write_removes_incomplete_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    dest = tmp_path / "dest.txt"
    source.write_text("will-not-finish\n", encoding="utf-8")

    def write_zero(_fd: int, _data: bytes | bytearray) -> int:
        return 0

    monkeypatch.setattr(os, "write", write_zero)
    with pytest.raises(ValueError, match="write_failed"):
        _copy_regular_file_fail_closed(source, dest)
    assert not dest.exists()


def test_copy_regular_file_rejects_swapped_source_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    dest = tmp_path / "dest.txt"
    source.write_text("bound\n", encoding="utf-8")
    real_fstat = os.fstat

    def fstat_wrong_ino(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result((st.st_mode, st.st_ino + 1, st.st_dev, *st[3:]))

    monkeypatch.setattr(os, "fstat", fstat_wrong_ino)
    with pytest.raises(ValueError, match="artifact_replaced_between_check_and_open"):
        _copy_regular_file_fail_closed(source, dest)
    assert not dest.exists()


def test_snapshot_allowed_txt_uses_fail_closed_copy(tmp_path: Path) -> None:
    evidence_parent = tmp_path / "evidence-root"
    root = canonical_evidence_root(parent=evidence_parent)
    archive_dir = root / f"evidence-snap-{os.getpid()}"
    archive_dir.mkdir()
    real = archive_dir / "real-allowed.txt"
    real.write_text("good\n", encoding="utf-8")
    link = archive_dir / BOUND_ARTIFACT_FILENAME
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        snapshot_allowed_txt_from_evidence_root(link, parent=evidence_parent)
