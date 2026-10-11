"""Factory Gate A executor — one harness turn per admitted work order."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

from omnigent.factory.gate_a.admission import (
    FactoryGateAAdmissionError,
    factory_gate_a_enabled,
    load_admission_from_environ,
)
from omnigent.factory.gate_a.cursor_cli_session import run_admitted_session
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


class FactoryGateAExecutor(Executor):
    """
    Runs exactly one Gate A Cursor CLI positive turn per admitted order.

    No ToolSpec bridging; native Shell/Write remain denied via trial cli-config.
    """

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
        del messages, tools, system_prompt, config
        if not factory_gate_a_enabled():
            yield ExecutorError(
                message=(
                    "factory Gate A executor requires "
                    "OMNIGENT_FACTORY_ORDER_SCOPED_WORKER_ENABLED=1 and "
                    "OMNIGENT_FACTORY_GATE_A_CURSOR_CLI=1"
                )
            )
            return
        try:
            admission = load_admission_from_environ()
        except FactoryGateAAdmissionError as exc:
            yield ExecutorError(message=str(exc))
            return

        pass_key = os.environ.get("HARNESS_FACTORY_GATE_A_PASS_CURSOR_API_KEY", "").strip() == "1"
        if not pass_key:
            yield ExecutorError(
                message=(
                    "factory Gate A requires HARNESS_FACTORY_GATE_A_PASS_CURSOR_API_KEY=1 "
                    "for a certified positive turn"
                )
            )
            return
        result = await asyncio.to_thread(
            run_admitted_session,
            admission,
            pass_cursor_api_key=True,
        )
        if not result.ok:
            yield ExecutorError(message="; ".join(result.problems) or "Gate A turn failed")
            return
        yield TextChunk(text="Gate A admitted turn certified.")
        yield TurnComplete(response=None)
