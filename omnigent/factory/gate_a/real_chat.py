"""Operator chat commands for the opt-in Gate A real-task runner (local beta)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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

RUN_APPROVED_PREFIX = "run approved task "
REVIEW_APPROVED_PREFIX = "review approved task "
STATUS_PREFIX = "status "

_HEARTBEAT_INTERVAL_S = 30.0


@dataclass(frozen=True)
class OperatorCommand:
    kind: Literal["run", "review", "status"]
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


def _require_absolute_dir(raw: str, label: str) -> Path:
    value = raw.strip()
    if not value:
        raise ValueError(f"{label} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path.resolve()


def resolve_bound_paths(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    env = environ if environ is not None else os.environ
    spec_raw = env.get(REAL_TASK_SPEC_ENV, "")
    artifacts_raw = env.get(REAL_TASK_ARTIFACTS_ENV, "")
    if not spec_raw.strip():
        raise ValueError(f"{REAL_TASK_SPEC_ENV} is required")
    if not artifacts_raw.strip():
        raise ValueError(f"{REAL_TASK_ARTIFACTS_ENV} is required")
    spec_path = Path(spec_raw.strip()).expanduser()
    if not spec_path.is_absolute():
        raise ValueError(f"{REAL_TASK_SPEC_ENV} must be an absolute path")
    spec_path = spec_path.resolve()
    if not spec_path.is_file():
        raise ValueError(f"spec file not found: {spec_path}")
    artifacts_dir = _require_absolute_dir(artifacts_raw, REAL_TASK_ARTIFACTS_ENV)
    return spec_path, artifacts_dir


def format_safe_summary(receipt: RealTaskReceipt, *, receipt_path: Path) -> str:
    lines = [
        f"receipt_ok: {receipt.ok}",
        f"task_id: {receipt.task_id}",
        f"receipt: {receipt_path}",
        f"problem_count: {len(receipt.problems)}",
        f"freeze_manifest: {receipt.freeze_manifest_path or '(none)'}",
        f"builder_session_ids: {', '.join(receipt.builder_session_ids) or '(none)'}",
        f"review_session_ids: {', '.join(receipt.review_session_ids) or '(none)'}",
        f"builder_exit_code: {receipt.builder_exit_code}",
        f"review_exit_code: {receipt.review_exit_code}",
        f"verify_exit_code: {receipt.verify_exit_code}",
        f"review_pass: {receipt.review_pass}",
        "note: snapshot from receipt.json only (not a live artifact verification)",
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
    return format_safe_summary(receipt, receipt_path=receipt_path)


def usage_hint() -> str:
    return (
        "Gate A real-task chat (beta). Commands:\n"
        "  run approved task <task_id>\n"
        "  review approved task <task_id>\n"
        "  status <task_id>\n"
        "Paths and spec are pinned by operator env vars; the model cannot override them.\n"
    )


async def run_approved_task_with_heartbeat(
    spec_path: Path,
    artifacts_dir: Path,
    task_id: str,
    *,
    review_only: bool = False,
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
    options = RealTaskRunOptions(artifacts_dir=artifacts_dir, review_only=review_only)
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
