"""
``harness: factory-gate-a-real`` wrap.

Local beta chat surface for the supervised Gate A real-task runner. Disabled unless
both ``OMNIGENT_FACTORY_GATE_A_REAL_TASK=1`` and
``OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1`` are set at harness registration time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI

from dev.factory.gate_a_real.spec import RealTaskSpecError
from omnigent.factory.gate_a.motion_order_cancel import cancel_motion_order
from omnigent.factory.gate_a.motion_order_start import start_motion_order
from omnigent.factory.gate_a.motion_order_status import read_motion_order_status_summary
from omnigent.factory.gate_a.motion_order_submit import submit_motion_order_draft
from omnigent.factory.gate_a.real_chat import (
    factory_gate_a_real_enabled,
    latest_user_message_text,
    parse_operator_command,
    read_status_summary,
    resolve_task_paths,
    run_approved_task_with_heartbeat,
    usage_hint,
)
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


class FactoryGateARealExecutor(Executor):
    """Operator-command executor; ignores model tool calls and unpinned inputs."""

    def supports_streaming(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        del tools, system_prompt, config
        if not factory_gate_a_real_enabled():
            yield ExecutorError(
                message=(
                    "factory-gate-a-real requires OMNIGENT_FACTORY_GATE_A_REAL_TASK=1 and "
                    "OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1"
                )
            )
            return
        user_text = latest_user_message_text(messages)
        command = parse_operator_command(user_text)
        if command is None:
            yield TextChunk(text=usage_hint())
            yield TurnComplete(response=None)
            return

        if command.kind == "order_status":
            try:
                summary = read_motion_order_status_summary(command.task_id)
            except ValueError as exc:
                yield ExecutorError(message=str(exc))
                return
            yield TextChunk(text=summary)
            yield TurnComplete(response=None)
            return

        if command.kind == "order_submit":
            try:
                summary = submit_motion_order_draft(command.task_id)
            except ValueError as exc:
                yield ExecutorError(message=str(exc))
                return
            yield TextChunk(text=summary)
            yield TurnComplete(response=None)
            return

        if command.kind == "order_start":
            try:
                summary = start_motion_order(command.task_id)
            except ValueError as exc:
                yield ExecutorError(message=str(exc))
                return
            yield TextChunk(text=summary)
            yield TurnComplete(response=None)
            return

        if command.kind == "order_cancel":
            try:
                summary = cancel_motion_order(command.task_id)
            except ValueError as exc:
                yield ExecutorError(message=str(exc))
                return
            yield TextChunk(text=summary)
            yield TurnComplete(response=None)
            return

        try:
            spec_path, artifacts_dir = resolve_task_paths(command.task_id)
        except ValueError as exc:
            yield ExecutorError(message=str(exc))
            return

        if command.kind == "status":
            try:
                summary = read_status_summary(
                    spec_path=spec_path,
                    artifacts_dir=artifacts_dir,
                    task_id=command.task_id,
                )
            except (ValueError, RealTaskSpecError) as exc:
                yield ExecutorError(message=str(exc))
                return
            yield TextChunk(text=summary)
            yield TurnComplete(response=None)
            return

        async for event in run_approved_task_with_heartbeat(
            spec_path,
            artifacts_dir,
            command.task_id,
            review_only=command.kind == "review",
        ):
            yield event
            if isinstance(event, ExecutorError):
                return
        yield TurnComplete(response=None)


def _build_factory_gate_a_real_executor() -> Executor:
    if not factory_gate_a_real_enabled():
        raise RuntimeError(
            "factory-gate-a-real harness is not enabled (missing real-task chat env gates)"
        )
    return FactoryGateARealExecutor()


def create_app() -> FastAPI:
    """Build the factory Gate A real-task chat harness FastAPI app."""
    adapter = ExecutorAdapter(
        executor_factory=_build_factory_gate_a_real_executor,
        harness_label="Factory Gate A Real Task",
    )
    return adapter.build()
