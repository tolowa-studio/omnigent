"""Test seam aliases for Gate A preflight (qualified receipts still come from Seatbelt)."""

from __future__ import annotations

from dev.factory.gate_a_mcp.preflight import (
    install_preflight_receipt_for_tests,
    reset_preflight_session_for_tests,
)

__all__ = [
    "install_preflight_receipt_for_tests",
    "reset_preflight_session_for_tests",
]
