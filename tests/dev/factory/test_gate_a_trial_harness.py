"""Offline harness unit tests (stdio env, redaction, evidence paths, stream init)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.checkout import (
    archive_artifact_evidence,
    artifact_path_under_canonical_evidence_root,
    canonical_evidence_root,
    create_disposable_worktree,
    dispose_worktree,
    snapshot_allowed_txt_from_evidence_root,
)
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME
from dev.factory.gate_a_mcp.stdio_launch import (
    gate_a_stdio_mcp_launch,
    prove_stdio_mcp_child_env_clean,
)
from dev.factory.gate_a_trial.mcp_discovery import mcp_server_listing_usable
from dev.factory.gate_a_trial.secret_redact import redact_secrets
from dev.factory.gate_a_trial.stream_json import headless_stream_init_acceptable, parse_stream_json


def test_gate_a_stdio_launch_uses_env_i_and_empty_mcp_json_env(tmp_path: Path) -> None:
    home = tmp_path / "mcp-home"
    home.mkdir()
    command, args, env = gate_a_stdio_mcp_launch(disposable_home=home)
    assert command.endswith("env") or command == "env"
    assert args[0] == "-i"
    assert env == {}
    assert any(str(home) in part for part in args if "=" in part)


def test_stdio_child_env_probe_rejects_cursor_api_key(tmp_path: Path) -> None:
    home = tmp_path / "mcp-home"
    home.mkdir()
    proof = prove_stdio_mcp_child_env_clean(
        python_executable=sys.executable,
        disposable_home=home,
    )
    assert proof.get("ok") is True


def test_redact_secrets_strips_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "super-secret-test-key-12345678")
    text = "failed: CURSOR_API_KEY=super-secret-test-key-12345678"
    redacted = redact_secrets(text)
    assert "super-secret-test-key" not in redacted
    assert "<redacted:cursor-secret>" in redacted


def test_mcp_post_enable_requires_ready_status() -> None:
    assert mcp_server_listing_usable("ready")
    assert not mcp_server_listing_usable("loaded")
    assert not mcp_server_listing_usable("not loaded (needs approval)")


def test_stream_init_requires_env_api_key_source() -> None:
    stdout = json.dumps({"type": "system", "subtype": "init", "apiKeySource": "env"}) + "\n"
    summary = parse_stream_json(stdout)
    ok, _ = headless_stream_init_acceptable(summary)
    assert ok
    login_stdout = (
        json.dumps({"type": "system", "subtype": "init", "apiKeySource": "login"}) + "\n"
    )
    login_summary = parse_stream_json(login_stdout)
    ok_login, reason = headless_stream_init_acceptable(login_summary)
    assert not ok_login
    assert "login" in reason


def test_snapshot_allowed_txt_requires_evidence_root(tmp_path: Path) -> None:
    root = canonical_evidence_root(parent=tmp_path / "evidence")
    checkout = create_disposable_worktree(parent=tmp_path / "wt")
    try:
        artifact = checkout / BOUND_ARTIFACT_FILENAME
        artifact.write_text("ok\n", encoding="utf-8")
        archived = archive_artifact_evidence(artifact, parent=root)
        under, _, _ = artifact_path_under_canonical_evidence_root(archived, parent=root)
        assert under
        snap = snapshot_allowed_txt_from_evidence_root(archived, parent=root)
        assert snap.name == BOUND_ARTIFACT_FILENAME
        with pytest.raises(ValueError):
            snapshot_allowed_txt_from_evidence_root(artifact, parent=root)
    finally:
        dispose_worktree(checkout)
