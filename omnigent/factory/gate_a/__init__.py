"""Factory Gate A Cursor CLI adapter (disabled unless explicitly enabled)."""

from omnigent.factory.gate_a.admission import (
    FACTORY_GATE_A_CURSOR_CLI_ENV,
    FactoryGateAAdmissionError,
    FactoryWorkOrderAdmission,
    factory_gate_a_enabled,
    load_admission_from_environ,
    reject_external_path_override_env,
    reject_fabricated_witness_env,
    validate_config_hash_drift,
    validate_fixture_scope,
    validate_mcp_payload_against_admission,
    validate_not_expired,
    validate_witness_matches_prestarted,
    validate_witness_order_binding,
)

__all__ = [
    "FACTORY_GATE_A_CURSOR_CLI_ENV",
    "FactoryGateAAdmissionError",
    "FactoryWorkOrderAdmission",
    "factory_gate_a_enabled",
    "load_admission_from_environ",
    "reject_external_path_override_env",
    "reject_fabricated_witness_env",
    "validate_config_hash_drift",
    "validate_fixture_scope",
    "validate_mcp_payload_against_admission",
    "validate_not_expired",
    "validate_witness_matches_prestarted",
    "validate_witness_order_binding",
]
