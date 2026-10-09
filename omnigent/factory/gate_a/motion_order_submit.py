"""Motion Core draft order submit bridge for factory Gate A real chat (local beta)."""

from __future__ import annotations

import hashlib
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
    _minimal_node_env,
    _require_bool,
    _require_safe_status_token,
    _require_str_field,
    motion_order_pins_configured,
    resolve_motion_order_cli,
    resolve_motion_order_pins,
    resolve_node_executable,
)
from omnigent.factory.gate_a.real_chat import (
    _assert_under_root,
    _require_absolute_dir,
    validate_operator_task_id,
)

MOTION_TASK_CONTRACTS_DIR_ENV = "OMNIGENT_FACTORY_MOTION_TASK_CONTRACTS_DIR"
MOTION_ORDER_SUBMIT_ENABLE_ENV = "OMNIGENT_FACTORY_MOTION_ORDER_SUBMIT"

_MAX_TASK_CONTRACT_CHARS = 64 * 1024

_TASK_CONTRACT_REQUIRED_STRING_KEYS = frozenset(
    {
        "idempotency_key",
        "client",
        "repo",
        "worktree",
        "branch",
        "objective",
        "scope",
        "acceptance",
        "authority_ref",
    }
)
_TASK_CONTRACT_POLICY_KEYS = frozenset(
    {
        "gates",
        "base_sha",
        "human_gates",
        "review",
        "non_goals",
    }
)
_TASK_CONTRACT_ALLOWED_KEYS = (
    _TASK_CONTRACT_REQUIRED_STRING_KEYS | _TASK_CONTRACT_POLICY_KEYS | frozenset({"brief_hash"})
)

_MAX_POLICY_ARRAY_LEN = 20
_MAX_POLICY_LINE_LEN = 512
_BASE_SHA_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_POLICY_WORK_ORDER_MARKER_RE = re.compile(r"#\s*work\s+order", re.IGNORECASE)
_POLICY_REVIEW_BOLD_FIELD_RE = re.compile(r"^\*\*[A-Z][A-Z0-9_ ]*:\*\*")

_GITHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CANONICAL_CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_BRIEF_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

_CORE_EXIT_IDEMPOTENCY_CONFLICT = 4
_CORE_EXIT_IDEMPOTENCY_AMBIGUOUS = 5

MotionOrderSubmitSubprocessRunner = Callable[
    [list[str], dict[str, str], str],
    subprocess.CompletedProcess[str],
]


def motion_order_submit_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else dict(os.environ)
    if env.get(MOTION_ORDER_SUBMIT_ENABLE_ENV, "").strip() != "1":
        return False
    if not motion_order_pins_configured(env):
        return False
    return bool(env.get(MOTION_TASK_CONTRACTS_DIR_ENV, "").strip())


def _submit_disabled_message() -> str:
    return (
        f"{MOTION_ORDER_SUBMIT_ENABLE_ENV}=1, Motion Core pin env vars, and "
        f"{MOTION_TASK_CONTRACTS_DIR_ENV} are required for order submit"
    )


def resolve_motion_task_contracts_root(environ: dict[str, str] | None = None) -> Path:
    env = dict(environ) if environ is not None else dict(os.environ)
    raw = env.get(MOTION_TASK_CONTRACTS_DIR_ENV, "").strip()
    if not raw:
        raise ValueError(f"{MOTION_TASK_CONTRACTS_DIR_ENV} is required for order submit")
    return _require_absolute_dir(raw, MOTION_TASK_CONTRACTS_DIR_ENV)


def resolve_task_contract_path(
    task_id: str,
    environ: dict[str, str] | None = None,
) -> Path:
    validate_operator_task_id(task_id)
    contracts_root = resolve_motion_task_contracts_root(environ)
    contract_path = _assert_under_root(
        contracts_root / f"{task_id}.json",
        contracts_root,
        label="task contract path",
    )
    if not contract_path.is_file():
        raise ValueError(f"task contract not found for task_id {task_id!r}")
    return contract_path


def _normalize_client_id(raw: str) -> str:
    text = raw.strip().lower()
    return re.sub(r"[^a-z0-9._-]", "", re.sub(r"\s+", "-", text))


def _require_canonical_client(client: str) -> str:
    if not isinstance(client, str) or not client.strip():
        raise ValueError("task contract client must be a non-empty string")
    if client != client.strip():
        raise ValueError("task contract client must be a canonical client id")
    if _normalize_client_id(client) != client:
        raise ValueError("task contract client must be a canonical client id")
    if not _CANONICAL_CLIENT_RE.fullmatch(client):
        raise ValueError("task contract client must be a canonical client id")
    return client


def expected_structured_order_id(client_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{client_id}\0{idempotency_key}".encode()).hexdigest()
    return f"struct-{digest[:32]}"


def _is_single_line_text(value: str) -> bool:
    return "\n" not in value and "\r" not in value


def _validate_policy_list_line(text: str) -> None:
    stripped = text.strip()
    if _POLICY_WORK_ORDER_MARKER_RE.search(stripped):
        raise ValueError("task contract policy is invalid")
    if not stripped:
        return
    if stripped[0] in "-*#>`":
        raise ValueError("task contract policy is invalid")
    if stripped[0].isdigit() and len(stripped) > 1 and stripped[1] in ".)":
        raise ValueError("task contract policy is invalid")


def _validate_policy_review_content(text: str) -> None:
    stripped = text.strip()
    if _POLICY_WORK_ORDER_MARKER_RE.search(stripped):
        raise ValueError("task contract policy is invalid")
    if stripped.startswith("# "):
        raise ValueError("task contract policy is invalid")
    if stripped.startswith("---"):
        raise ValueError("task contract policy is invalid")
    if _POLICY_REVIEW_BOLD_FIELD_RE.match(stripped):
        raise ValueError("task contract policy is invalid")


def _validate_policy_string_array(value: Any, *, allow_empty: bool) -> None:
    if not isinstance(value, list):
        raise ValueError("task contract policy is invalid")
    if not allow_empty and len(value) == 0:
        raise ValueError("task contract policy is invalid")
    if len(value) > _MAX_POLICY_ARRAY_LEN:
        raise ValueError("task contract policy is invalid")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("task contract policy is invalid")
        if not _is_single_line_text(item):
            raise ValueError("task contract policy is invalid")
        if len(item) > _MAX_POLICY_LINE_LEN:
            raise ValueError("task contract policy is invalid")
        _validate_policy_list_line(item)


def _validate_policy_review(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task contract policy is invalid")
    if not _is_single_line_text(value):
        raise ValueError("task contract policy is invalid")
    if len(value) > _MAX_POLICY_LINE_LEN:
        raise ValueError("task contract policy is invalid")
    _validate_policy_review_content(value)


def _validate_policy_base_sha(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("task contract policy is invalid")
    if not _BASE_SHA_RE.fullmatch(value.strip()):
        raise ValueError("task contract policy is invalid")


def _validate_task_contract_policy(data: dict[str, Any]) -> None:
    _validate_policy_string_array(data.get("gates"), allow_empty=False)
    _validate_policy_base_sha(data.get("base_sha"))
    _validate_policy_string_array(data.get("human_gates"), allow_empty=False)
    _validate_policy_review(data.get("review"))
    _validate_policy_string_array(data.get("non_goals"), allow_empty=True)


def _validate_task_contract_object(data: dict[str, Any], task_id: str) -> str:
    unknown = set(data) - _TASK_CONTRACT_ALLOWED_KEYS
    if unknown:
        raise ValueError("task contract has unsupported fields")
    for key in _TASK_CONTRACT_REQUIRED_STRING_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"task contract {key} must be a non-empty string")
    if data["idempotency_key"] != task_id:
        raise ValueError("task contract idempotency_key does not match task_id")
    client_id = _require_canonical_client(data["client"])
    if not _GITHUB_SLUG_RE.fullmatch(data["repo"].strip()):
        raise ValueError("task contract repo must be a GitHub owner/repo slug")
    worktree = data["worktree"].strip()
    if not Path(worktree).is_absolute():
        raise ValueError("task contract worktree must be an absolute path")
    brief_hash = data.get("brief_hash")
    if brief_hash is not None:
        if not isinstance(brief_hash, str) or not _BRIEF_HASH_RE.fullmatch(brief_hash.strip()):
            raise ValueError("task contract brief_hash must be 64 lowercase hex chars")
    _validate_task_contract_policy(data)
    return client_id


def load_task_contract_bytes(task_id: str, environ: dict[str, str] | None = None) -> bytes:
    path = resolve_task_contract_path(task_id, environ)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"task contract is not readable: {exc}") from exc
    if len(raw) > _MAX_TASK_CONTRACT_CHARS:
        raise ValueError("task contract exceeds size limit")
    if not raw.strip():
        raise ValueError("task contract is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("task contract must be UTF-8") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"task contract is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("task contract root must be a JSON object")
    _validate_task_contract_object(data, task_id)
    return raw


def _default_subprocess_runner_with_stdin(
    argv: list[str],
    env: dict[str, str],
    stdin_text: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        input=stdin_text,
        timeout=_SUBPROCESS_TIMEOUT_S,
        env=env,
        check=False,
    )


def format_motion_order_submit_safe_summary(
    *,
    task_id: str,
    order_id: str,
    work_id: str,
    brief_hash: str,
    state: str,
    idempotent_replay: bool,
) -> str:
    submit_status = "idempotent_replay" if idempotent_replay else "created"
    lines = [
        "order_submit_ok: true",
        f"submit_status: {submit_status}",
        f"task_id: {task_id}",
        f"order_id: {order_id}",
        f"work_id: {work_id}",
        f"brief_hash: {brief_hash}",
        f"state: {state}",
        "note: draft order from Motion Core new (no worker started)",
    ]
    return "\n".join(lines) + "\n"


def _require_brief_hash_field(payload: dict[str, Any], field: str) -> str:
    value = _require_str_field(payload, field)
    if not _BRIEF_HASH_RE.fullmatch(value):
        raise ValueError(f"Motion Core order submit {field} must be 64 lowercase hex chars")
    return value


def _parse_motion_order_new_json(
    stdout: str,
    *,
    task_id: str,
    expected_order_id: str,
) -> str:
    stripped = stdout.strip()
    if not stripped:
        raise ValueError("Motion Core order submit returned empty output")
    if len(stripped) > _MAX_STDIO_CHARS:
        raise ValueError("Motion Core order submit output exceeds size limit")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Motion Core order submit is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Motion Core order submit root must be a JSON object")
    if not _require_bool(payload, "ok"):
        raise ValueError("Motion Core order submit ok must be true")
    idempotent = payload.get("idempotent")
    if type(idempotent) is not bool:
        raise ValueError("Motion Core order submit idempotent must be a boolean")
    if payload.get("structured") is not True:
        raise ValueError("Motion Core order submit structured must be true")
    order_id = _require_safe_status_token(_require_str_field(payload, "order_id"), "order_id")
    if order_id != expected_order_id:
        raise ValueError("Motion Core order submit order_id does not match expected stable id")
    work_id = _require_safe_status_token(_require_str_field(payload, "work_id"), "work_id")
    if work_id != order_id:
        raise ValueError("Motion Core order submit work_id must match order_id")
    brief_hash = _require_brief_hash_field(payload, "brief_hash")
    state = _require_safe_status_token(_require_str_field(payload, "state"), "state")
    if state != "new":
        raise ValueError('Motion Core order submit state must be "new"')
    if "order" in payload:
        raise ValueError("Motion Core order submit must not nest order payload")
    return format_motion_order_submit_safe_summary(
        task_id=task_id,
        order_id=order_id,
        work_id=work_id,
        brief_hash=brief_hash,
        state=state,
        idempotent_replay=idempotent,
    )


def _raise_for_submit_exit_code(returncode: int) -> None:
    if returncode == _CORE_EXIT_IDEMPOTENCY_CONFLICT:
        raise ValueError("Motion Core order submit rejected: idempotency conflict")
    if returncode == _CORE_EXIT_IDEMPOTENCY_AMBIGUOUS:
        raise ValueError("Motion Core order submit rejected: idempotency index ambiguous")
    raise ValueError(f"Motion Core order submit exited with code {returncode}")


def submit_motion_order_draft(
    task_id: str,
    environ: dict[str, str] | None = None,
    *,
    subprocess_runner: MotionOrderSubmitSubprocessRunner | None = None,
) -> str:
    if not motion_order_submit_enabled(environ):
        raise ValueError(_submit_disabled_message())
    validate_operator_task_id(task_id)
    env = dict(environ) if environ is not None else dict(os.environ)
    task_bytes = load_task_contract_bytes(task_id, env)
    task_text = task_bytes.decode("utf-8")
    contract = json.loads(task_text)
    if not isinstance(contract, dict):
        raise ValueError("task contract root must be a JSON object")
    client_id = _require_canonical_client(contract["client"])
    expected_order_id = expected_structured_order_id(client_id, task_id)
    core_root, orders_root = resolve_motion_order_pins(env)
    cli_path = resolve_motion_order_cli(core_root)
    path_value = env.get("PATH") or os.environ.get("PATH", "")
    node_executable = resolve_node_executable(path_value)

    argv = [
        str(node_executable),
        str(cli_path),
        "new",
        "--task-stdin",
        "--orders-root",
        str(orders_root),
        "--json",
    ]
    child_env = _minimal_node_env(env)
    runner = subprocess_runner or _default_subprocess_runner_with_stdin
    try:
        completed = runner(argv, child_env, task_text)
    except subprocess.TimeoutExpired:
        raise ValueError("Motion Core order submit timed out") from None
    except OSError as exc:
        raise ValueError(f"Motion Core order submit failed to start: {exc}") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout) > _MAX_STDIO_CHARS or len(stderr) > _MAX_STDIO_CHARS:
        raise ValueError("Motion Core order submit output exceeds size limit")
    if completed.returncode != 0:
        _raise_for_submit_exit_code(completed.returncode)
    return _parse_motion_order_new_json(
        stdout,
        task_id=task_id,
        expected_order_id=expected_order_id,
    )
