"""Operator chat commands for the opt-in Gate A real-task runner (local beta)."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dev.factory.gate_a_real.deliverables import collect_deliverables
from dev.factory.gate_a_real.orchestration import (
    RealTaskRunOptions,
    RealTaskRunResult,
    real_task_gate_enabled,
    run_real_task_gate,
)
from dev.factory.gate_a_real.receipt import RealTaskReceipt
from dev.factory.gate_a_real.spec import RealTaskSpecError, load_real_task_spec
from omnigent.inner.datamodel import Message
from omnigent.inner.executor import ExecutorEvent, TextChunk

REAL_TASK_CHAT_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_CHAT"
REAL_TASK_SPEC_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_SPEC"
REAL_TASK_ARTIFACTS_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS"
REAL_TASK_SPEC_DIR_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_SPEC_DIR"
REAL_TASK_ARTIFACTS_ROOT_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT"

_OPERATOR_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RECEIPT_MANIFEST_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

RUN_APPROVED_PREFIX = "run approved task "
REVIEW_APPROVED_PREFIX = "review approved task "
ORDER_SUBMIT_PREFIX = "order submit "
ORDER_START_PREFIX = "order start "
ORDER_CANCEL_PREFIX = "order cancel "
ORDER_STATUS_PREFIX = "order status "
STATUS_PREFIX = "status "

_HEARTBEAT_INTERVAL_S = 30.0


@dataclass(frozen=True)
class OperatorCommand:
    kind: Literal[
        "run", "review", "status", "order_status", "order_submit", "order_start", "order_cancel"
    ]
    task_id: str


def factory_gate_a_real_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else dict(os.environ)
    return real_task_gate_enabled(env) and env.get(REAL_TASK_CHAT_ENV, "").strip() == "1"


RECEIPT_SCHEMA = "omnigent.factory.gate_a_real.receipt/v1"


def _text_from_content_block(block: object) -> str:
    if not isinstance(block, dict):
        return ""
    block_type = block.get("type")
    if block_type not in ("text", "input_text"):
        return ""
    text = block.get("text")
    return text if isinstance(text, str) else ""


def _user_content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _text_from_content_block(content)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            part = _text_from_content_block(block)
            if part:
                parts.append(part)
        return "".join(parts)
    return ""


def _message_role(message: object) -> str | None:
    if isinstance(message, Message):
        return message.role
    if isinstance(message, dict):
        role = message.get("role")
        return role if isinstance(role, str) else None
    return None


def _message_content(message: object) -> object | None:
    if isinstance(message, Message):
        return message.content
    if isinstance(message, dict):
        if "content" not in message:
            return None
        return message["content"]
    return None


def latest_user_message_text(messages: Sequence[Message | dict[str, object]]) -> str:
    for message in reversed(messages):
        if _message_role(message) != "user":
            continue
        content = _message_content(message)
        if content is None:
            continue
        return _user_content_to_text(content)
    return ""


def parse_operator_command(text: str) -> OperatorCommand | None:
    stripped = text.strip()
    if stripped.startswith(ORDER_SUBMIT_PREFIX):
        task_id = stripped[len(ORDER_SUBMIT_PREFIX) :].strip()
        if task_id and " " not in task_id:
            return OperatorCommand(kind="order_submit", task_id=task_id)
    if stripped.startswith(ORDER_START_PREFIX):
        order_id = stripped[len(ORDER_START_PREFIX) :].strip()
        if order_id and " " not in order_id:
            return OperatorCommand(kind="order_start", task_id=order_id)
    if stripped.startswith(ORDER_CANCEL_PREFIX):
        order_id = stripped[len(ORDER_CANCEL_PREFIX) :].strip()
        if order_id and " " not in order_id:
            return OperatorCommand(kind="order_cancel", task_id=order_id)
    if stripped.startswith(ORDER_STATUS_PREFIX):
        order_id = stripped[len(ORDER_STATUS_PREFIX) :].strip()
        if order_id and " " not in order_id:
            return OperatorCommand(kind="order_status", task_id=order_id)
    if stripped.startswith(RUN_APPROVED_PREFIX):
        task_id = stripped[len(RUN_APPROVED_PREFIX) :].strip()
        if task_id and " " not in task_id:
            return OperatorCommand(kind="run", task_id=task_id)
    if stripped.startswith(REVIEW_APPROVED_PREFIX):
        task_id = stripped[len(REVIEW_APPROVED_PREFIX) :].strip()
        if task_id and " " not in task_id:
            return OperatorCommand(kind="review", task_id=task_id)
    if stripped.startswith(STATUS_PREFIX):
        task_id = stripped[len(STATUS_PREFIX) :].strip()
        if task_id and " " not in task_id:
            return OperatorCommand(kind="status", task_id=task_id)
    return None


def validate_operator_task_id(task_id: str) -> None:
    if not _OPERATOR_TASK_ID_RE.fullmatch(task_id):
        raise ValueError(
            "task_id must be a single token of letters, digits, '.', '_', or '-' "
            "(no whitespace or path separators)"
        )


def _reject_symlink_along_path(path: Path, *, under_root: Path | None = None) -> None:
    """Reject symlinks on existing components along a lexical path (before resolve).

    Path checks assume operator-controlled pinned directories at host startup; they
    do not provide race-free isolation against concurrent symlink creation.
    """
    if under_root is not None:
        if under_root.is_symlink():
            raise ValueError(f"symlink not allowed under pinned root: {under_root}")
        try:
            relative = path.relative_to(under_root)
        except ValueError:
            return
        cursor = under_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"symlink not allowed under pinned root: {cursor}")
        return
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"symlink not allowed: {cursor}")


def _require_absolute_dir(raw: str, label: str, *, must_exist: bool = True) -> Path:
    value = raw.strip()
    if not value:
        raise ValueError(f"{label} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    _reject_symlink_along_path(path)
    resolved = path.resolve()
    if must_exist and not resolved.is_dir():
        raise ValueError(f"{label} is not a directory: {resolved}")
    return resolved


def _assert_under_root(path: Path, root: Path, *, label: str) -> Path:
    root_resolved = root.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError(f"{label} escapes pinned root {root_resolved}") from None
    _reject_symlink_along_path(path, under_root=root)
    resolved = path.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"{label} escapes pinned root {root_resolved}")
    return resolved


def _binding_mode(env: dict[str, str]) -> Literal["single", "registry"]:
    spec_file = env.get(REAL_TASK_SPEC_ENV, "").strip()
    spec_dir = env.get(REAL_TASK_SPEC_DIR_ENV, "").strip()
    artifacts_file = env.get(REAL_TASK_ARTIFACTS_ENV, "").strip()
    artifacts_root = env.get(REAL_TASK_ARTIFACTS_ROOT_ENV, "").strip()
    if spec_file and spec_dir:
        raise ValueError(
            f"set either {REAL_TASK_SPEC_ENV} (single-spec) or "
            f"{REAL_TASK_SPEC_DIR_ENV} (registry), not both"
        )
    if artifacts_file and artifacts_root:
        raise ValueError(
            f"set either {REAL_TASK_ARTIFACTS_ENV} (single-spec) or "
            f"{REAL_TASK_ARTIFACTS_ROOT_ENV} (registry), not both"
        )
    if spec_file:
        return "single"
    if spec_dir:
        return "registry"
    raise ValueError(
        f"{REAL_TASK_SPEC_ENV} or {REAL_TASK_SPEC_DIR_ENV} is required "
        f"(operator-pinned binding at host startup)"
    )


def resolve_bound_paths(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    """Single-spec compatibility binding (one spec file + one artifacts directory)."""
    env = dict(environ) if environ is not None else dict(os.environ)
    if _binding_mode(env) != "single":
        raise ValueError(
            f"{REAL_TASK_SPEC_ENV} and {REAL_TASK_ARTIFACTS_ENV} are required "
            f"for single-spec binding"
        )
    spec_raw = env.get(REAL_TASK_SPEC_ENV, "")
    artifacts_raw = env.get(REAL_TASK_ARTIFACTS_ENV, "")
    if not artifacts_raw.strip():
        raise ValueError(f"{REAL_TASK_ARTIFACTS_ENV} is required")
    spec_path = Path(spec_raw.strip()).expanduser()
    if not spec_path.is_absolute():
        raise ValueError(f"{REAL_TASK_SPEC_ENV} must be an absolute path")
    _reject_symlink_along_path(spec_path)
    spec_path = spec_path.resolve()
    if not spec_path.is_file():
        raise ValueError(f"spec file not found: {spec_path}")
    artifacts_dir = _require_absolute_dir(
        artifacts_raw,
        REAL_TASK_ARTIFACTS_ENV,
        must_exist=False,
    )
    return spec_path, artifacts_dir


def _resolve_registry_roots(env: dict[str, str]) -> tuple[Path, Path]:
    spec_dir_raw = env.get(REAL_TASK_SPEC_DIR_ENV, "").strip()
    artifacts_root_raw = env.get(REAL_TASK_ARTIFACTS_ROOT_ENV, "").strip()
    if not spec_dir_raw:
        raise ValueError(f"{REAL_TASK_SPEC_DIR_ENV} is required for registry binding")
    if not artifacts_root_raw:
        raise ValueError(f"{REAL_TASK_ARTIFACTS_ROOT_ENV} is required for registry binding")
    spec_root = _require_absolute_dir(spec_dir_raw, REAL_TASK_SPEC_DIR_ENV)
    artifacts_root = _require_absolute_dir(artifacts_root_raw, REAL_TASK_ARTIFACTS_ROOT_ENV)
    return spec_root, artifacts_root


def resolve_task_paths(
    task_id: str,
    environ: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Resolve spec and artifacts paths for an operator command task_id."""
    validate_operator_task_id(task_id)
    env = dict(environ) if environ is not None else dict(os.environ)
    mode = _binding_mode(env)
    if mode == "single":
        return resolve_bound_paths(env)
    spec_root, artifacts_root = _resolve_registry_roots(env)
    spec_path = _assert_under_root(spec_root / f"{task_id}.json", spec_root, label="spec path")
    if not spec_path.is_file():
        raise ValueError(f"spec file not found for task_id {task_id!r}: {spec_path}")
    artifacts_dir = _assert_under_root(
        artifacts_root / task_id,
        artifacts_root,
        label="artifacts path",
    )
    return spec_path, artifacts_dir


def format_safe_summary(
    receipt: RealTaskReceipt,
    *,
    receipt_path: Path,
    note: str = ("note: snapshot from receipt.json only (not a live artifact verification)"),
) -> str:
    lines = [
        f"receipt_ok: {receipt.ok}",
        f"task_id: {receipt.task_id}",
        f"receipt: {receipt_path}",
        f"problem_count: {len(receipt.problems)}",
        f"freeze_manifest: {receipt.freeze_manifest_path or '(none)'}",
        f"omnigent_session_id: {receipt.omnigent_session_id or '(none)'}",
        f"builder_session_ids: {', '.join(receipt.builder_session_ids) or '(none)'}",
        f"review_session_ids: {', '.join(receipt.review_session_ids) or '(none)'}",
        f"builder_exit_code: {receipt.builder_exit_code}",
        f"review_exit_code: {receipt.review_exit_code}",
        f"verify_exit_code: {receipt.verify_exit_code}",
        f"review_pass: {receipt.review_pass}",
        note,
    ]
    return "\n".join(lines) + "\n"


def _require_bool(data: dict[str, object], key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ValueError(f"receipt {key} must be a boolean")
    return value


def _require_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"receipt {key} must be a string")
    return value


def _require_str_list(data: dict[str, object], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"receipt {key} must be a list")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"receipt {key} must contain only strings")
    return value


def _require_int_or_none(data: dict[str, object], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"receipt {key} must be an integer or null")
    return value


def _require_str_or_none(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"receipt {key} must be a string or null")
    return value


def _optional_str_field(data: dict[str, object], key: str) -> str | None:
    if key not in data:
        return None
    return _require_str_or_none(data, key)


def _require_receipt_manifest_sha256(value: str | None, label: str) -> str:
    if not value:
        raise ValueError(f"receipt ok=true requires {label}")
    if not _RECEIPT_MANIFEST_SHA256_RE.fullmatch(value):
        raise ValueError(f"receipt ok=true requires {label} to be 64 lowercase hex chars")
    return value


def _reject_receipt_ok_success_evidence_mismatch(receipt: RealTaskReceipt) -> None:
    """Fail closed when receipt.ok claims success but snapshot fields disagree."""
    if not receipt.ok:
        return
    if receipt.problems:
        raise ValueError("receipt ok=true requires problems to be empty")
    if not receipt.review_pass:
        raise ValueError("receipt ok=true requires review_pass true")
    if receipt.builder_exit_code != 0:
        raise ValueError("receipt ok=true requires builder_exit_code 0")
    if receipt.review_exit_code != 0:
        raise ValueError("receipt ok=true requires review_exit_code 0")
    if receipt.verify_exit_code != 0:
        raise ValueError("receipt ok=true requires verify_exit_code 0")
    if not receipt.builder_session_ids:
        raise ValueError("receipt ok=true requires nonempty builder_session_ids")
    if not receipt.review_session_ids:
        raise ValueError("receipt ok=true requires nonempty review_session_ids")
    if not receipt.freeze_manifest_path:
        raise ValueError("receipt ok=true requires freeze_manifest_path")
    deliverable = _require_receipt_manifest_sha256(
        receipt.deliverable_manifest_sha256,
        "deliverable_manifest_sha256",
    )
    post_review = _require_receipt_manifest_sha256(
        receipt.post_review_manifest_sha256,
        "post_review_manifest_sha256",
    )
    if deliverable != post_review:
        raise ValueError(
            "receipt ok=true requires deliverable_manifest_sha256 "
            "and post_review_manifest_sha256 to match"
        )


def _verify_workspace_deliverables_match_receipt(
    spec_workspace: Path,
    deliverable_paths: tuple[str, ...],
    receipt: RealTaskReceipt,
) -> None:
    """Rehash bound workspace deliverables; ok=true status requires a live match."""
    try:
        inventory = collect_deliverables(spec_workspace, deliverable_paths)
    except FileNotFoundError as exc:
        raise ValueError(f"status deliverable check failed: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"status deliverable check failed: {exc}") from exc
    except OSError as exc:
        raise ValueError(
            f"status deliverable check failed: deliverable unreadable: {exc}"
        ) from exc
    expected = receipt.deliverable_manifest_sha256
    if inventory.manifest_sha256 != expected:
        raise ValueError(
            "status deliverable check failed: current workspace manifest_sha256 "
            "does not match receipt deliverable_manifest_sha256"
        )


def _load_receipt_file(path: Path) -> RealTaskReceipt:
    if not path.is_file():
        raise ValueError(f"receipt not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"receipt is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("receipt root must be a JSON object")
    schema = data.get("schema")
    if schema != RECEIPT_SCHEMA:
        raise ValueError(f"receipt schema must be {RECEIPT_SCHEMA!r}")
    return RealTaskReceipt(
        ok=_require_bool(data, "ok"),
        task_id=_require_str(data, "task_id"),
        spec_sha256=_require_str(data, "spec_sha256"),
        workspace=_require_str(data, "workspace"),
        problems=_require_str_list(data, "problems"),
        builder_session_ids=_require_str_list(data, "builder_session_ids"),
        review_session_ids=_require_str_list(data, "review_session_ids"),
        builder_exit_code=_require_int_or_none(data, "builder_exit_code"),
        review_exit_code=_require_int_or_none(data, "review_exit_code"),
        verify_exit_code=_require_int_or_none(data, "verify_exit_code"),
        deliverable_manifest_sha256=_require_str_or_none(data, "deliverable_manifest_sha256"),
        post_review_manifest_sha256=_require_str_or_none(data, "post_review_manifest_sha256"),
        freeze_manifest_path=_require_str_or_none(data, "freeze_manifest_path"),
        review_pass=_require_bool(data, "review_pass"),
        completed_at=_require_str(data, "completed_at"),
        omnigent_session_id=_optional_str_field(data, "omnigent_session_id"),
    )


def read_status_summary(
    *,
    spec_path: Path,
    artifacts_dir: Path,
    task_id: str,
) -> str:
    spec = load_real_task_spec(spec_path)
    if spec.task_id != task_id:
        raise ValueError(f"task_id {task_id!r} does not match bound spec task_id {spec.task_id!r}")
    receipt_path = artifacts_dir / "receipt.json"
    receipt = _load_receipt_file(receipt_path)
    if receipt.task_id != task_id:
        raise ValueError(
            f"receipt task_id {receipt.task_id!r} does not match requested {task_id!r}"
        )
    if receipt.spec_sha256 != spec.spec_sha256:
        raise ValueError("receipt spec_sha256 does not match bound spec")
    if receipt.workspace != str(spec.workspace):
        raise ValueError("receipt workspace does not match bound spec")
    _reject_receipt_ok_success_evidence_mismatch(receipt)
    if receipt.ok:
        _verify_workspace_deliverables_match_receipt(
            Path(spec.workspace),
            spec.deliverable_paths,
            receipt,
        )
        note = (
            "note: status verifies spec/receipt binding and rehashes workspace "
            "deliverables against deliverable_manifest_sha256; session IDs, exit "
            "codes, and freeze path remain historical receipt claims"
        )
    else:
        note = "note: snapshot from receipt.json (failed run; no live deliverable verification)"
    return format_safe_summary(receipt, receipt_path=receipt_path, note=note)


def usage_hint() -> str:
    return (
        "Gate A real-task chat (beta). Commands:\n"
        "  run approved task <task_id>\n"
        "  review approved task <task_id>\n"
        "  status <task_id>\n"
        "  order status <order_id>  (Motion Core snapshot; requires motion pin env vars)\n"
        "  order submit <task_id>  (Motion Core draft new; opt-in submit + contracts dir)\n"
        "  order start <order_id>  (Motion Core launch; opt-in start + local approval file)\n"
        "  order cancel <order_id>  (Motion Core cancel; explicit local-beta opt-in)\n"
        "Spec and artifacts roots are pinned by operator env vars at host startup; "
        "the model cannot override paths or task binding.\n"
    )


async def run_approved_task_with_heartbeat(
    spec_path: Path,
    artifacts_dir: Path,
    task_id: str,
    *,
    review_only: bool = False,
    omnigent_session_id: str | None = None,
) -> AsyncIterator[ExecutorEvent]:
    try:
        spec = load_real_task_spec(spec_path)
    except RealTaskSpecError as exc:
        from omnigent.inner.executor import ExecutorError

        yield ExecutorError(message=str(exc))
        return
    if spec.task_id != task_id:
        from omnigent.inner.executor import ExecutorError

        yield ExecutorError(
            message=(f"task_id {task_id!r} does not match bound spec task_id {spec.task_id!r}")
        )
        return

    if review_only:
        yield TextChunk(text=f"Starting Gate A review-only run for {task_id}…\n")
    else:
        yield TextChunk(text=f"Starting Gate A real-task run for {task_id}…\n")
    options = RealTaskRunOptions(
        artifacts_dir=artifacts_dir,
        review_only=review_only,
        omnigent_session_id=omnigent_session_id,
    )
    run_task = asyncio.create_task(asyncio.to_thread(run_real_task_gate, spec, options))
    heartbeat = 0
    while not run_task.done():
        done, _pending = await asyncio.wait({run_task}, timeout=_HEARTBEAT_INTERVAL_S)
        if run_task in done:
            break
        heartbeat += 1
        label = "review" if review_only else "run"
        yield TextChunk(text=f"({label} in progress… heartbeat {heartbeat})\n")

    try:
        result: RealTaskRunResult = run_task.result()
    except Exception as exc:  # noqa: BLE001 — surface orchestration failures to operator
        from omnigent.inner.executor import ExecutorError

        yield ExecutorError(message=str(exc))
        return

    receipt_path = artifacts_dir / "receipt.json"
    yield TextChunk(text=format_safe_summary(result.receipt, receipt_path=receipt_path))
    if not result.receipt.ok:
        from omnigent.inner.executor import ExecutorError

        phase = "review-only run" if review_only else "real-task run"
        yield ExecutorError(
            message=(
                f"Gate A {phase} finished with receipt_ok=False "
                f"(problem_count={len(result.receipt.problems)}); "
                "see receipt path in the summary above"
            ),
            retryable=False,
        )
