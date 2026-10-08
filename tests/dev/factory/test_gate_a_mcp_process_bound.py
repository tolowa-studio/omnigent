"""Process-bound Gate A MCP witness (prestarted HTTP path)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.preflight import (
    PREFLIGHT_READY_MARKER,
    preflight_ready_marker_path,
    wait_for_preflight_ready_marker,
)
from dev.factory.gate_a_mcp.process_witness import (
    ProcessWitnessError,
    QualifiedProcessWitness,
    load_qualified_witness,
    stamp_gate_a_mcp_success_payload,
    validate_live_witness,
    wait_for_qualified_witness,
    write_qualified_witness,
)
from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID


def _sample_witness(**overrides: object) -> QualifiedProcessWitness:
    base = {
        "witness_nonce": "nonce-a",
        "server_pid": os.getpid(),
        "listen_host": "127.0.0.1",
        "listen_port": 49152,
        "mcp_url_path": "/mcp",
        "qualified_for_gate_a": True,
        "minted_monotonic": time.monotonic(),
        "settle_observed_seconds": 90.0,
        "order_id": INTERNAL_STAGE_ORDER_ID,
    }
    base.update(overrides)
    return QualifiedProcessWitness(**base)  # type: ignore[arg-type]


def test_stamp_gate_a_mcp_success_payload_only_when_env_nonce_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {"ok": True, "artifact": "allowed.txt"}
    assert stamp_gate_a_mcp_success_payload(base) == base
    monkeypatch.setenv("GATE_A_MCP_WITNESS_NONCE", "bind-nonce")
    stamped = stamp_gate_a_mcp_success_payload(base)
    assert stamped["witness_nonce"] == "bind-nonce"
    assert stamped["server_pid"] == os.getpid()


def test_witness_rejects_process_replacement(tmp_path: Path) -> None:
    witness = _sample_witness(server_pid=999_999)
    write_qualified_witness(tmp_path, witness)
    with pytest.raises(ProcessWitnessError, match="not alive"):
        validate_live_witness(witness, expected_nonce="nonce-a", expected_pid=999_999)


def test_witness_rejects_stale_nonce(tmp_path: Path) -> None:
    witness = _sample_witness()
    write_qualified_witness(tmp_path, witness)
    loaded = load_qualified_witness(tmp_path)
    validate_live_witness(loaded, expected_nonce="nonce-a", expected_pid=os.getpid())
    with pytest.raises(ProcessWitnessError, match="nonce mismatch"):
        validate_live_witness(loaded, expected_nonce="other-nonce")


def test_wait_for_witness_times_out(tmp_path: Path) -> None:
    with pytest.raises(ProcessWitnessError, match="timed out"):
        wait_for_qualified_witness(
            tmp_path,
            expected_nonce="missing",
            timeout_seconds=0.3,
            poll_interval_seconds=0.05,
        )


def test_preflight_marker_rejects_stale_pid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = preflight_ready_marker_path(home)
    marker.write_text(
        json.dumps(
            {
                "qualified_for_gate_a": True,
                "server_pid": 424242,
                "order_id": INTERNAL_STAGE_ORDER_ID,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert PREFLIGHT_READY_MARKER == ".gate_a_preflight_ready"
    assert not wait_for_preflight_ready_marker(
        home,
        timeout_seconds=0.5,
        expected_server_pid=os.getpid(),
    )


def test_preflight_marker_accepts_matching_pid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = preflight_ready_marker_path(home)
    marker.write_text(
        json.dumps(
            {
                "qualified_for_gate_a": True,
                "server_pid": os.getpid(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert wait_for_preflight_ready_marker(
        home,
        timeout_seconds=0.5,
        expected_server_pid=os.getpid(),
    )
