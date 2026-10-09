from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from omnigent.factory.gate_a.motion_order_cancel import (
    MOTION_ORDER_CANCEL_ENABLE_ENV,
    cancel_motion_order,
    motion_order_cancel_enabled,
)
from omnigent.factory.gate_a.motion_order_status import (
    MOTION_CORE_ROOT_ENV,
    MOTION_ORDERS_ROOT_ENV,
)
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_CHAT_ENV,
    OperatorCommand,
    parse_operator_command,
)
from omnigent.inner.datamodel import Message
from omnigent.inner.executor import ExecutorError, TextChunk
from omnigent.inner.factory_gate_a_real_harness import FactoryGateARealExecutor

_ORDER_ID = "ord-fixture-123"


def _cancel_env(tmp_path: Path) -> dict[str, str]:
    core_root = tmp_path / "core"
    cli_path = core_root / "bin" / "motion-order.mjs"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("// injected runner only\n", encoding="utf-8")
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    return {
        MOTION_ORDER_CANCEL_ENABLE_ENV: "1",
        MOTION_CORE_ROOT_ENV: str(core_root),
        MOTION_ORDERS_ROOT_ENV: str(orders_root),
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/must/not/pass",
        "OPENAI_API_KEY": "must-not-pass",
    }


def _completed(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, json.dumps(payload), "")


def test_parse_operator_command_order_cancel_exact() -> None:
    assert parse_operator_command(f"order cancel {_ORDER_ID}") == OperatorCommand(
        kind="order_cancel", task_id=_ORDER_ID
    )
    assert parse_operator_command("order cancel") is None
    assert parse_operator_command(f"order cancel {_ORDER_ID} extra") is None
    assert parse_operator_command(f"ORDER cancel {_ORDER_ID}") is None


def test_cancel_is_disabled_by_default_and_requires_pins(tmp_path: Path) -> None:
    assert not motion_order_cancel_enabled({})
    assert not motion_order_cancel_enabled({MOTION_ORDER_CANCEL_ENABLE_ENV: "1"})
    with pytest.raises(ValueError, match="required for order cancel"):
        cancel_motion_order(_ORDER_ID, {})


@pytest.mark.parametrize("outcome", ["cancelled", "cancel_requested", "already_completed"])
def test_cancel_reports_only_core_outcome_and_uses_bounded_pinned_cli(
    tmp_path: Path, outcome: str
) -> None:
    env = _cancel_env(tmp_path)
    observed: dict[str, object] = {}

    def injected_runner(
        argv: list[str], child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed["env"] = child_env
        return _completed({"ok": True, "order_id": _ORDER_ID, "outcome": outcome})

    summary = cancel_motion_order(_ORDER_ID, env, subprocess_runner=injected_runner)
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[-5:] == [
        "cancel",
        _ORDER_ID,
        "--orders-root",
        env[MOTION_ORDERS_ROOT_ENV],
        "--json",
    ]
    assert "worker_stopped:" not in summary
    assert "does not establish worker termination" in summary
    assert f"outcome: {outcome}" in summary
    assert "HOME" not in observed["env"]
    assert "OPENAI_API_KEY" not in observed["env"]


def test_cancel_reports_worker_stopped_only_when_core_explicitly_says_true(
    tmp_path: Path,
) -> None:
    env = _cancel_env(tmp_path)

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return _completed(
            {
                "ok": True,
                "order_id": _ORDER_ID,
                "outcome": "cancelled",
                "worker_stopped": True,
            }
        )

    assert "worker_stopped: true" in cancel_motion_order(
        _ORDER_ID, env, subprocess_runner=injected_runner
    )


@pytest.mark.parametrize(
    ("order_id", "payload", "message"),
    [
        ("../escape", {"ok": True, "order_id": "../escape", "outcome": "cancelled"}, "order_id"),
        (_ORDER_ID, {"ok": True, "order_id": "another", "outcome": "cancelled"}, "does not match"),
        (
            _ORDER_ID,
            {"ok": True, "order_id": _ORDER_ID, "outcome": "worker_stopped"},
            "unsupported",
        ),
        (
            _ORDER_ID,
            {"ok": True, "order_id": _ORDER_ID, "outcome": "cancelled", "worker_stopped": "yes"},
            "boolean",
        ),
    ],
)
def test_cancel_fails_closed_on_invalid_id_or_core_result(
    tmp_path: Path, order_id: str, payload: object, message: str
) -> None:
    env = _cancel_env(tmp_path)
    calls = 0

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _completed(payload)

    with pytest.raises(ValueError, match=message):
        cancel_motion_order(order_id, env, subprocess_runner=injected_runner)
    if order_id == "../escape":
        assert calls == 0


def test_cancel_timeout_is_bounded_and_sanitized(tmp_path: Path) -> None:
    env = _cancel_env(tmp_path)

    def injected_runner(
        _argv: list[str], _child_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("motion-order", 15)

    with pytest.raises(ValueError, match="order cancel timed out"):
        cancel_motion_order(_ORDER_ID, env, subprocess_runner=injected_runner)


@pytest.mark.asyncio
async def test_executor_order_cancel_calls_local_beta_bridge_without_start_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    observed: list[str] = []

    def injected_cancel(order_id: str) -> str:
        observed.append(order_id)
        return "order_cancel_ok: true\noutcome: cancel_requested\n"

    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.cancel_motion_order", injected_cancel
    )
    events: list[object] = []
    async for event in FactoryGateARealExecutor().run_turn(
        [Message(role="user", content=f"order cancel {_ORDER_ID}")], [], ""
    ):
        events.append(event)
    assert observed == [_ORDER_ID]
    assert any(
        isinstance(event, TextChunk) and "cancel_requested" in event.text for event in events
    )
    assert not any(isinstance(event, ExecutorError) for event in events)
