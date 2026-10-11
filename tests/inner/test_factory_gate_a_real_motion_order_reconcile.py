from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from omnigent.factory.gate_a.motion_order_reconcile import (
    MOTION_ORDER_RECONCILE_ENABLE_ENV,
    motion_order_reconcile_enabled,
    reconcile_motion_order,
)
from omnigent.factory.gate_a.motion_order_status import (
    MOTION_CORE_ROOT_ENV,
    MOTION_ORDERS_ROOT_ENV,
    validate_motion_order_id,
)
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_CHAT_ENV,
    OperatorCommand,
    parse_operator_command,
    usage_hint,
)
from omnigent.inner.datamodel import Message
from omnigent.inner.executor import ExecutorError, TextChunk
from omnigent.inner.factory_gate_a_real_harness import FactoryGateARealExecutor

_ORDER_ID = "ord-fixture-123"
_CORE_FACTORY_ORDER_ID = "tol-701-20260928T193739-9afc6322"
_UNCERTAIN = "outcome may need inspection via order status"
_AMBIGUOUS = "inspect with order status before retrying"


def _reconcile_env(tmp_path: Path) -> dict[str, str]:
    core_root = tmp_path / "core"
    cli_path = core_root / "bin" / "motion-order.mjs"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("// injected runner only\n", encoding="utf-8")
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    return {
        MOTION_ORDER_RECONCILE_ENABLE_ENV: "1",
        MOTION_CORE_ROOT_ENV: str(core_root),
        MOTION_ORDERS_ROOT_ENV: str(orders_root),
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/must/not/pass",
        "OPENAI_API_KEY": "must-not-pass",
    }


def _completed(
    payload: object, *, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, json.dumps(payload), stderr)


def test_parse_operator_command_order_reconcile_exact() -> None:
    assert parse_operator_command(f"order reconcile {_ORDER_ID}") == OperatorCommand(
        kind="order_reconcile", task_id=_ORDER_ID
    )
    assert parse_operator_command("order reconcile") is None
    assert parse_operator_command(f"order reconcile {_ORDER_ID} extra") is None
    assert parse_operator_command(f"ORDER reconcile {_ORDER_ID}") is None


def test_usage_hint_documents_order_reconcile() -> None:
    assert "order reconcile <order_id>" in usage_hint()


def test_validate_motion_order_id_accepts_motion_core_factory_shape_for_reconcile() -> None:
    validate_motion_order_id(_CORE_FACTORY_ORDER_ID)


def test_reconcile_accepts_motion_core_factory_order_id_shape(tmp_path: Path) -> None:
    env = _reconcile_env(tmp_path)
    order_id = _CORE_FACTORY_ORDER_ID

    def injected_runner(
        argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert argv[-4] == order_id
        return _completed({"ok": True, "order_id": order_id, "outcome": "unchanged"})

    summary = reconcile_motion_order(order_id, env, subprocess_runner=injected_runner)
    assert f"order_id: {order_id}" in summary


def test_reconcile_is_disabled_by_default_and_requires_pins(tmp_path: Path) -> None:
    assert not motion_order_reconcile_enabled({})
    assert not motion_order_reconcile_enabled({MOTION_ORDER_RECONCILE_ENABLE_ENV: "1"})
    with pytest.raises(ValueError, match="required for order reconcile"):
        reconcile_motion_order(_ORDER_ID, {})


@pytest.mark.parametrize(
    "outcome",
    ["merged", "closed", "unchanged", "no_pr", "lookup_failed", "skipped"],
)
def test_reconcile_reports_core_outcome_and_uses_bounded_pinned_cli(
    tmp_path: Path, outcome: str
) -> None:
    env = _reconcile_env(tmp_path)
    observed: dict[str, object] = {}

    def injected_runner(
        argv: list[str], child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed["env"] = child_env
        return _completed({"ok": True, "order_id": _ORDER_ID, "outcome": outcome})

    summary = reconcile_motion_order(_ORDER_ID, env, subprocess_runner=injected_runner)
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[-5:] == [
        "reconcile",
        _ORDER_ID,
        "--orders-root",
        env[MOTION_ORDERS_ROOT_ENV],
        "--json",
    ]
    assert f"outcome: {outcome}" in summary
    assert "does not establish worker termination" in summary
    if outcome == "lookup_failed":
        assert "lookup_failed: true" in summary
    else:
        assert "lookup_failed: true" not in summary
    assert "cancellation_phase:" not in summary
    assert "HOME" not in observed["env"]
    assert "OPENAI_API_KEY" not in observed["env"]


def test_reconcile_reports_state_transition_when_core_sends_from_and_to_state(
    tmp_path: Path,
) -> None:
    env = _reconcile_env(tmp_path)

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return _completed(
            {
                "ok": True,
                "order_id": _ORDER_ID,
                "outcome": "merged",
                "from_state": "open",
                "to_state": "merged",
            }
        )

    summary = reconcile_motion_order(_ORDER_ID, env, subprocess_runner=injected_runner)
    assert "from_state: open" in summary
    assert "to_state: merged" in summary
    assert "state_changed: true" in summary


def test_reconcile_includes_cancellation_phase_only_when_core_reports_it(
    tmp_path: Path,
) -> None:
    env = _reconcile_env(tmp_path)

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return _completed(
            {
                "ok": True,
                "order_id": _ORDER_ID,
                "outcome": "unchanged",
                "cancellation_phase": "cancel_requested",
            }
        )

    summary = reconcile_motion_order(_ORDER_ID, env, subprocess_runner=injected_runner)
    assert "cancellation_phase: cancel_requested" in summary
    assert "worker_signal" not in summary


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "order_id": "another", "outcome": "merged"},
        {"ok": True, "order_id": _ORDER_ID, "outcome": "changed"},
        {"ok": False, "order_id": _ORDER_ID, "outcome": "merged"},
        {"ok": True, "order_id": _ORDER_ID, "outcome": "merged", "cancellation_phase": "bad\n"},
    ],
)
def test_reconcile_ambiguous_core_json_directs_to_order_status(
    tmp_path: Path, payload: object
) -> None:
    env = _reconcile_env(tmp_path)

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return _completed(payload)

    with pytest.raises(ValueError, match=_AMBIGUOUS):
        reconcile_motion_order(_ORDER_ID, env, subprocess_runner=injected_runner)


@pytest.mark.parametrize(
    ("factory", "expected_fragment"),
    [
        (
            lambda: _completed(
                {"ok": True, "order_id": _ORDER_ID, "outcome": "merged"}, returncode=3
            ),
            _UNCERTAIN,
        ),
        (
            lambda: subprocess.CompletedProcess(
                [],
                3,
                "",
                "secret-path /Users/op stderr",
            ),
            _UNCERTAIN,
        ),
        (
            lambda: subprocess.CompletedProcess([], 0, "not-json-at-all", ""),
            _AMBIGUOUS,
        ),
    ],
)
def test_reconcile_uncertain_or_ambiguous_never_leaks_stderr_or_exit_code(
    tmp_path: Path,
    factory: object,
    expected_fragment: str,
) -> None:
    env = _reconcile_env(tmp_path)

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert callable(factory)
        return factory()

    with pytest.raises(ValueError, match=expected_fragment) as exc_info:
        reconcile_motion_order(_ORDER_ID, env, subprocess_runner=injected_runner)
    message = str(exc_info.value)
    assert "code 3" not in message
    assert "secret-path" not in message


def test_reconcile_fails_closed_on_invalid_id_before_subprocess(tmp_path: Path) -> None:
    env = _reconcile_env(tmp_path)
    calls = 0

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _completed({"ok": True, "order_id": "../escape", "outcome": "merged"})

    with pytest.raises(ValueError, match="order_id"):
        reconcile_motion_order("../escape", env, subprocess_runner=injected_runner)
    assert calls == 0


@pytest.mark.asyncio
async def test_executor_order_reconcile_calls_local_beta_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    observed: list[str] = []

    def injected_reconcile(order_id: str) -> str:
        observed.append(order_id)
        return "order_reconcile_ok: true\noutcome: merged\n"

    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.reconcile_motion_order",
        injected_reconcile,
    )
    events: list[object] = []
    async for event in FactoryGateARealExecutor().run_turn(
        [Message(role="user", content=f"order reconcile {_ORDER_ID}")], [], ""
    ):
        events.append(event)
    assert observed == [_ORDER_ID]
    assert any(
        isinstance(event, TextChunk) and "outcome: merged" in event.text for event in events
    )
    assert not any(isinstance(event, ExecutorError) for event in events)
