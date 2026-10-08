"""In-process gate receipt trust (evidence object; JSON cannot mint)."""

from __future__ import annotations

from .manifest import FixtureReceipt

_RUNNER_MINT_CAPABILITY = object()
_TEST_SEAM_CAPABILITY: object | None = None

_ADMISSION_EVIDENCE_ATTR = "_gate_admission_evidence"


class ReceiptTrustError(ValueError):
    """Untrusted attempt to seal a gate receipt."""


class TrustedGateAdmissionEvidence:
    """
    Non-serializable proof that a ``FixtureReceipt`` was bound in the trusted parent.

    Order-scoped Seatbelt children cannot construct or pass this across the process
    boundary; JSON-loaded receipts never carry it.
    """

    __slots__ = ("_receipt",)

    def __init__(self, receipt: FixtureReceipt, *, _mint_capability: object) -> None:
        if _mint_capability is not _RUNNER_MINT_CAPABILITY and _mint_capability is not _TEST_SEAM_CAPABILITY:
            raise ReceiptTrustError("untrusted gate receipt seal attempt")
        self._receipt = receipt

    @property
    def receipt(self) -> FixtureReceipt:
        return self._receipt

    def __getstate__(self) -> object:
        raise TypeError("TrustedGateAdmissionEvidence cannot be serialized")

    def __setstate__(self, _state: object) -> None:
        raise TypeError("TrustedGateAdmissionEvidence cannot be deserialized")


def register_gate_admission_test_seam(seam_capability: object) -> None:
    """
    One-time registration for pytest (``tests/dev/factory/gate_admission_test_support.py``).

    Not env-gated; the seam handle is an unguessable object held only by that module.
    """
    global _TEST_SEAM_CAPABILITY
    if _TEST_SEAM_CAPABILITY is not None:
        raise RuntimeError("gate admission test seam already registered")
    _TEST_SEAM_CAPABILITY = seam_capability


def bind_trusted_gate_admission_for_runner(receipt: FixtureReceipt) -> TrustedGateAdmissionEvidence:
    """Mark *receipt* as minted by ``run_seatbelt_fixture`` (runner process only)."""
    evidence = TrustedGateAdmissionEvidence(receipt, _mint_capability=_RUNNER_MINT_CAPABILITY)
    setattr(receipt, _ADMISSION_EVIDENCE_ATTR, evidence)
    return evidence


def bind_trusted_gate_admission_for_tests(receipt: FixtureReceipt) -> TrustedGateAdmissionEvidence:
    """Bind admission evidence using the registered pytest seam."""
    if _TEST_SEAM_CAPABILITY is None:
        raise ReceiptTrustError("gate admission test seam not registered")
    evidence = TrustedGateAdmissionEvidence(receipt, _mint_capability=_TEST_SEAM_CAPABILITY)
    setattr(receipt, _ADMISSION_EVIDENCE_ATTR, evidence)
    return evidence


def gate_admission_evidence_for_receipt(receipt: FixtureReceipt) -> TrustedGateAdmissionEvidence | None:
    evidence = getattr(receipt, _ADMISSION_EVIDENCE_ATTR, None)
    if isinstance(evidence, TrustedGateAdmissionEvidence):
        return evidence
    return None


def receipt_admission_trusted(
    receipt: FixtureReceipt,
    evidence: TrustedGateAdmissionEvidence | None,
) -> bool:
    if evidence is None:
        return False
    return evidence.receipt is receipt
