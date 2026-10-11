"""Stage-only order-scoped worker adapter (feature-gated, receipt-gated)."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dev.factory.seatbelt_fixture.executor import (
    ChildRunResult,
    OrderScopedManifestExecutor,
    decode_child_payload,
)
from dev.factory.seatbelt_fixture.manifest import FixtureReceipt, OrderManifest
from dev.factory.seatbelt_fixture.validate import validate_manifest_before_spawn

from .binding import StageWorkOrderBinding, stage_worker_enabled
from .probe_trust import ProbeTrustError, assert_probe_program_trusted
from .receipt_admission import (
    AdmittedGateReceipt,
    ReceiptAdmissionError,
    admit_gate_receipt,
    assert_receipt_path_outside_worktree,
    load_receipt_json,
)
from .worktree_guard import WorktreeGuardError, assert_approved_worktree


class OrderScopedWorkerError(RuntimeError):
    """Stage worker refused or failed closed execution."""


@dataclass(frozen=True)
class WorkerRunResult:
    returncode: int
    stdout: str
    stderr: str
    manifest: OrderManifest
    probe_sha256: str


class OrderScopedWorkerAdapter:
    """
    Admit one bound work order after a qualified Gate A receipt, then spawn via Seatbelt.

    Not registered in the Omnigent runner or Cursor harness — library-only stage path.
    """

    def __init__(
        self,
        binding: StageWorkOrderBinding,
        *,
        gate_receipt: FixtureReceipt | None = None,
        gate_receipt_path: Path | None = None,
        trusted_probe_sha256: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._binding = binding
        self._environ = environ if environ is not None else os.environ
        if gate_receipt is not None and gate_receipt_path is not None:
            raise ValueError("pass gate_receipt or gate_receipt_path, not both")
        if gate_receipt_path is not None:
            assert_receipt_path_outside_worktree(
                gate_receipt_path,
                worktree=binding.worktree,
            )
            gate_receipt = load_receipt_json(gate_receipt_path)
        self._gate_receipt = gate_receipt
        if trusted_probe_sha256 is None:
            raise OrderScopedWorkerError(
                "trusted_probe_sha256 is required (probe pin independent of on-disk bytes)"
            )
        self._trusted_probe_sha256 = trusted_probe_sha256
        self._admitted: AdmittedGateReceipt | None = None

    @property
    def binding(self) -> StageWorkOrderBinding:
        return self._binding

    @property
    def admitted_receipt(self) -> AdmittedGateReceipt | None:
        return self._admitted

    def _ensure_platform_sandbox(self) -> None:
        if self._binding.sandbox_backend != "darwin_seatbelt":
            raise OrderScopedWorkerError(
                f"unsupported sandbox backend: {self._binding.sandbox_backend}"
            )
        if sys.platform != "darwin":
            raise OrderScopedWorkerError("darwin_seatbelt requires macOS")
        if shutil.which("sandbox-exec") is None:
            raise OrderScopedWorkerError("sandbox-exec missing")

    def _assert_binding_argv(self) -> None:
        if self._binding.argv != tuple(self._binding.argv):
            raise OrderScopedWorkerError("argv must be an exact tuple")
        if len(self._binding.argv) < 2:
            raise OrderScopedWorkerError("argv too short")
        if self._binding.allow_network:
            raise OrderScopedWorkerError("stage binding must not allow network")

    def _manifest_from_binding(self) -> OrderManifest:
        return OrderManifest(
            order_id=self._binding.order_id,
            verb=self._binding.argv[2] if len(self._binding.argv) > 2 else "",
            argv=self._binding.argv,
            cwd=str(self._binding.worktree),
        )

    def _truncate(self, text: str, limit: int) -> str:
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= limit:
            return text
        return encoded[:limit].decode("utf-8", errors="replace") + "\n…[truncated]"

    def admit(self) -> AdmittedGateReceipt:
        """Validate gate receipt once; required before execute."""
        if not stage_worker_enabled(self._environ):
            raise OrderScopedWorkerError(
                "stage worker disabled (set OMNIGENT_FACTORY_ORDER_SCOPED_WORKER_ENABLED=1)"
            )
        if self._gate_receipt is None:
            raise ReceiptAdmissionError("no gate receipt supplied")
        if self._binding.gate_receipt_order_id != self._binding.order_id:
            raise OrderScopedWorkerError("gate_receipt_order_id must match binding.order_id")
        try:
            self._admitted = admit_gate_receipt(
                self._gate_receipt,
                expected_order_id=self._binding.order_id,
                expected_sandbox_backend=self._binding.sandbox_backend,
            )
        except ReceiptAdmissionError as exc:
            raise OrderScopedWorkerError(str(exc)) from exc
        return self._admitted

    def execute(self) -> WorkerRunResult:
        """Run the bound argv under Seatbelt after admission and probe re-hash."""
        if self._admitted is None:
            self.admit()

        self._ensure_platform_sandbox()
        self._assert_binding_argv()

        try:
            worktree = assert_approved_worktree(
                self._binding.worktree,
                bound_worktree=self._binding.worktree,
            )
        except WorktreeGuardError as exc:
            raise OrderScopedWorkerError(str(exc)) from exc

        manifest = self._manifest_from_binding()
        if manifest.argv != self._binding.argv:
            raise OrderScopedWorkerError("manifest argv drift from binding")
        if manifest.order_id != self._binding.order_id:
            raise OrderScopedWorkerError("manifest order_id drift from binding")

        python = Path(manifest.argv[0])
        script = Path(manifest.argv[1])
        try:
            probe_sha = assert_probe_program_trusted(
                trusted_path=self._binding.trusted_probe_script,
                expected_sha256=self._trusted_probe_sha256,
                argv_script_path=script,
                worktree=worktree,
            )
        except ProbeTrustError as exc:
            raise OrderScopedWorkerError(str(exc)) from exc

        try:
            validate_manifest_before_spawn(
                manifest,
                bound_order_id=self._binding.order_id,
                expected_cwd=worktree,
                expected_child_script=self._binding.trusted_probe_script,
                expected_python=python,
                prior_order_id=None,
            )
        except Exception as exc:
            raise OrderScopedWorkerError(f"manifest validation failed: {exc}") from exc

        if frozenset(self._binding.env_allowlist) != self._binding.env_allowlist:
            raise OrderScopedWorkerError("env allowlist must be a frozenset")

        executor = OrderScopedManifestExecutor(
            order_id=self._binding.order_id,
            checkout=worktree,
            child_script=self._binding.trusted_probe_script,
            python_executable=python,
        )
        if executor.sandbox_backend != self._binding.sandbox_backend:
            raise OrderScopedWorkerError("executor sandbox backend mismatch")

        run: ChildRunResult = executor.execute(
            manifest,
            timeout_seconds=self._binding.timeout_seconds,
        )
        stdout = self._truncate(run.stdout, self._binding.max_stdout_bytes)
        stderr = self._truncate(run.stderr, self._binding.max_stderr_bytes)
        if run.returncode != 0:
            raise OrderScopedWorkerError(
                f"child exited with status {run.returncode}: {stderr.strip() or stdout.strip()}"
            )
        try:
            decode_child_payload(stdout)
        except ValueError as exc:
            raise OrderScopedWorkerError(f"invalid child JSON payload: {exc}") from exc
        return WorkerRunResult(
            returncode=run.returncode,
            stdout=stdout,
            stderr=stderr,
            manifest=manifest,
            probe_sha256=probe_sha,
        )


def decode_worker_child_payload(result: WorkerRunResult) -> dict[str, object]:
    return decode_child_payload(result.stdout)
