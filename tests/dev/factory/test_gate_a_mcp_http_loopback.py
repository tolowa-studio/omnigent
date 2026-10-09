"""Loopback streamable-HTTP transport host pinning and control-dir disposal."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

from dev.factory.gate_a_mcp.preflight import (
    reset_preflight_session_for_tests,
)
from dev.factory.gate_a_mcp.process_witness import (
    CAPABILITY_FILENAME,
    LoopbackTransportNotReady,
    ProcessWitnessError,
    dispose_gate_a_mcp_control_dir,
    loopback_allowed_hosts_for_port,
    mint_capability_token,
    probe_authenticated_loopback_mcp_transport,
    qualify_process_witness_transport,
    wait_for_qualified_witness,
    write_http_capability,
)
from dev.factory.gate_a_trial.prestarted_mcp import (
    PrestartedGateAMcp,
    materialize_control_dir,
    start_prestarted_gate_a_mcp,
)
from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID
from dev.factory.order_scoped.receipt_admission import GATE_A_EXACT_PROBE_NAMES
from dev.factory.seatbelt_fixture.manifest import (
    GATE_A_MIN_SETTLE_SECONDS,
    FixtureReceipt,
    ProbeRecord,
)
from tests.dev.factory.gate_admission_test_support import bind_trusted_gate_receipt_for_admission


def _qualified_receipt() -> FixtureReceipt:
    probes = [ProbeRecord(name, True, "ok") for name in sorted(GATE_A_EXACT_PROBE_NAMES)]
    receipt = FixtureReceipt(
        passed=True,
        qualified_for_gate_a=True,
        failure_reason=None,
        order_id=INTERNAL_STAGE_ORDER_ID,
        manifest_hash="deadbeef",
        command_hash="cafebabe",
        platform="darwin",
        machine="arm64",
        sandbox_backend="darwin_seatbelt",
        probes=probes,
        started_at="2020-01-01T00:00:00+00:00",
        ended_at="2020-01-01T00:02:00+00:00",
        settle_seconds=GATE_A_MIN_SETTLE_SECONDS,
        settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
    )
    return bind_trusted_gate_receipt_for_admission(receipt)


def test_loopback_allowed_hosts_include_ephemeral_port() -> None:
    assert loopback_allowed_hosts_for_port(49152) == ["127.0.0.1:49152"]


def test_transport_security_rejects_bare_loopback_host() -> None:
    middleware = TransportSecurityMiddleware(
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=loopback_allowed_hosts_for_port(55555),
        ),
    )
    assert middleware._validate_host("127.0.0.1") is False
    assert middleware._validate_host("127.0.0.1:55555") is True
    assert middleware._validate_host("127.0.0.1:55556") is False


def test_dispose_control_dir_requires_harness_prefix_and_parent(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    control = materialize_control_dir(parent)
    token = mint_capability_token()
    write_http_capability(control, token)
    assert (control / CAPABILITY_FILENAME).is_file()
    assert dispose_gate_a_mcp_control_dir(control, allowed_parent=parent)
    assert not control.exists()


def test_dispose_control_dir_refuses_arbitrary_path(tmp_path: Path) -> None:
    victim = tmp_path / "not-a-control-dir"
    victim.mkdir()
    (victim / "secret").write_text("keep\n", encoding="utf-8")
    assert dispose_gate_a_mcp_control_dir(victim, allowed_parent=tmp_path) is False
    assert victim.is_dir()


def test_prestarted_cleanup_removes_capability_after_stop(tmp_path: Path) -> None:
    parent = tmp_path / "controls"
    parent.mkdir()
    control = materialize_control_dir(parent)
    token = mint_capability_token()
    write_http_capability(control, token)
    handle = PrestartedGateAMcp(
        control_dir=control,
        control_parent=parent,
        witness_nonce="n",
        server_pid=os.getpid(),
        capability_token=token,
        mcp_url="http://127.0.0.1:9/mcp",
        _proc=None,
    )
    handle.cleanup()
    assert not control.exists()


def _spawn_test_http_server(
    *,
    control_dir: Path,
    witness_nonce: str,
    capability: str,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    env["GATE_A_MCP_CONTROL_DIR"] = str(control_dir)
    env["GATE_A_MCP_WITNESS_NONCE"] = witness_nonce
    env["GATE_A_MCP_HTTP_CAPABILITY"] = capability
    from dev.factory.order_scoped.binding import STAGE_WORKER_ENV

    env[STAGE_WORKER_ENV] = "1"
    bootstrap = """
from dev.factory.gate_a_mcp.preflight import (
    install_preflight_receipt_for_tests,
    reset_preflight_session_for_tests,
)
from dev.factory.gate_a_mcp.http_serve import run_prestarted_http_server
from tests.dev.factory.test_gate_a_mcp_http_loopback import _qualified_receipt
reset_preflight_session_for_tests()
install_preflight_receipt_for_tests(_qualified_receipt())
run_prestarted_http_server()
"""
    return subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_authenticated_mcp_initialize_list_tools_on_live_server(tmp_path: Path) -> None:
    reset_preflight_session_for_tests()
    parent = tmp_path / "parent"
    parent.mkdir()
    control = materialize_control_dir(parent)
    witness_nonce = "nonce-live-transport"
    capability = mint_capability_token()
    write_http_capability(control, capability)
    proc = _spawn_test_http_server(
        control_dir=control,
        witness_nonce=witness_nonce,
        capability=capability,
    )
    try:
        wait_for_qualified_witness(
            control,
            expected_nonce=witness_nonce,
            expected_pid=proc.pid,
            capability_token=capability,
            timeout_seconds=120.0,
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        dispose_gate_a_mcp_control_dir(control, allowed_parent=parent)


def test_start_failure_disposes_control_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ProcessWitnessError("witness not yet published")

    monkeypatch.setattr(
        "dev.factory.gate_a_trial.prestarted_mcp.wait_for_qualified_witness",
        boom,
    )
    with pytest.raises(ProcessWitnessError, match="witness not yet published"):
        start_prestarted_gate_a_mcp(
            python_executable=sys.executable,
            control_parent=parent,
            witness_timeout_seconds=0.1,
        )
    remaining = list(parent.glob("gate-a-mcp-control-*"))
    assert remaining == []


def test_early_witness_before_bind_waits_for_transport(tmp_path: Path) -> None:
    reset_preflight_session_for_tests()
    parent = tmp_path / "parent"
    parent.mkdir()
    control = materialize_control_dir(parent)
    witness_nonce = "nonce-early-witness"
    capability = mint_capability_token()
    write_http_capability(control, capability)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    env["GATE_A_MCP_CONTROL_DIR"] = str(control)
    env["GATE_A_MCP_WITNESS_NONCE"] = witness_nonce
    env["GATE_A_MCP_HTTP_CAPABILITY"] = capability
    env["GATE_A_MCP_TEST_DELAY_BIND_SECONDS"] = "1.5"
    from dev.factory.order_scoped.binding import STAGE_WORKER_ENV

    env[STAGE_WORKER_ENV] = "1"
    bootstrap = """
from dev.factory.gate_a_mcp.preflight import (
    install_preflight_receipt_for_tests,
    reset_preflight_session_for_tests,
)
from dev.factory.gate_a_mcp.http_serve import run_prestarted_http_server
from tests.dev.factory.test_gate_a_mcp_http_loopback import _qualified_receipt
reset_preflight_session_for_tests()
install_preflight_receipt_for_tests(_qualified_receipt())
run_prestarted_http_server()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for_qualified_witness(
            control,
            expected_nonce=witness_nonce,
            expected_pid=proc.pid,
            capability_token=capability,
            timeout_seconds=60.0,
            transport_startup_retry_seconds=5.0,
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        dispose_gate_a_mcp_control_dir(control, allowed_parent=parent)


def test_never_binding_endpoint_not_false_ready(tmp_path: Path) -> None:
    from dev.factory.gate_a_mcp.process_witness import (
        QualifiedProcessWitness,
        write_qualified_witness,
    )

    parent = tmp_path / "parent"
    parent.mkdir()
    control = materialize_control_dir(parent)
    capability = mint_capability_token()
    write_http_capability(control, capability)
    witness = QualifiedProcessWitness(
        witness_nonce="never-binds",
        server_pid=os.getpid(),
        listen_host="127.0.0.1",
        listen_port=9,
        mcp_url_path="/mcp",
        qualified_for_gate_a=True,
        minted_monotonic=time.monotonic(),
        settle_observed_seconds=90.0,
        order_id=INTERNAL_STAGE_ORDER_ID,
    )
    write_qualified_witness(control, witness)
    with pytest.raises(
        (ProcessWitnessError, LoopbackTransportNotReady),
        match=r"timed out|not accepting|not ready",
    ):
        qualify_process_witness_transport(
            witness,
            capability,
            startup_retry_seconds=0.5,
        )


def test_probe_rejects_false_ready_invalid_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = TransportSecurityMiddleware(
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1"],
        ),
    )
    assert middleware._validate_host("127.0.0.1:9") is False

    def fake_get(_url: str, _headers: dict[str, str], *, timeout_seconds: float = 10.0) -> int:
        return 421

    monkeypatch.setattr(
        "dev.factory.gate_a_mcp.process_witness._http_get_status",
        fake_get,
    )
    with pytest.raises(ProcessWitnessError, match="421"):
        probe_authenticated_loopback_mcp_transport(
            "http://127.0.0.1:9/mcp",
            "unused-token",
        )
