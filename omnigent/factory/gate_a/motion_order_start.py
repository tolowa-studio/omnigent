"""Motion Core order start bridge for factory Gate A real chat (local operator beta)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnigent.factory.gate_a.motion_order_status import (
    _MAX_STDIO_CHARS,
    _SUBPROCESS_TIMEOUT_S,
    _require_bool,
    _require_str_field,
    resolve_motion_order_cli,
    resolve_motion_order_pins,
    resolve_node_executable,
    validate_motion_order_id,
)
from omnigent.factory.gate_a.real_chat import (
    _assert_under_root,
    _require_absolute_dir,
)

_STATUS_SUBPROCESS_TIMEOUT_S = _SUBPROCESS_TIMEOUT_S
_START_SUBPROCESS_TIMEOUT_S = 120.0

MOTION_ORDER_START_ENABLE_ENV = "OMNIGENT_FACTORY_MOTION_ORDER_START"
MOTION_ORDER_APPROVALS_DIR_ENV = "OMNIGENT_FACTORY_MOTION_ORDER_APPROVALS_DIR"

_APPROVAL_SCHEMA_ID = "omnigent.factory.motion-order-start-approval.v1"
_STRUCTURED_SUBMIT_SCHEMA_ID = "motion.order.structured-submit.v2"
_MAX_APPROVAL_BYTES = 64 * 1024

_APPROVAL_ALLOWED_KEYS = frozenset(
    {
        "schema_id",
        "approved",
        "order_id",
        "brief_hash",
        "base_sha",
        "approved_by",
        "approval_ref",
    }
)

_BRIEF_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_BASE_SHA_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")

_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "CLOUDSDK_CONFIG",
    "SystemRoot",
    "SYSTEMROOT",
    "PATHEXT",
)
_FORCED_MOTION_ORDER_ENV = {
    "MOTION_ORDER_AUTO_MERGE": "0",
    "MOTION_ORDER_DUPLICATE_CHECK": "enforce",
    "MOTION_ORDER_DETACH": "launchd",
    "MOTION_ORDER_CLIENT_SECRET_SCOPING": "1",
}

MotionOrderStartSubprocessRunner = Callable[
    [list[str], dict[str, str]],
    subprocess.CompletedProcess[str],
]


def motion_order_start_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else dict(os.environ)
    if env.get(MOTION_ORDER_START_ENABLE_ENV, "").strip() != "1":
        return False
    from omnigent.factory.gate_a.motion_order_status import motion_order_pins_configured

    if not motion_order_pins_configured(env):
        return False
    return bool(env.get(MOTION_ORDER_APPROVALS_DIR_ENV, "").strip())


def _start_disabled_message() -> str:
    return (
        f"{MOTION_ORDER_START_ENABLE_ENV}=1, Motion Core pin env vars, and "
        f"{MOTION_ORDER_APPROVALS_DIR_ENV} are required for order start"
    )


def resolve_motion_order_approvals_root(environ: dict[str, str] | None = None) -> Path:
    env = dict(environ) if environ is not None else dict(os.environ)
    raw = env.get(MOTION_ORDER_APPROVALS_DIR_ENV, "").strip()
    if not raw:
        raise ValueError(f"{MOTION_ORDER_APPROVALS_DIR_ENV} is required for order start")
    return _require_absolute_dir(raw, MOTION_ORDER_APPROVALS_DIR_ENV)


def resolve_approval_path(order_id: str, environ: dict[str, str] | None = None) -> Path:
    validate_motion_order_id(order_id)
    approvals_root = resolve_motion_order_approvals_root(environ)
    return _assert_under_root(
        approvals_root / f"{order_id}.json",
        approvals_root,
        label="order start approval path",
    )


def _is_single_line_text(value: str) -> bool:
    return "\n" not in value and "\r" not in value


def _validate_approval_object(data: dict[str, Any], order_id: str) -> tuple[str, str]:
    unknown = set(data) - _APPROVAL_ALLOWED_KEYS
    if unknown:
        raise ValueError("operator order start approval is invalid")
    if data.get("schema_id") != _APPROVAL_SCHEMA_ID:
        raise ValueError("operator order start approval is invalid")
    if data.get("approved") is not True:
        raise ValueError("operator order start approval is invalid")
    approved_order_id = data.get("order_id")
    if not isinstance(approved_order_id, str) or approved_order_id != order_id:
        raise ValueError("operator order start approval is invalid")
    brief_hash = data.get("brief_hash")
    if not isinstance(brief_hash, str) or not _BRIEF_HASH_RE.fullmatch(brief_hash):
        raise ValueError("operator order start approval is invalid")
    base_sha = data.get("base_sha")
    if not isinstance(base_sha, str) or not _BASE_SHA_RE.fullmatch(base_sha):
        raise ValueError("operator order start approval is invalid")
    approved_by = data.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("operator order start approval is invalid")
    if not _is_single_line_text(approved_by):
        raise ValueError("operator order start approval is invalid")
    approval_ref = data.get("approval_ref")
    if not isinstance(approval_ref, str) or not approval_ref.strip():
        raise ValueError("operator order start approval is invalid")
    if not _is_single_line_text(approval_ref):
        raise ValueError("operator order start approval is invalid")
    return brief_hash, base_sha


def load_order_start_approval(
    order_id: str, environ: dict[str, str] | None = None
) -> tuple[str, str]:
    path = resolve_approval_path(order_id, environ)
    if not path.is_file():
        raise ValueError("operator order start approval is not available")
    try:
        raw = path.read_bytes()
    except OSError:
        raise ValueError("operator order start approval is not available") from None
    if len(raw) > _MAX_APPROVAL_BYTES:
        raise ValueError("operator order start approval is invalid")
    if not raw.strip():
        raise ValueError("operator order start approval is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("operator order start approval is invalid") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("operator order start approval is invalid") from None
    if not isinstance(data, dict):
        raise ValueError("operator order start approval is invalid")
    return _validate_approval_object(data, order_id)


def _minimal_core_child_env(environ: dict[str, str]) -> dict[str, str]:
    minimal: dict[str, str] = {}
    for key in _CHILD_ENV_ALLOWLIST:
        value = environ.get(key)
        if value:
            minimal[key] = value
    minimal.update(_FORCED_MOTION_ORDER_ENV)
    if "PATH" not in minimal:
        raise ValueError("PATH is required to locate node for Motion Core order start")
    return minimal


def _subprocess_timeout_for_argv(argv: list[str]) -> float:
    cmd = argv[2] if len(argv) > 2 else ""
    if cmd == "start":
        return _START_SUBPROCESS_TIMEOUT_S
    return _STATUS_SUBPROCESS_TIMEOUT_S


def _default_subprocess_runner(
    argv: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_subprocess_timeout_for_argv(argv),
        env=env,
        check=False,
    )


def _parse_bounded_stdout(stdout: str, *, label: str) -> dict[str, Any]:
    stripped = stdout.strip()
    if not stripped:
        raise ValueError(f"Motion Core {label} returned empty output")
    if len(stripped) > _MAX_STDIO_CHARS:
        raise ValueError(f"Motion Core {label} output exceeds size limit")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        raise ValueError(f"Motion Core {label} is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError(f"Motion Core {label} root must be a JSON object")
    return payload


def _cancellation_active(payload: dict[str, Any]) -> bool:
    marker = payload.get("cancellation_marker")
    if marker is not None:
        return True
    cancel_request = payload.get("cancel_request")
    if cancel_request is not None:
        return True
    phase = payload.get("cancellation_phase")
    if phase is None:
        return True
    if not isinstance(phase, str):
        return True
    normalized = phase.strip().lower()
    if not normalized:
        return True
    return normalized not in ("none", "active")


def _extract_precheck_fields(
    payload: dict[str, Any],
    *,
    order_id: str,
) -> tuple[str, str]:
    if not _require_bool(payload, "ok"):
        raise ValueError("order start precheck failed: Motion Core status not ok")
    order = payload.get("order")
    if not isinstance(order, dict):
        raise ValueError("order start precheck failed: order snapshot unavailable")
    reported_id = _require_str_field(order, "order_id")
    if reported_id != order_id:
        raise ValueError("order start precheck failed: order_id mismatch")
    state = _require_str_field(order, "state")
    if state != "new":
        raise ValueError("order start precheck failed: order is not in new state")
    brief_hash = _require_str_field(order, "brief_hash")
    if not _BRIEF_HASH_RE.fullmatch(brief_hash):
        raise ValueError("order start precheck failed: order snapshot invalid")
    structured = order.get("structured_submit")
    if not isinstance(structured, dict):
        raise ValueError("order start precheck failed: structured submit binding missing")
    schema_id = structured.get("schema_id")
    if schema_id != _STRUCTURED_SUBMIT_SCHEMA_ID:
        raise ValueError("order start precheck failed: structured submit schema mismatch")
    structured_brief_hash = structured.get("brief_hash")
    if not isinstance(structured_brief_hash, str) or not _BRIEF_HASH_RE.fullmatch(
        structured_brief_hash
    ):
        raise ValueError("order start precheck failed: structured submit binding invalid")
    if structured_brief_hash != brief_hash:
        raise ValueError("order start precheck failed: structured brief_hash mismatch")
    policy = structured.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("order start precheck failed: policy binding missing")
    base_sha = policy.get("base_sha")
    if not isinstance(base_sha, str) or not _BASE_SHA_RE.fullmatch(base_sha):
        raise ValueError("order start precheck failed: base_sha mismatch")
    if _cancellation_active(payload):
        raise ValueError("order start precheck failed: cancellation is active")
    return brief_hash, base_sha


def _run_status_precheck(
    order_id: str,
    env: dict[str, str],
    *,
    subprocess_runner: MotionOrderStartSubprocessRunner,
) -> tuple[str, str]:
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
    child_env = _minimal_core_child_env(env)
    try:
        completed = subprocess_runner(argv, child_env)
    except subprocess.TimeoutExpired:
        raise ValueError("order start precheck failed: Motion Core status timed out") from None
    except OSError:
        raise ValueError("order start precheck failed: Motion Core status unavailable") from None
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_STDIO_CHARS or len(stderr) > _MAX_STDIO_CHARS:
        raise ValueError("order start precheck failed: Motion Core status output exceeds limit")
    if completed.returncode != 0:
        code = completed.returncode
        raise ValueError(
            f"order start precheck failed: Motion Core status exited with code {code}"
        )
    payload = _parse_bounded_stdout(stdout, label="order status")
    return _extract_precheck_fields(payload, order_id=order_id)


def _bindings_match(
    *,
    approval_brief_hash: str,
    approval_base_sha: str,
    status_brief_hash: str,
    status_base_sha: str,
) -> None:
    if approval_brief_hash != status_brief_hash:
        raise ValueError("order start precheck failed: brief_hash mismatch")
    if approval_base_sha != status_base_sha:
        raise ValueError("order start precheck failed: base_sha mismatch")


def format_motion_order_start_safe_summary(*, order_id: str, state: str) -> str:
    lines = [
        "order_start_ok: true",
        f"order_id: {order_id}",
        f"state: {state}",
        "launch_submitted: true",
        "note: launch submitted via Motion Core direct path (local operator beta; not auto-merge)",
    ]
    return "\n".join(lines) + "\n"


def _parse_start_success(stdout: str, *, order_id: str) -> str:
    payload = _parse_bounded_stdout(stdout, label="order start")
    if not _require_bool(payload, "ok"):
        raise ValueError("Motion Core order start returned ambiguous outcome")
    reported_id = _require_str_field(payload, "order_id")
    if reported_id != order_id:
        raise ValueError("Motion Core order start returned ambiguous outcome")
    state = _require_str_field(payload, "state")
    if state != "started":
        raise ValueError("Motion Core order start returned ambiguous outcome")
    direct_path = payload.get("direct_path")
    if not isinstance(direct_path, dict):
        raise ValueError("Motion Core order start returned ambiguous outcome")
    mocked = direct_path.get("mocked")
    if mocked is not False:
        raise ValueError("Motion Core order start returned ambiguous outcome")
    detached = direct_path.get("detached")
    if not isinstance(detached, dict):
        raise ValueError("Motion Core order start returned ambiguous outcome")
    if not _require_bool(detached, "ok"):
        raise ValueError("Motion Core order start returned ambiguous outcome")
    mechanism = detached.get("mechanism")
    if mechanism != "launchd":
        raise ValueError("Motion Core order start returned ambiguous outcome")
    return format_motion_order_start_safe_summary(order_id=order_id, state=state)


def _start_outcome_uncertain_message() -> str:
    return (
        "Motion Core order start did not complete successfully; "
        "outcome may need inspection via order status"
    )


def _start_ambiguous_outcome_message() -> str:
    return (
        "Motion Core order start returned an ambiguous outcome; "
        "inspect with order status before retrying"
    )


def start_motion_order(
    order_id: str,
    environ: dict[str, str] | None = None,
    *,
    subprocess_runner: MotionOrderStartSubprocessRunner | None = None,
) -> str:
    if not motion_order_start_enabled(environ):
        raise ValueError(_start_disabled_message())
    validate_motion_order_id(order_id)
    env = dict(environ) if environ is not None else dict(os.environ)
    approval_brief_hash, approval_base_sha = load_order_start_approval(order_id, env)
    runner = subprocess_runner or _default_subprocess_runner
    status_brief_hash, status_base_sha = _run_status_precheck(
        order_id, env, subprocess_runner=runner
    )
    _bindings_match(
        approval_brief_hash=approval_brief_hash,
        approval_base_sha=approval_base_sha,
        status_brief_hash=status_brief_hash,
        status_base_sha=status_base_sha,
    )

    core_root, orders_root = resolve_motion_order_pins(env)
    cli_path = resolve_motion_order_cli(core_root)
    path_value = env.get("PATH") or os.environ.get("PATH", "")
    node_executable = resolve_node_executable(path_value)
    argv = [
        str(node_executable),
        str(cli_path),
        "start",
        order_id,
        "--execute-direct-path",
        "--orders-root",
        str(orders_root),
        "--json",
    ]
    child_env = _minimal_core_child_env(env)
    try:
        completed = runner(argv, child_env)
    except subprocess.TimeoutExpired:
        raise ValueError(_start_outcome_uncertain_message()) from None
    except OSError:
        raise ValueError(_start_outcome_uncertain_message()) from None

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_STDIO_CHARS or len(stderr) > _MAX_STDIO_CHARS:
        raise ValueError(_start_ambiguous_outcome_message())
    if completed.returncode != 0:
        raise ValueError(_start_outcome_uncertain_message())
    try:
        return _parse_start_success(stdout, order_id=order_id)
    except ValueError:
        raise ValueError(_start_ambiguous_outcome_message()) from None
