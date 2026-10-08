"""
``harness: factory-gate-a`` wrap.

Isolated entrypoint for the disposable Cursor CLI Gate A lane. Disabled unless
both ``OMNIGENT_FACTORY_ORDER_SCOPED_WORKER_ENABLED=1`` and
``OMNIGENT_FACTORY_GATE_A_CURSOR_CLI=1`` are set at harness registration time.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.factory.gate_a.admission import factory_gate_a_enabled
from omnigent.factory.gate_a.executor import FactoryGateAExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_factory_gate_a_executor() -> Executor:
    if not factory_gate_a_enabled():
        raise RuntimeError(
            "factory-gate-a harness is not enabled (missing factory Gate A env gates)"
        )
    return FactoryGateAExecutor()


def create_app() -> FastAPI:
    """Build the factory Gate A harness FastAPI app."""
    adapter = ExecutorAdapter(
        executor_factory=_build_factory_gate_a_executor,
        harness_label="Factory Gate A",
    )
    return adapter.build()
