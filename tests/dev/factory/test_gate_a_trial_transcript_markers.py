"""Filesystem marker snapshots for Gate A trial transcripts."""

from __future__ import annotations

from pathlib import Path

from dev.factory.gate_a_trial.constants import SHELL_MARKER_FILENAME
from dev.factory.gate_a_trial.transcript import GateATrialTranscript


def test_dangling_symlink_marker_counts_as_present(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "missing-target"
    marker = workspace / SHELL_MARKER_FILENAME
    marker.symlink_to(target)
    assert not marker.exists()

    transcript = GateATrialTranscript()
    transcript.snapshot_markers(workspace)
    assert transcript.filesystem_markers[SHELL_MARKER_FILENAME] is True
    assert transcript.synthetic_markers_present() == [SHELL_MARKER_FILENAME]
