"""Offline tests for factory-gate-a harness registration and admission guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from dev.factory.gate_a_trial.prestarted_mcp import PrestartedGateAMcp
from dev.factory.order_scoped.binding import (
    INTERNAL_STAGE_BRIEF_HASH,
    INTERNAL_STAGE_ORDER_ID,
    STAGE_WORKER_ENV,
)
from omnigent.factory.gate_a.admission import (
    FACTORY_GATE_A_CURSOR_CLI_ENV,
    FactoryGateAAdmissionError,
    FactoryWorkOrderAdmission,
    factory_gate_a_enabled,
    load_admission_from_environ,
    reject_external_path_override_env,
    reject_fabricated_witness_env,
    validate_config_hash_drift,
    validate_fixture_scope,
    validate_mcp_payload_against_admission,
    validate_not_expired,
    validate_witness_matches_prestarted,
)
from omnigent.factory.gate_a.cursor_cli_session import run_admitted_session
from omnigent.inner import cursor_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES
from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness
import time


def _sample_admission(
    *,
    expires_at: datetime | None = None,
) -> FactoryWorkOrderAdmission:
    return FactoryWorkOrderAdmission(
        order_id=INTERNAL_STAGE_ORDER_ID,
        brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
    )


def test_factory_gate_a_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STAGE_WORKER_ENV, raising=False)
    monkeypatch.delenv(FACTORY_GATE_A_CURSOR_CLI_ENV, raising=False)
    assert not factory_gate_a_enabled()
    assert "factory-gate-a" not in _HARNESS_MODULES


def test_factory_gate_a_enabled_requires_both_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STAGE_WORKER_ENV, "1")
    monkeypatch.delenv(FACTORY_GATE_A_CURSOR_CLI_ENV, raising=False)
    assert not factory_gate_a_enabled()
    monkeypatch.setenv(FACTORY_GATE_A_CURSOR_CLI_ENV, "1")
    assert factory_gate_a_enabled()


def test_expired_admission_rejects_before_subprocess() -> None:
    admission = _sample_admission(
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )

    def _forbid_spawn(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("subprocess must not spawn for expired admission")

    with patch(
        "dev.factory.gate_a_trial.orchestration.start_bound_mcp",
        side_effect=_forbid_spawn,
    ):
        result = run_admitted_session(admission, pass_cursor_api_key=True)
    assert not result.ok
    assert any("expired" in p.lower() for p in result.problems)


def test_session_rejects_keyless_success() -> None:
    admission = _sample_admission()
    result = run_admitted_session(admission, pass_cursor_api_key=False)
    assert not result.ok
    assert any("PASS_CURSOR_API_KEY" in p for p in result.problems)


def test_config_hash_drift_fail_closed() -> None:
    with pytest.raises(FactoryGateAAdmissionError, match="drift"):
        validate_config_hash_drift(
            {"cli-config.json": "abc123"},
            {"cli-config.json": "different"},
        )


def test_witness_mismatch_against_prestarted() -> None:
    prestarted = PrestartedGateAMcp(
        control_dir=Path("/tmp/control"),
        control_parent=Path("/tmp"),
        witness_nonce="handle-nonce",
        server_pid=1,
        capability_token="tok",
        mcp_url="http://127.0.0.1/mcp",
        _proc=None,
    )
    witness = QualifiedProcessWitness(
        witness_nonce="other-nonce",
        server_pid=1,
        listen_host="127.0.0.1",
        listen_port=9,
        mcp_url_path="/mcp",
        qualified_for_gate_a=True,
        minted_monotonic=time.monotonic(),
        settle_observed_seconds=90.0,
        order_id=INTERNAL_STAGE_ORDER_ID,
    )
    with pytest.raises(FactoryGateAAdmissionError, match="witness_nonce"):
        validate_witness_matches_prestarted(prestarted, witness)


def test_mcp_payload_order_mismatch() -> None:
    admission = _sample_admission()
    with pytest.raises(FactoryGateAAdmissionError, match="order_id"):
        validate_mcp_payload_against_admission(
            admission,
            {
                "order_id": "wrong-order",
                "brief_hash": INTERNAL_STAGE_BRIEF_HASH,
            },
        )


def test_reject_fabricated_witness_env() -> None:
    with pytest.raises(FactoryGateAAdmissionError, match="WITNESS_NONCE"):
        reject_fabricated_witness_env({"HARNESS_FACTORY_GATE_A_WITNESS_NONCE": "forged"})


def test_load_admission_rejects_witness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_BRIEF_HASH", INTERNAL_STAGE_BRIEF_HASH)
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_EXPIRES_AT", "2030-01-01T00:00:00+00:00")
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_WITNESS_NONCE", "forged")
    with pytest.raises(FactoryGateAAdmissionError, match="WITNESS_NONCE"):
        load_admission_from_environ()


def test_load_admission_rejects_workspace_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_BRIEF_HASH", INTERNAL_STAGE_BRIEF_HASH)
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_EXPIRES_AT", "2030-01-01T00:00:00+00:00")
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_WORKSPACE", "/tmp/evil-ws")
    with pytest.raises(FactoryGateAAdmissionError, match="WORKSPACE"):
        load_admission_from_environ()


def test_load_admission_rejects_config_dir_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_BRIEF_HASH", INTERNAL_STAGE_BRIEF_HASH)
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_EXPIRES_AT", "2030-01-01T00:00:00+00:00")
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_CONFIG_DIR", "/tmp/evil-cfg")
    with pytest.raises(FactoryGateAAdmissionError, match="CONFIG_DIR"):
        load_admission_from_environ()


def test_load_admission_rejects_external_config_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_BRIEF_HASH", INTERNAL_STAGE_BRIEF_HASH)
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_EXPIRES_AT", "2030-01-01T00:00:00+00:00")
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_CONFIG_HASHES_JSON", '{"cli-config.json":"x"}')
    with pytest.raises(FactoryGateAAdmissionError, match="CONFIG_HASHES"):
        load_admission_from_environ()


def test_load_admission_minimal_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_BRIEF_HASH", INTERNAL_STAGE_BRIEF_HASH)
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_EXPIRES_AT", "2030-01-01T00:00:00+00:00")
    admission = load_admission_from_environ()
    assert admission.order_id == INTERNAL_STAGE_ORDER_ID
    assert admission.brief_hash == INTERNAL_STAGE_BRIEF_HASH


def test_reject_external_path_override_env_workspace() -> None:
    with pytest.raises(FactoryGateAAdmissionError, match="adapter-owned"):
        reject_external_path_override_env({"HARNESS_FACTORY_GATE_A_WORKSPACE": "/x"})


def test_load_admission_rejects_non_canonical_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_BRIEF_HASH", "wrong-brief-hash")
    monkeypatch.setenv("HARNESS_FACTORY_GATE_A_EXPIRES_AT", "2030-01-01T00:00:00+00:00")
    with pytest.raises(FactoryGateAAdmissionError, match="canonical"):
        load_admission_from_environ()


def test_mcp_payload_brief_hash_mismatch() -> None:
    admission = _sample_admission()
    with pytest.raises(FactoryGateAAdmissionError, match="brief_hash"):
        validate_mcp_payload_against_admission(
            admission,
            {"order_id": INTERNAL_STAGE_ORDER_ID},
        )


def test_fixture_scope_rejects_arbitrary_order() -> None:
    admission = _sample_admission()
    admission = FactoryWorkOrderAdmission(
        order_id="not-the-fixture",
        brief_hash=admission.brief_hash,
        expires_at=admission.expires_at,
    )
    with pytest.raises(FactoryGateAAdmissionError, match="synthetic fixture"):
        validate_fixture_scope(admission)


def test_fixture_scope_rejects_non_canonical_brief() -> None:
    admission = FactoryWorkOrderAdmission(
        order_id=INTERNAL_STAGE_ORDER_ID,
        brief_hash="not-canonical",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with pytest.raises(FactoryGateAAdmissionError, match="canonical"):
        validate_fixture_scope(admission)


def test_cursor_sdk_harness_refuses_factory_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STAGE_WORKER_ENV, "1")
    monkeypatch.setenv(FACTORY_GATE_A_CURSOR_CLI_ENV, "1")
    with pytest.raises(RuntimeError, match="factory-gate-a"):
        cursor_harness._build_cursor_executor()


def test_factory_gate_a_harness_create_app() -> None:
    from omnigent.inner import factory_gate_a_harness

    app = factory_gate_a_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
