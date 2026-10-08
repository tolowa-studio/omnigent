"""Opt-in local Gate A path for one bound real-operator task."""

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from dev.factory.gate_a_real.orchestration import real_task_gate_enabled, run_real_task_gate
from dev.factory.gate_a_real.spec import RealTaskSpec, load_real_task_spec

__all__ = [
    "REAL_TASK_ENV",
    "RealTaskSpec",
    "load_real_task_spec",
    "real_task_gate_enabled",
    "run_real_task_gate",
]
