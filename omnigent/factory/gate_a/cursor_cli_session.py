"""One-shot Gate A Cursor CLI session lifecycle for an admitted work order."""

from __future__ import annotations

from dataclasses import dataclass

from dev.factory.gate_a_trial.orchestration import GateAResult, run_admitted_gate_a_turn
from omnigent.factory.gate_a.admission import (
    FactoryGateAAdmissionError,
    FactoryWorkOrderAdmission,
    validate_fixture_scope,
    validate_not_expired,
)


@dataclass(frozen=True)
class FactoryGateASessionResult:
    """Adapter-facing result of one admitted Gate A CLI session."""

    ok: bool
    problems: tuple[str, ...]
    gate_result: GateAResult | None = None


def run_admitted_session(
    admission: FactoryWorkOrderAdmission,
    *,
    pass_cursor_api_key: bool = False,
) -> FactoryGateASessionResult:
    """
    Run one admitted order through the shared Gate A orchestration library.

    Validates expiry before any subprocess spawn. Does not enable front-door writes
    or task submit/cancel paths. Discovery and a Cursor API key are required for ok.
    """
    try:
        validate_not_expired(admission)
        validate_fixture_scope(admission)
    except FactoryGateAAdmissionError as exc:
        return FactoryGateASessionResult(ok=False, problems=(str(exc),))

    if not pass_cursor_api_key:
        return FactoryGateASessionResult(
            ok=False,
            problems=("positive CLI requires HARNESS_FACTORY_GATE_A_PASS_CURSOR_API_KEY=1",),
        )

    gate_result = run_admitted_gate_a_turn(
        order_id=admission.order_id,
        brief_hash=admission.brief_hash,
        expires_at=admission.expires_at,
        pass_cursor_api_key=True,
        run_discovery=True,
    )
    problems = tuple(gate_result.problems)
    return FactoryGateASessionResult(
        ok=gate_result.ok,
        problems=problems,
        gate_result=gate_result,
    )
