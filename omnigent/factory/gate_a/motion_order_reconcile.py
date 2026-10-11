"""Bounded Motion Core order reconcile bridge for factory Gate A (local beta)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from omnigent.factory.gate_a.motion_order_status import (
    _MAX_STDIO_CHARS,
    _SUBPROCESS_TIMEOUT_S,
    _minimal_node_env,
    _optional_str_field,
    _require_bool,
    _require_safe_status_token,
    _require_str_field,
    motion_order_pins_configured,
    resolve_motion_order_cli,
    resolve_motion_order_pins,
    resolve_node_executable,
    validate_motion_order_id,
)

MOTION_ORDER_RECONCILE_ENABLE_ENV = "OMNIGENT_FACTORY_MOTION_ORDER_RECONCILE"

_RECONCILE_OUTCOMES = frozenset(
    {"merged", "closed", "unchanged", "no_pr", "lookup_failed", "skipped"}
)
_RECONCILE_SUBPROCESS_TIMEOUT_S = _SUBPROCESS_TIMEOUT_S

MotionOrderReconcileSubprocessRunner = Callable[
    [list[str], dict[str, str]],
    subprocess.CompletedProcess[str],
]


@dataclass(frozen=True)
class _ReconcileParsed:
    outcome: str
    from_state: str | None
    to_state: str | None
    cancellation_phase: str | None


def motion_order_reconcile_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else dict(os.environ)
    return env.get(
        MOTION_ORDER_RECONCILE_ENABLE_ENV, ""
    ).strip() == "1" and motion_order_pins_configured(env)


def _reconcile_outcome_uncertain_message() -> str:
    return (
        "Motion Core order reconcile did not complete successfully; "
        "outcome may need inspection via order status"
    )


def _reconcile_ambiguous_outcome_message() -> str:
    return (
        "Motion Core order reconcile returned an ambiguous outcome; "
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
        timeout=_RECONCILE_SUBPROCESS_TIMEOUT_S,
        env=env,
        check=False,
    )


def _optional_safe_state(payload: dict[str, object], key: str) -> str | None:
    raw = _optional_str_field(payload, key)
    if raw is None:
        return None
    return _require_safe_status_token(raw, key)


def _parse_reconcile_json(stdout: str, *, requested_order_id: str) -> _ReconcileParsed:
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
    if outcome not in _RECONCILE_OUTCOMES:
        raise ValueError("outcome unsupported")
    from_state = _optional_safe_state(payload, "from_state")
    to_state = _optional_safe_state(payload, "to_state")
    raw_phase = _optional_str_field(payload, "cancellation_phase")
    cancellation_phase = (
        _require_safe_status_token(raw_phase, "cancellation_phase")
        if raw_phase is not None
        else None
    )
    return _ReconcileParsed(
        outcome=outcome,
        from_state=from_state,
        to_state=to_state,
        cancellation_phase=cancellation_phase,
    )


def format_motion_order_reconcile_safe_summary(
    *,
    order_id: str,
    outcome: str,
    from_state: str | None,
    to_state: str | None,
    cancellation_phase: str | None,
) -> str:
    lines = [
        "order_reconcile_ok: true",
        f"outcome: {outcome}",
        f"order_id: {order_id}",
    ]
    if from_state is not None:
        lines.append(f"from_state: {from_state}")
    if to_state is not None:
        lines.append(f"to_state: {to_state}")
    if from_state is not None and to_state is not None and from_state != to_state:
        lines.append("state_changed: true")
    if outcome == "lookup_failed":
        lines.append("lookup_failed: true")
        lines.append("note: reconcile could not resolve order; use order status for snapshot")
    if cancellation_phase is not None:
        lines.append(f"cancellation_phase: {cancellation_phase}")
    lines.append("note: reconcile result does not establish worker termination")
    return "\n".join(lines) + "\n"


def reconcile_motion_order(
    order_id: str,
    environ: dict[str, str] | None = None,
    *,
    subprocess_runner: MotionOrderReconcileSubprocessRunner | None = None,
) -> str:
    if not motion_order_reconcile_enabled(environ):
        raise ValueError(
            f"{MOTION_ORDER_RECONCILE_ENABLE_ENV}=1 and Motion Core pin env vars are required "
            "for order reconcile"
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
        "reconcile",
        order_id,
        "--orders-root",
        str(orders_root),
        "--json",
    ]
    runner = subprocess_runner or _default_subprocess_runner
    try:
        completed = runner(argv, _minimal_node_env(env))
    except subprocess.TimeoutExpired:
        raise ValueError(_reconcile_outcome_uncertain_message()) from None
    except OSError:
        raise ValueError(_reconcile_outcome_uncertain_message()) from None
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_STDIO_CHARS or len(stderr) > _MAX_STDIO_CHARS:
        raise ValueError(_reconcile_ambiguous_outcome_message())
    if completed.returncode != 0:
        raise ValueError(_reconcile_outcome_uncertain_message())
    try:
        parsed = _parse_reconcile_json(stdout, requested_order_id=order_id)
    except ValueError:
        raise ValueError(_reconcile_ambiguous_outcome_message()) from None
    return format_motion_order_reconcile_safe_summary(
        order_id=order_id,
        outcome=parsed.outcome,
        from_state=parsed.from_state,
        to_state=parsed.to_state,
        cancellation_phase=parsed.cancellation_phase,
    )
