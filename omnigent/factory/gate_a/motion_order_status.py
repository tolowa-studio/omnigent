"""Read-only Motion Core order status bridge for factory Gate A real chat (local beta)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnigent.factory.gate_a.real_chat import (
    _assert_under_root,
    _require_absolute_dir,
)

MOTION_CORE_ROOT_ENV = "OMNIGENT_FACTORY_MOTION_CORE_ROOT"
MOTION_ORDERS_ROOT_ENV = "OMNIGENT_FACTORY_MOTION_ORDERS_ROOT"

_MOTION_ORDER_ID_MAX_LEN = 128
# Simple slugs and Motion Core IDs with an uppercase timestamp T.
_MOTION_ORDER_ID_SIMPLE_RE = re.compile(
    rf"^[a-z0-9](?:[a-z0-9-]{{0,{_MOTION_ORDER_ID_MAX_LEN - 2}}}[a-z0-9])?$"
)
_MOTION_ORDER_ID_FACTORY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}T\d{6}-[a-f0-9]{8}$")

_STATUS_TOKEN_MAX_LEN = 64
_STATUS_TOKEN_RE = re.compile(
    rf"^[a-zA-Z0-9](?:[a-zA-Z0-9._-]{{0,{_STATUS_TOKEN_MAX_LEN - 2}}}[a-zA-Z0-9._-])?$"
)

_CLI_REL = Path("bin") / "motion-order.mjs"
_SUBPROCESS_TIMEOUT_S = 15.0
_MAX_STDIO_CHARS = 256 * 1024

_NODE_ENV_KEYS = ("PATH", "SystemRoot", "SYSTEMROOT", "PATHEXT")

MotionOrderSubprocessRunner = Callable[
    [list[str], dict[str, str]],
    subprocess.CompletedProcess[str],
]


def validate_motion_order_id(order_id: str) -> None:
    if not order_id or len(order_id) > _MOTION_ORDER_ID_MAX_LEN:
        raise ValueError(
            "order_id must be a single Motion factory order id token "
            f"(max {_MOTION_ORDER_ID_MAX_LEN} chars; no whitespace or path separators)"
        )
    if any(ch in order_id for ch in "/\\\r\n\t "):
        raise ValueError(
            "order_id must be a single Motion factory order id token "
            f"(max {_MOTION_ORDER_ID_MAX_LEN} chars; no whitespace or path separators)"
        )
    if _MOTION_ORDER_ID_FACTORY_RE.fullmatch(order_id):
        return
    if _MOTION_ORDER_ID_SIMPLE_RE.fullmatch(order_id):
        return
    raise ValueError(
        "order_id must be a single Motion factory order id token "
        f"(max {_MOTION_ORDER_ID_MAX_LEN} chars; no whitespace or path separators)"
    )


def motion_order_pins_configured(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else dict(os.environ)
    return bool(env.get(MOTION_CORE_ROOT_ENV, "").strip()) and bool(
        env.get(MOTION_ORDERS_ROOT_ENV, "").strip()
    )


def _missing_pins_message() -> str:
    return (
        f"{MOTION_CORE_ROOT_ENV} and {MOTION_ORDERS_ROOT_ENV} must be set to absolute paths "
        "at host startup for order status"
    )


def resolve_motion_order_pins(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    env = dict(environ) if environ is not None else dict(os.environ)
    core_raw = env.get(MOTION_CORE_ROOT_ENV, "").strip()
    orders_raw = env.get(MOTION_ORDERS_ROOT_ENV, "").strip()
    if not core_raw or not orders_raw:
        raise ValueError(_missing_pins_message())
    core_root = _require_absolute_dir(core_raw, MOTION_CORE_ROOT_ENV)
    orders_root = _require_absolute_dir(orders_raw, MOTION_ORDERS_ROOT_ENV)
    return core_root, orders_root


def resolve_motion_order_cli(core_root: Path) -> Path:
    cli_path = _assert_under_root(core_root / _CLI_REL, core_root, label="motion-order CLI")
    if cli_path.is_symlink():
        raise ValueError(f"motion-order CLI must not be a symlink: {cli_path}")
    if not cli_path.is_file():
        raise ValueError(f"motion-order CLI not found: {cli_path}")
    return cli_path.resolve()


def _minimal_node_env(environ: dict[str, str]) -> dict[str, str]:
    minimal: dict[str, str] = {}
    for key in _NODE_ENV_KEYS:
        value = environ.get(key)
        if value:
            minimal[key] = value
    if "PATH" not in minimal:
        raise ValueError("PATH is required to locate node for Motion Core order status")
    return minimal


def _default_subprocess_runner(
    argv: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    # capture_output buffers full stdout/stderr before return; size limits below are post-capture.
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        env=env,
        check=False,
    )


def _require_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ValueError(f"Motion Core status {key} must be a boolean")
    return value


def _require_str_field(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Motion Core status {key} must be a string")
    return value


def _optional_str_field(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Motion Core status {key} must be a string or null")
    return value


def _require_safe_status_token(value: str, label: str) -> str:
    if len(value) > _STATUS_TOKEN_MAX_LEN:
        raise ValueError(f"Motion Core status {label} exceeds length limit")
    if any(ord(ch) < 32 or ch in "\r\n" for ch in value):
        raise ValueError(f"Motion Core status {label} must be a single-line token")
    if not _STATUS_TOKEN_RE.fullmatch(value):
        raise ValueError(f"Motion Core status {label} contains unsafe characters")
    return value


def _receipt_category_count(receipts: object) -> int:
    if not isinstance(receipts, dict):
        raise ValueError("Motion Core status receipts must be an object")
    return len(receipts)


def resolve_node_executable(path_value: str) -> Path:
    """Pick node from operator PATH; mise shims must be executed via the shim path."""
    node = shutil.which("node", path=path_value)
    if node is None:
        raise ValueError("node executable not found on PATH")
    node_shim = Path(node)
    if not node_shim.is_absolute():
        node_shim = node_shim.absolute()
    node_target = node_shim.resolve()
    if not node_target.is_file():
        raise ValueError(f"node executable not found: {node_target}")
    if not os.access(node_target, os.X_OK):
        raise ValueError(f"node executable is not executable: {node_target}")
    return node_shim


_REPORT_SECTION_UNAVAILABLE = "unavailable"
_REPORT_SECTION_MISMATCH = "mismatch"


def _parse_motion_core_report(report: object, *, requested_order_id: str) -> tuple[bool, int, int]:
    if not isinstance(report, dict):
        raise ValueError("Motion Core status report must be an object")
    report_ok = _require_bool(report, "ok")
    reported_id = _require_str_field(report, "order_id")
    if reported_id != requested_order_id:
        raise ValueError("Motion Core report order_id does not match requested order_id")
    sections = report.get("sections")
    if not isinstance(sections, dict):
        raise ValueError("Motion Core status report sections must be an object")
    unavailable_count = 0
    mismatch_count = 0
    for section in sections.values():
        if not isinstance(section, dict):
            raise ValueError("Motion Core status report section must be an object")
        status = _require_safe_status_token(
            _require_str_field(section, "status"),
            "report.section.status",
        )
        if status == _REPORT_SECTION_UNAVAILABLE:
            unavailable_count += 1
        elif status == _REPORT_SECTION_MISMATCH:
            mismatch_count += 1
    return report_ok, unavailable_count, mismatch_count


def format_motion_order_safe_summary(
    *,
    order_id: str,
    state: str,
    cancellation_phase: str | None,
    receipt_category_count: int,
    report_ok: bool,
    report_unavailable_count: int,
    report_mismatch_count: int,
) -> str:
    phase_display = cancellation_phase if cancellation_phase else "(none)"
    lines = [
        "order_status_ok: true",
        f"report_ok: {'true' if report_ok else 'false'}",
        f"report_unavailable_count: {report_unavailable_count}",
        f"report_mismatch_count: {report_mismatch_count}",
        f"order_id: {order_id}",
        f"state: {state}",
        f"cancellation_phase: {phase_display}",
        f"receipt_category_count: {receipt_category_count}",
        "note: snapshot from Motion Core order status (not live worker state)",
    ]
    return "\n".join(lines) + "\n"


def _parse_motion_order_json(stdout: str, *, requested_order_id: str) -> str:
    stripped = stdout.strip()
    if not stripped:
        raise ValueError("Motion Core status returned empty output")
    if len(stripped) > _MAX_STDIO_CHARS:
        raise ValueError("Motion Core status output exceeds size limit")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Motion Core status is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Motion Core status root must be a JSON object")
    if not _require_bool(payload, "ok"):
        raise ValueError("Motion Core status ok must be true")
    order = payload.get("order")
    if not isinstance(order, dict):
        raise ValueError("Motion Core status order must be an object")
    reported_id = _require_str_field(order, "order_id")
    if reported_id != requested_order_id:
        raise ValueError("Motion Core order_id does not match requested order_id")
    state = _require_safe_status_token(_require_str_field(order, "state"), "order.state")
    raw_phase = _optional_str_field(payload, "cancellation_phase")
    cancellation_phase = (
        _require_safe_status_token(raw_phase, "cancellation_phase")
        if raw_phase is not None
        else None
    )
    receipt_category_count = _receipt_category_count(payload.get("receipts"))
    report_ok, report_unavailable_count, report_mismatch_count = _parse_motion_core_report(
        payload.get("report"),
        requested_order_id=requested_order_id,
    )
    return format_motion_order_safe_summary(
        order_id=reported_id,
        state=state,
        cancellation_phase=cancellation_phase,
        receipt_category_count=receipt_category_count,
        report_ok=report_ok,
        report_unavailable_count=report_unavailable_count,
        report_mismatch_count=report_mismatch_count,
    )


def read_motion_order_status_summary(
    order_id: str,
    environ: dict[str, str] | None = None,
    *,
    subprocess_runner: MotionOrderSubprocessRunner | None = None,
) -> str:
    validate_motion_order_id(order_id)
    env = dict(environ) if environ is not None else dict(os.environ)
    core_root, orders_root = resolve_motion_order_pins(env)
    cli_path = resolve_motion_order_cli(core_root)
    path_value = env.get("PATH") or os.environ.get("PATH", "")
    node_executable = resolve_node_executable(path_value)

    argv = [
        str(node_executable),
        str(cli_path),
        "status",
        order_id,
        "--orders-root",
        str(orders_root),
        "--json",
    ]
    child_env = _minimal_node_env(env)
    runner = subprocess_runner or _default_subprocess_runner
    try:
        completed = runner(argv, child_env)
    except subprocess.TimeoutExpired:
        raise ValueError("Motion Core order status timed out") from None
    except OSError as exc:
        raise ValueError(f"Motion Core order status failed to start: {exc}") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_STDIO_CHARS or len(stderr) > _MAX_STDIO_CHARS:
        raise ValueError("Motion Core status output exceeds size limit")
    if completed.returncode != 0:
        raise ValueError(f"Motion Core order status exited with code {completed.returncode}")
    return _parse_motion_order_json(stdout, requested_order_id=order_id)
