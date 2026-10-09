"""Bounded Motion Core order cancellation bridge for factory Gate A (local beta)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable

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

MotionOrderCancelSubprocessRunner = Callable[
    [list[str], dict[str, str]],
    subprocess.CompletedProcess[str],
]


def motion_order_cancel_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else dict(os.environ)
    return env.get(
        MOTION_ORDER_CANCEL_ENABLE_ENV, ""
    ).strip() == "1" and motion_order_pins_configured(env)


def _default_subprocess_runner(
    argv: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        env=env,
        check=False,
    )


def _parse_cancel_json(stdout: str, *, requested_order_id: str) -> tuple[str, bool | None]:
    stripped = stdout.strip()
    if not stripped:
        raise ValueError("Motion Core order cancel returned empty output")
    if len(stripped) > _MAX_STDIO_CHARS:
        raise ValueError("Motion Core order cancel output exceeds size limit")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Motion Core order cancel is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Motion Core order cancel root must be a JSON object")
    if not _require_bool(payload, "ok"):
        raise ValueError("Motion Core order cancel ok must be true")
    order_id = _require_str_field(payload, "order_id")
    if order_id != requested_order_id:
        raise ValueError("Motion Core order cancel order_id does not match requested order_id")
    outcome = _require_str_field(payload, "outcome")
    if outcome not in _CANCEL_OUTCOMES:
        raise ValueError("Motion Core order cancel outcome is unsupported")
    worker_stopped: bool | None = None
    if "worker_stopped" in payload:
        worker_stopped = _require_bool(payload, "worker_stopped")
    return outcome, worker_stopped


def format_motion_order_cancel_safe_summary(
    *, order_id: str, outcome: str, worker_stopped: bool | None
) -> str:
    lines = [
        "order_cancel_ok: true",
        f"outcome: {outcome}",
        f"order_id: {order_id}",
    ]
    if worker_stopped is not None:
        lines.append(f"worker_stopped: {'true' if worker_stopped else 'false'}")
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
        raise ValueError("Motion Core order cancel timed out") from None
    except OSError as exc:
        raise ValueError(f"Motion Core order cancel failed to start: {exc}") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_STDIO_CHARS or len(stderr) > _MAX_STDIO_CHARS:
        raise ValueError("Motion Core order cancel output exceeds size limit")
    if completed.returncode != 0:
        raise ValueError(f"Motion Core order cancel exited with code {completed.returncode}")
    outcome, worker_stopped = _parse_cancel_json(stdout, requested_order_id=order_id)
    return format_motion_order_cancel_safe_summary(
        order_id=order_id,
        outcome=outcome,
        worker_stopped=worker_stopped,
    )
