"""Adversarial tests for the stage order-scoped worker adapter."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from dev.factory.order_scoped.adapter import OrderScopedWorkerAdapter, OrderScopedWorkerError, decode_worker_child_payload
from dev.factory.order_scoped.binding import (
    INTERNAL_STAGE_ORDER_ID,
    STAGE_WORKER_ENV,
    StageWorkOrderBinding,
    stage_worker_enabled,
)
from dev.factory.order_scoped.cursor_integration import (
    CURSOR_SHELL_INTEGRATION_BLOCKER,
    cursor_shell_routes_only_through_adapter,
)
from dev.factory.order_scoped.probe_trust import fingerprint_probe_program
from dev.factory.order_scoped.receipt_admission import (
    GATE_A_EXACT_PROBE_NAMES,
    ReceiptAdmissionError,
    admit_gate_receipt,
)
from dev.factory.order_scoped.worktree_guard import WorktreeGuardError, assert_approved_worktree
from dev.factory.seatbelt_fixture.manifest import (
    GATE_A_MIN_SETTLE_SECONDS,
    FixtureReceipt,
    ProbeRecord,
    SEATBELT_FILE_DENIAL_ERRNOS,
    child_script_path,
)
from dev.factory.seatbelt_fixture.runner import run_seatbelt_fixture

from tests.dev.factory.gate_admission_test_support import bind_trusted_gate_receipt_for_admission


def _enabled_env() -> dict[str, str]:
    env = dict(os.environ)
    env[STAGE_WORKER_ENV] = "1"
    return env


def _qualified_receipt(
    *,
    order_id: str = INTERNAL_STAGE_ORDER_ID,
    sandbox_backend: str = "darwin_seatbelt",
    settle_observed: float = GATE_A_MIN_SETTLE_SECONDS,
    qualified: bool = True,
    passed: bool = True,
    probe_names: frozenset[str] | None = None,
) -> FixtureReceipt:
    names = probe_names or GATE_A_EXACT_PROBE_NAMES
    probes = [ProbeRecord(name, True, "ok") for name in sorted(names)]
    return FixtureReceipt(
        passed=passed,
        qualified_for_gate_a=qualified,
        failure_reason=None if passed else "synthetic failure",
        order_id=order_id,
        manifest_hash="deadbeef",
        command_hash="cafebabe",
        platform="darwin",
        machine="arm64",
        sandbox_backend=sandbox_backend,
        probes=probes,
        started_at="2020-01-01T00:00:00+00:00",
        ended_at="2020-01-01T00:02:00+00:00",
        settle_seconds=GATE_A_MIN_SETTLE_SECONDS,
        settle_observed_seconds=settle_observed,
    )


def _trusted_receipt(**kwargs: object) -> FixtureReceipt:
    return bind_trusted_gate_receipt_for_admission(_qualified_receipt(**kwargs))


def _probe_pin() -> str:
    return fingerprint_probe_program(child_script_path())


def test_stage_worker_disabled_by_default() -> None:
    assert not stage_worker_enabled({})
    assert not stage_worker_enabled({STAGE_WORKER_ENV: "0"})
    assert stage_worker_enabled({STAGE_WORKER_ENV: "1"})


def test_admit_requires_qualified_not_passed_alone() -> None:
    receipt = _trusted_receipt(qualified=False, passed=True)
    with pytest.raises(ReceiptAdmissionError, match="qualified_for_gate_a"):
        admit_gate_receipt(
            receipt,
            expected_order_id=INTERNAL_STAGE_ORDER_ID,
            expected_sandbox_backend="darwin_seatbelt",
        )


def test_admit_rejects_untrusted_synthetic_receipt() -> None:
    receipt = _qualified_receipt()
    with pytest.raises(ReceiptAdmissionError, match="not minted"):
        admit_gate_receipt(
            receipt,
            expected_order_id=INTERNAL_STAGE_ORDER_ID,
            expected_sandbox_backend="darwin_seatbelt",
        )


def test_admit_rejects_short_settle_window() -> None:
    receipt = _trusted_receipt(settle_observed=1.0)
    with pytest.raises(ReceiptAdmissionError, match="settle"):
        admit_gate_receipt(
            receipt,
            expected_order_id=INTERNAL_STAGE_ORDER_ID,
            expected_sandbox_backend="darwin_seatbelt",
        )


def test_admit_rejects_exact_probe_set_mismatch() -> None:
    receipt = _trusted_receipt(probe_names=frozenset({"positive"}))
    with pytest.raises(ReceiptAdmissionError, match="probe set mismatch"):
        admit_gate_receipt(
            receipt,
            expected_order_id=INTERNAL_STAGE_ORDER_ID,
            expected_sandbox_backend="darwin_seatbelt",
        )


def test_admit_rejects_backend_mismatch() -> None:
    receipt = _trusted_receipt(sandbox_backend="linux_bwrap")
    with pytest.raises(ReceiptAdmissionError, match="sandbox backend"):
        admit_gate_receipt(
            receipt,
            expected_order_id=INTERNAL_STAGE_ORDER_ID,
            expected_sandbox_backend="darwin_seatbelt",
        )


def test_binding_rejects_allow_network(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    checkout.mkdir()
    base = StageWorkOrderBinding.internal_stage_default(
        worktree=checkout,
        python_executable=Path(sys.executable),
    )
    binding = StageWorkOrderBinding(
        order_id=base.order_id,
        argv=base.argv,
        worktree=base.worktree,
        env_allowlist=base.env_allowlist,
        allow_network=True,
        timeout_seconds=base.timeout_seconds,
        max_stdout_bytes=base.max_stdout_bytes,
        max_stderr_bytes=base.max_stderr_bytes,
        sandbox_backend=base.sandbox_backend,
        trusted_probe_script=base.trusted_probe_script,
        gate_receipt_order_id=base.gate_receipt_order_id,
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    adapter.admit()
    with pytest.raises(OrderScopedWorkerError, match="network"):
        adapter.execute()


def test_execute_refuses_when_feature_disabled(tmp_path: Path) -> None:
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=tmp_path,
        python_executable=Path(sys.executable),
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=_probe_pin(),
        environ={},
    )
    with pytest.raises(OrderScopedWorkerError, match="disabled"):
        adapter.execute()


def test_mutated_probe_script_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "co"
    checkout.mkdir()
    trusted = child_script_path()
    good_hash = fingerprint_probe_program(trusted)
    evil_copy = checkout / "fixture_child_main.py"
    evil_copy.write_text(trusted.read_text(encoding="utf-8") + "\n# mutated\n", encoding="utf-8")
    argv = (str(Path(sys.executable)), str(evil_copy), "positive")
    binding = StageWorkOrderBinding(
        order_id=INTERNAL_STAGE_ORDER_ID,
        argv=argv,
        worktree=checkout,
        env_allowlist=StageWorkOrderBinding.internal_stage_default(
            worktree=checkout, python_executable=Path(sys.executable)
        ).env_allowlist,
        allow_network=False,
        timeout_seconds=120.0,
        max_stdout_bytes=100_000,
        max_stderr_bytes=100_000,
        sandbox_backend="darwin_seatbelt",
        trusted_probe_script=trusted,
        gate_receipt_order_id=INTERNAL_STAGE_ORDER_ID,
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=good_hash,
        environ=_enabled_env(),
    )
    adapter.admit()
    with pytest.raises(OrderScopedWorkerError, match="trusted path"):
        adapter.execute()


def test_worktree_symlink_escape_rejected(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("symlink guard test is unix-oriented")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=link,
        python_executable=Path(sys.executable),
    )
    with pytest.raises(WorktreeGuardError, match="symlink"):
        assert_approved_worktree(link, bound_worktree=binding.worktree)


def test_execute_rejects_symlink_worktree_without_side_effects(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("symlink guard test is unix-oriented")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=link,
        python_executable=Path(sys.executable),
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    adapter.admit()
    with pytest.raises(OrderScopedWorkerError, match="symlink"):
        adapter.execute()
    assert not (real / "allowed.txt").exists()


def test_cursor_shell_bypass_blocker_documented() -> None:
    assert cursor_shell_routes_only_through_adapter() is False
    assert "allowlist" in CURSOR_SHELL_INTEGRATION_BLOCKER.lower()
    assert "hook" in CURSOR_SHELL_INTEGRATION_BLOCKER.lower()


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_worker_positive_write_inside_worktree(tmp_path: Path) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    checkout = tmp_path / "worktree"
    checkout.mkdir()
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=checkout,
        python_executable=Path(sys.executable),
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    adapter.admit()
    assert binding.sandbox_backend == "darwin_seatbelt"
    result = adapter.execute()
    payload = decode_worker_child_payload(result)
    assert result.returncode == 0
    assert payload.get("ok") is True
    assert (checkout / "allowed.txt").is_file()


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_denied_outside_write_via_probe_not_in_binding(tmp_path: Path) -> None:
    """Binding argv is closed; outside probes are not admissible work manifests."""
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing")

    checkout = tmp_path / "co"
    checkout.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    trusted = child_script_path()
    argv = (str(Path(sys.executable)), str(trusted), "probe_outside_write", str(outside))
    binding = StageWorkOrderBinding(
        order_id=INTERNAL_STAGE_ORDER_ID,
        argv=argv,
        worktree=checkout,
        env_allowlist=StageWorkOrderBinding.internal_stage_default(
            worktree=checkout, python_executable=Path(sys.executable)
        ).env_allowlist,
        allow_network=False,
        timeout_seconds=120.0,
        max_stdout_bytes=100_000,
        max_stderr_bytes=100_000,
        sandbox_backend="darwin_seatbelt",
        trusted_probe_script=trusted,
        gate_receipt_order_id=INTERNAL_STAGE_ORDER_ID,
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    adapter.admit()
    assert binding.sandbox_backend == "darwin_seatbelt"
    result = adapter.execute()
    payload = decode_worker_child_payload(result)
    assert result.returncode == 0
    assert payload.get("write_denied") is True
    assert payload.get("write_denial_errno") == errno.EPERM
    assert payload.get("write_denial_errno") in SEATBELT_FILE_DENIAL_ERRNOS
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_missing_receipt_file_fails(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    checkout.mkdir()
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=checkout,
        python_executable=Path(sys.executable),
    )
    with pytest.raises(ReceiptAdmissionError, match="cannot read receipt"):
        OrderScopedWorkerAdapter(
            binding,
            gate_receipt_path=tmp_path / "no-such-receipt.json",
            trusted_probe_sha256=_probe_pin(),
            environ=_enabled_env(),
        )


def test_forged_receipt_json_inside_worktree_rejected_before_spawn(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    checkout.mkdir()
    forged_path = checkout / "gate_receipt.json"
    forged_path.write_text(
        json.dumps(_qualified_receipt().to_jsonable(), sort_keys=True),
        encoding="utf-8",
    )
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=checkout,
        python_executable=Path(sys.executable),
    )
    with pytest.raises(ReceiptAdmissionError, match="inside worktree"):
        OrderScopedWorkerAdapter(
            binding,
            gate_receipt_path=forged_path,
            trusted_probe_sha256=_probe_pin(),
            environ=_enabled_env(),
        )
    assert not (checkout / "allowed.txt").exists()


def test_cross_order_trusted_receipt_rejected(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    checkout.mkdir()
    other_order = "other-order-id"
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=checkout,
        python_executable=Path(sys.executable),
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(order_id=other_order),
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    with pytest.raises(OrderScopedWorkerError, match="order_id mismatch"):
        adapter.admit()


def test_unpinned_probe_inside_worktree_rejected(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    checkout.mkdir()
    in_tree = checkout / "probe.py"
    in_tree.write_text(child_script_path().read_text(encoding="utf-8"), encoding="utf-8")
    argv = (str(Path(sys.executable)), str(in_tree), "positive")
    binding = StageWorkOrderBinding(
        order_id=INTERNAL_STAGE_ORDER_ID,
        argv=argv,
        worktree=checkout,
        env_allowlist=StageWorkOrderBinding.internal_stage_default(
            worktree=checkout, python_executable=Path(sys.executable)
        ).env_allowlist,
        allow_network=False,
        timeout_seconds=120.0,
        max_stdout_bytes=100_000,
        max_stderr_bytes=100_000,
        sandbox_backend="darwin_seatbelt",
        trusted_probe_script=in_tree,
        gate_receipt_order_id=INTERNAL_STAGE_ORDER_ID,
    )
    with pytest.raises(OrderScopedWorkerError, match="trusted_probe_sha256 is required"):
        OrderScopedWorkerAdapter(
            binding,
            gate_receipt=_trusted_receipt(),
            environ=_enabled_env(),
        )


def test_in_worktree_probe_rejected_at_execute(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    checkout.mkdir()
    in_tree = checkout / "probe.py"
    in_tree.write_text(child_script_path().read_text(encoding="utf-8"), encoding="utf-8")
    digest = fingerprint_probe_program(in_tree)
    argv = (str(Path(sys.executable)), str(in_tree), "positive")
    binding = StageWorkOrderBinding(
        order_id=INTERNAL_STAGE_ORDER_ID,
        argv=argv,
        worktree=checkout,
        env_allowlist=StageWorkOrderBinding.internal_stage_default(
            worktree=checkout, python_executable=Path(sys.executable)
        ).env_allowlist,
        allow_network=False,
        timeout_seconds=120.0,
        max_stdout_bytes=100_000,
        max_stderr_bytes=100_000,
        sandbox_backend="darwin_seatbelt",
        trusted_probe_script=in_tree,
        gate_receipt_order_id=INTERNAL_STAGE_ORDER_ID,
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=digest,
        environ=_enabled_env(),
    )
    adapter.admit()
    with pytest.raises(OrderScopedWorkerError, match="inside worktree"):
        adapter.execute()


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_sibling_fixture_manifest_read_denied(tmp_path: Path) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing")

    checkout = tmp_path / "co"
    checkout.mkdir()
    trusted = child_script_path()
    manifest_path = trusted.parent / "manifest.py"
    argv = (str(Path(sys.executable)), str(trusted), "probe_outside_read", str(manifest_path))
    binding = StageWorkOrderBinding(
        order_id=INTERNAL_STAGE_ORDER_ID,
        argv=argv,
        worktree=checkout,
        env_allowlist=StageWorkOrderBinding.internal_stage_default(
            worktree=checkout, python_executable=Path(sys.executable)
        ).env_allowlist,
        allow_network=False,
        timeout_seconds=120.0,
        max_stdout_bytes=100_000,
        max_stderr_bytes=100_000,
        sandbox_backend="darwin_seatbelt",
        trusted_probe_script=trusted,
        gate_receipt_order_id=INTERNAL_STAGE_ORDER_ID,
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    adapter.admit()
    result = adapter.execute()
    payload = decode_worker_child_payload(result)
    assert payload.get("read_denied") is True
    assert payload.get("read_denial_errno") == errno.EPERM


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_execute_raises_when_child_cannot_write_worktree(tmp_path: Path) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing")

    checkout = tmp_path / "co"
    checkout.mkdir()
    os.chmod(checkout, stat.S_IRUSR | stat.S_IXUSR)
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=checkout,
        python_executable=Path(sys.executable),
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=_trusted_receipt(),
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    adapter.admit()
    try:
        with pytest.raises(OrderScopedWorkerError, match="child exited"):
            adapter.execute()
        assert not (checkout / "allowed.txt").exists()
    finally:
        os.chmod(checkout, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
@pytest.mark.skipif(
    os.environ.get("FIXTURE_RUN_GATE_A_FULL") != "1",
    reason="set FIXTURE_RUN_GATE_A_FULL=1 for fresh 90s M1 fixture after worker changes",
)
def test_fresh_gate_a_receipt_admits_worker(tmp_path: Path) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing")

    receipt = run_seatbelt_fixture(
        settle_seconds=GATE_A_MIN_SETTLE_SECONDS,
        order_id=INTERNAL_STAGE_ORDER_ID,
    )
    assert receipt.qualified_for_gate_a
    checkout = Path(tempfile.mkdtemp(dir=tmp_path, prefix="worker-admit-"))
    binding = StageWorkOrderBinding.internal_stage_default(
        worktree=checkout,
        python_executable=Path(sys.executable),
        gate_receipt_order_id=INTERNAL_STAGE_ORDER_ID,
    )
    adapter = OrderScopedWorkerAdapter(
        binding,
        gate_receipt=receipt,
        trusted_probe_sha256=_probe_pin(),
        environ=_enabled_env(),
    )
    admitted = adapter.admit()
    assert admitted.qualified_for_gate_a
    run = adapter.execute()
    assert run.returncode == 0
