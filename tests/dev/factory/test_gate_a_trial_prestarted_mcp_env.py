"""Prestarted Gate A MCP server env must match harness evidence-root spelling."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.checkout import (
    artifact_path_under_canonical_evidence_root,
    canonical_evidence_root,
    canonical_harness_temp_root,
)
from dev.factory.gate_a_trial.prestarted_mcp import prestarted_gate_a_mcp_sanitized_env


def _pin_harness_tmpdir(monkeypatch: pytest.MonkeyPatch, tmpdir: str) -> None:
    """``tempfile.gettempdir()`` caches; reset after changing ``TMPDIR``."""
    monkeypatch.setenv("TMPDIR", tmpdir)
    monkeypatch.setattr(tempfile, "tempdir", None)


def test_prestarted_server_tmpdir_matches_harness_under_macos_style_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_tmp = tmp_path / "private" / "var" / "folders" / "xx" / "T"
    fake_tmp.mkdir(parents=True)
    _pin_harness_tmpdir(monkeypatch, str(fake_tmp))
    control = tmp_path / "gate-a-mcp-control-test"
    control.mkdir()
    env = prestarted_gate_a_mcp_sanitized_env(
        control_dir=control,
        witness_nonce="witness",
        capability="capability-token",
    )
    harness_temp = canonical_harness_temp_root()
    assert Path(env["TMPDIR"]) == harness_temp
    evidence_root = canonical_evidence_root()
    assert evidence_root == harness_temp / "omnigent-gate-a-mcp-evidence"
    assert "CURSOR_API_KEY" not in env
    assert "MCP_CONFIG" not in env


_V27_REAL_POSITIVE_TRANSCRIPT = (
    "stream-positive_gate_a_mcp_tool-20261008T061608Z.stdout.jsonl"
)


def _gate_a_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_local_gate_a_trial_transcript(transcript_name: str) -> str:
    path = _gate_a_repo_root() / "dev/factory/gate_a_trial/transcripts" / transcript_name
    if not path.is_file():
        pytest.skip(
            "local-evidence-missing: Gate A historical CLI transcript not present "
            f"({transcript_name}); ignored under dev/factory/gate_a_trial/transcripts/"
        )
    return path.read_text(encoding="utf-8")


def test_real_positive_transcript_artifact_path_accepted_when_tmpdir_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dev.factory.gate_a_mcp.constants import TOOL_NAME
    from dev.factory.gate_a_trial.stream_json import (
        _parse_mcp_text_content_list,
        parse_stream_json,
        positive_gate_a_tool_satisfied,
    )

    _pin_harness_tmpdir(monkeypatch, "/tmp")
    stdout = _read_local_gate_a_trial_transcript(_V27_REAL_POSITIVE_TRANSCRIPT)
    filtered = "\n".join(line for line in stdout.splitlines() if "readToolCall" not in line)
    summary = parse_stream_json(filtered)
    mcp_calls = [
        call
        for call in summary.tool_calls
        if call.name == TOOL_NAME and call.status == "completed"
    ]
    assert len(mcp_calls) == 1
    loose = _parse_mcp_text_content_list(mcp_calls[0].result)
    assert isinstance(loose, dict)
    artifact_path = loose["artifact_path"]
    under, reason, _ = artifact_path_under_canonical_evidence_root(artifact_path)
    assert under, reason
    parsed = positive_gate_a_tool_satisfied(summary, TOOL_NAME)
    assert parsed is None
