"""Pytest-only gate admission seam (import this module, not conftest)."""

from __future__ import annotations

from dev.factory.seatbelt_fixture.manifest import FixtureReceipt
from dev.factory.seatbelt_fixture.trusted_admission import (
    bind_trusted_gate_admission_for_tests,
    register_gate_admission_test_seam,
)

_TEST_SEAM_CAPABILITY = object()
register_gate_admission_test_seam(_TEST_SEAM_CAPABILITY)


def bind_trusted_gate_receipt_for_admission(receipt: FixtureReceipt) -> FixtureReceipt:
    """Attach trusted admission evidence to a synthetic receipt for unit tests."""
    bind_trusted_gate_admission_for_tests(receipt)
    return receipt
