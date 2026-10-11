"""Factory Gate A work-order admission (expiry, config drift, witness binding)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness
from dev.factory.gate_a_trial.prestarted_mcp import PrestartedGateAMcp
from dev.factory.order_scoped.binding import (
    INTERNAL_STAGE_BRIEF_HASH,
    INTERNAL_STAGE_ORDER_ID,
    STAGE_WORKER_ENV,
)

FACTORY_GATE_A_CURSOR_CLI_ENV = "OMNIGENT_FACTORY_GATE_A_CURSOR_CLI"

_ENV_ORDER_ID = "HARNESS_FACTORY_GATE_A_ORDER_ID"
_ENV_BRIEF_HASH = "HARNESS_FACTORY_GATE_A_BRIEF_HASH"
_ENV_CONFIG_DIR = "HARNESS_FACTORY_GATE_A_CONFIG_DIR"
_ENV_EXPIRES_AT = "HARNESS_FACTORY_GATE_A_EXPIRES_AT"
_ENV_CONFIG_HASHES = "HARNESS_FACTORY_GATE_A_CONFIG_HASHES_JSON"
_ENV_WORKSPACE = "HARNESS_FACTORY_GATE_A_WORKSPACE"
_FORBIDDEN_WITNESS_NONCE = "HARNESS_FACTORY_GATE_A_WITNESS_NONCE"
_FORBIDDEN_SERVER_PID = "HARNESS_FACTORY_GATE_A_SERVER_PID"

_FORBIDDEN_PATH_OVERRIDE_KEYS = (
    _ENV_WORKSPACE,
    _ENV_CONFIG_DIR,
    _ENV_CONFIG_HASHES,
)


class FactoryGateAAdmissionError(RuntimeError):
    """Admission rejected before any Cursor CLI subprocess is spawned."""


@dataclass(frozen=True)
class FactoryWorkOrderAdmission:
    """One admitted factory order (synthetic fixture only).

    Workspace, ``CURSOR_CONFIG_DIR``, and config fingerprints are minted per turn by
    the adapter; callers must not supply path overrides via harness env.
    """

    order_id: str
    brief_hash: str
    expires_at: datetime


def factory_gate_a_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True only when both factory env gates are explicitly enabled."""
    env = environ if environ is not None else os.environ
    if env.get(STAGE_WORKER_ENV, "").strip() != "1":
        return False
    return env.get(FACTORY_GATE_A_CURSOR_CLI_ENV, "").strip() == "1"


def reject_fabricated_witness_env(environ: Mapping[str, str]) -> None:
    """Fail closed when a runner tries to inject witness fields via harness env."""
    if environ.get(_FORBIDDEN_WITNESS_NONCE, "").strip():
        raise FactoryGateAAdmissionError(
            "HARNESS_FACTORY_GATE_A_WITNESS_NONCE must not be set; witness is adapter-minted"
        )
    if environ.get(_FORBIDDEN_SERVER_PID, "").strip():
        raise FactoryGateAAdmissionError(
            "HARNESS_FACTORY_GATE_A_SERVER_PID must not be set; witness is adapter-minted"
        )


def reject_external_path_override_env(environ: Mapping[str, str]) -> None:
    """Reject harness env that would steer writes at a real workspace or Cursor config."""
    for key in _FORBIDDEN_PATH_OVERRIDE_KEYS:
        if environ.get(key, "").strip():
            raise FactoryGateAAdmissionError(
                f"{key} must not be set; workspace and config paths are adapter-owned"
            )


def validate_fixture_scope(admission: FactoryWorkOrderAdmission) -> None:
    """Confine the production adapter to the known synthetic stage work order."""
    if admission.order_id != INTERNAL_STAGE_ORDER_ID:
        raise FactoryGateAAdmissionError(
            f"order_id must be the synthetic fixture {INTERNAL_STAGE_ORDER_ID!r}"
        )
    if admission.brief_hash != INTERNAL_STAGE_BRIEF_HASH:
        raise FactoryGateAAdmissionError(
            "brief_hash must match the canonical synthetic fixture brief hash"
        )


def validate_not_expired(
    admission: FactoryWorkOrderAdmission,
    *,
    now: datetime | None = None,
) -> None:
    """Raise when admission is past ``expires_at``."""
    moment = now or datetime.now(timezone.utc)
    expires = admission.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if moment >= expires:
        raise FactoryGateAAdmissionError("admission expired")


def validate_config_hash_drift(
    expected_hashes: dict[str, str],
    actual_hashes: dict[str, str],
) -> None:
    """Raise when materialized config fingerprints diverge from expected pins."""
    problems: list[str] = []
    for key, expected in expected_hashes.items():
        actual = actual_hashes.get(key)
        if actual is None:
            problems.append(f"missing config fingerprint: {key}")
        elif actual != expected:
            problems.append(f"config hash drift for {key}")
    if problems:
        raise FactoryGateAAdmissionError("; ".join(problems))


def validate_witness_matches_prestarted(
    prestarted: PrestartedGateAMcp,
    witness: QualifiedProcessWitness,
) -> None:
    """Raise when the qualified witness file does not match the owned MCP handle."""
    if witness.witness_nonce != prestarted.witness_nonce:
        raise FactoryGateAAdmissionError("witness_nonce mismatch against prestarted handle")
    if witness.server_pid != prestarted.server_pid:
        raise FactoryGateAAdmissionError("server_pid mismatch against prestarted handle")


def validate_witness_order_binding(
    admission: FactoryWorkOrderAdmission,
    witness: QualifiedProcessWitness,
) -> None:
    """Raise when the live witness order does not match the admitted fixture order."""
    if witness.order_id != admission.order_id:
        raise FactoryGateAAdmissionError("witness order_id mismatch")


def validate_mcp_payload_against_admission(
    admission: FactoryWorkOrderAdmission,
    payload: Mapping[str, object],
) -> None:
    """Raise when MCP JSON payload order diverges from admission (static prompt guard)."""
    order_id = payload.get("order_id")
    if order_id != admission.order_id:
        raise FactoryGateAAdmissionError("payload order_id mismatch")
    payload_brief = payload.get("brief_hash")
    if not isinstance(payload_brief, str) or payload_brief != admission.brief_hash:
        raise FactoryGateAAdmissionError("payload brief_hash mismatch")


def load_admission_from_environ(
    environ: Mapping[str, str] | None = None,
) -> FactoryWorkOrderAdmission:
    """Build admission from harness env (runner-minted; not user chat)."""
    env = environ if environ is not None else os.environ
    reject_fabricated_witness_env(env)
    reject_external_path_override_env(env)
    order_id = env.get(_ENV_ORDER_ID, "").strip() or INTERNAL_STAGE_ORDER_ID
    brief_hash = env.get(_ENV_BRIEF_HASH, "").strip()
    if not brief_hash:
        raise FactoryGateAAdmissionError("HARNESS_FACTORY_GATE_A_BRIEF_HASH is required")
    expires_raw = env.get(_ENV_EXPIRES_AT, "").strip()
    if not expires_raw:
        raise FactoryGateAAdmissionError("HARNESS_FACTORY_GATE_A_EXPIRES_AT is required")
    expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    admission = FactoryWorkOrderAdmission(
        order_id=order_id,
        brief_hash=brief_hash,
        expires_at=expires_at,
    )
    validate_fixture_scope(admission)
    return admission
