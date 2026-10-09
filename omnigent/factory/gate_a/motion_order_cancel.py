"""Bounded Motion Core order cancellation bridge for factory Gate A (local beta)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

from omnigent.factory.gate_a.motion_order_status import (
    _MAX_STDIO_CHARS,
    _SUBPROCESS_TIMEOUT_S,
    _minimal_node_env,
    _require_bool,
    _require_str_field,
    motion_order_pins_configured,
    resolve_motion_order_cli,
    resolve_motion_order_pins,
    resolve_node_executable,
    validate_motion_order_id,
)

MOTION_ORDER_CANCEL_ENABLE_ENV = "OMNIGENT_FACTORY_MOTION_ORDER_CANCEL"

_CANCEL_OUTCOMES = frozenset({"cancelled", "cancel_requested", "already_completed"})
_CANCEL_SUBPROCESS_TIMEOUT_S = _SUBPROCESS_TIMEOUT_S

MotionOrderCancelSubprocessRunner = Callable[
    [list[str], dict[str, str]],
    subprocess.CompletedProcess[str],
]


def motion_order_cancel_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else dict(os.environ)
    return env.get(
        MOTION_ORDER_CANCEL_ENABLE_ENV, ""
    ).strip() == "1" and motion_order_pins_configured(env)


def _cancel_outcome_uncertain_message() -> str:
    return (
        "Motion Core order cancel did not complete successfully; "
        "outcome may need inspection via order status"
    )


def _cancel_ambiguous_outcome_message() -> str:
    return (
        "Motion Core order cancel returned an ambiguous outcome; "
        "inspect with order status before retrying"
    )


def _default_subprocess_runner(
    argv: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_CANCEL_SUBPROCESS_TIMEOUT_S,
        env=env,
        check=False,
    )


def _parse_worker_signal_ok(payload: dict[str, Any]) -> bool | None:
    if "worker_signal" not in payload:
        return None
    worker_signal = payload.get("worker_signal")
    if worker_signal is None:
        return None
    if not isinstance(worker_signal, dict):
        raise ValueError("worker_signal invalid")
    ok = worker_signal.get("ok")
    if ok is True:
        return True
    if ok is False:
        return False
    return None


def _parse_cancel_json(stdout: str, *, requested_order_id: str) -> tuple[str, bool | None]:
    stripped = stdout.strip()
    if not stripped:
        raise ValueError("empty output")
    if len(stripped) > _MAX_STDIO_CHARS:
        raise ValueError("output exceeds size limit")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        raise ValueError("invalid json") from None
    if not isinstance(payload, dict):
        raise ValueError("root must be object")
    if not _require_bool(payload, "ok"):
        raise ValueError("ok must be true")
    order_id = _require_str_field(payload, "order_id")
    if order_id != requested_order_id:
        raise ValueError("order_id mismatch")
    outcome = _require_str_field(payload, "outcome")
    if outcome not in _CANCEL_OUTCOMES:
        raise ValueError("outcome unsupported")
    worker_signal_ok = _parse_worker_signal_ok(payload)
    return outcome, worker_signal_ok


def format_motion_order_cancel_safe_summary(
    *, order_id: str, outcome: str, worker_signal_ok: bool | None
) -> str:
    lines = [
        "order_cancel_ok: true",
        f"outcome: {outcome}",
        f"order_id: {order_id}",
    ]
    if worker_signal_ok is True:
        lines.append("worker_signal_ok: true")
    else:
        lines.append("note: cancellation result does not establish worker termination")
    return "\n".join(lines) + "\n"


def cancel_motion_order(
    order_id: str,
    environ: dict[str, str] | None = None,
    *,
    subprocess_runner: MotionOrderCancelSubprocessRunner | None = None,
) -> str:
    if not motion_order_cancel_enabled(environ):
        raise ValueError(
            f"{MOTION_ORDER_CANCEL_ENABLE_ENV}=1 and Motion Core pin env vars are required "
            "for order cancel"
        )
    validate_motion_order_id(order_id)
    env = dict(environ) if environ is not None else dict(os.environ)
    core_root, orders_root = resolve_motion_order_pins(env)
    cli_path = resolve_motion_order_cli(core_root)
    path_value = env.get("PATH") or os.environ.get("PATH", "")
    node_executable = resolve_node_executable(path_value)
    argv = [
        str(node_executable),
        str(cli_path),
        "cancel",
        order_id,
        "--orders-root",
        str(orders_root),
        "--json",
    ]
    runner = subprocess_runner or _default_subprocess_runner
    try:
        completed = runner(argv, _minimal_node_env(env))
    except subprocess.TimeoutExpired:
        raise ValueError(_cancel_outcome_uncertain_message()) from None
    except OSError:
        raise ValueError(_cancel_outcome_uncertain_message()) from None
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_STDIO_CHARS or len(stderr) > _MAX_STDIO_CHARS:
        raise ValueError(_cancel_ambiguous_outcome_message())
    if completed.returncode != 0:
        raise ValueError(_cancel_outcome_uncertain_message())
    try:
        outcome, worker_signal_ok = _parse_cancel_json(stdout, requested_order_id=order_id)
    except ValueError:
        raise ValueError(_cancel_ambiguous_outcome_message()) from None
    return format_motion_order_cancel_safe_summary(
        order_id=order_id,
        outcome=outcome,
        worker_signal_ok=worker_signal_ok,
    )
