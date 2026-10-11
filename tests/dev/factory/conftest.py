"""Ensure gate admission test seam is registered before factory dev tests run."""

import pytest

from tests.dev.factory.gate_admission_test_support import (  # noqa: F401
    bind_trusted_gate_receipt_for_admission,
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: long-running Gate A Seatbelt fixture tests")
