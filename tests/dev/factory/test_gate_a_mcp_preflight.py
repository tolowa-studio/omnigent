"""Gate A MCP preflight receipt: single-use, expiry, order binding, replay."""

from __future__ import annotations

import time

import pytest

from dev.factory.gate_a_mcp.preflight import (
    GateAPreflightError,
    consume_preflight_receipt,
    install_preflight_receipt_for_tests,
    reset_preflight_session_for_tests,
)
from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID
from dev.factory.order_scoped.receipt_admission import GATE_A_EXACT_PROBE_NAMES
from dev.factory.seatbelt_fixture.manifest import (
    GATE_A_MIN_SETTLE_SECONDS,
    FixtureReceipt,
    ProbeRecord,
)
from tests.dev.factory.gate_admission_test_support import bind_trusted_gate_receipt_for_admission


def _qualified_receipt(order_id: str = INTERNAL_STAGE_ORDER_ID) -> FixtureReceipt:
    probes = [ProbeRecord(name, True, "ok") for name in sorted(GATE_A_EXACT_PROBE_NAMES)]
    receipt = FixtureReceipt(
        passed=True,
        qualified_for_gate_a=True,
        failure_reason=None,
        order_id=order_id,
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


@pytest.fixture(autouse=True)
def _reset_preflight() -> None:
    reset_preflight_session_for_tests()
    yield
    reset_preflight_session_for_tests()


def test_consume_preflight_happy_path_once() -> None:
    install_preflight_receipt_for_tests(_qualified_receipt())
    first = consume_preflight_receipt(INTERNAL_STAGE_ORDER_ID)
    assert first.qualified_for_gate_a
    with pytest.raises(GateAPreflightError, match="replay"):
        consume_preflight_receipt(INTERNAL_STAGE_ORDER_ID)


def test_consume_preflight_order_binding() -> None:
    install_preflight_receipt_for_tests(_qualified_receipt())
    with pytest.raises(GateAPreflightError, match="order_id mismatch"):
        consume_preflight_receipt("wrong-order-id")


def test_consume_preflight_missing_receipt() -> None:
    with pytest.raises(GateAPreflightError, match="missing"):
        consume_preflight_receipt(INTERNAL_STAGE_ORDER_ID)


def test_consume_preflight_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    install_preflight_receipt_for_tests(_qualified_receipt())
    import dev.factory.gate_a_mcp.preflight as preflight_mod

    monkeypatch.setattr(preflight_mod, "PREFLIGHT_TTL_SECONDS", 0.01)
    time.sleep(0.02)
    with pytest.raises(GateAPreflightError, match="expired"):
        consume_preflight_receipt(INTERNAL_STAGE_ORDER_ID)
