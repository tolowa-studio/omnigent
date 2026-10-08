"""Tests for the disposable macOS Seatbelt factory fixture."""

from __future__ import annotations

import errno
import json
import os
import shutil
import sys
from pathlib import Path
import pytest

from dev.factory.seatbelt_fixture import child_main
from dev.factory.seatbelt_fixture.manifest import (
    CANARY_FILE_TOKEN,
    HOME_PROBE_DIR_PREFIX,
    HOME_SENTINEL_FILENAME,
    HOME_SENTINEL_TOKEN,
    OrderManifest,
    child_script_path,
)
from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS, FixtureReceipt, ProbeRecord
from dev.factory.seatbelt_fixture.runner import (
    _create_home_sentinel,
    _negative_probe_ok,
    _qualified_for_gate_a,
    run_seatbelt_fixture,
    run_validation_smoke,
)
from dev.factory.seatbelt_fixture import __main__ as fixture_cli
from dev.factory.seatbelt_fixture.validate import ManifestValidationError, validate_manifest_before_spawn

_FIXTURE_RECEIPT_JSON_KEYS = (
    "manifest_hash",
    "command_hash",
    "sandbox_backend",
    "probes",
    "passed",
    "qualified_for_gate_a",
)


def _parse_fixture_cli_receipt_stdout(stdout: str) -> dict[str, object]:
    """Parse the final fixture CLI JSON receipt when log lines precede it on stdout."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and all(k in payload for k in _FIXTURE_RECEIPT_JSON_KEYS):
            return payload
    raise ValueError("no fixture receipt JSON found in CLI stdout")


def test_parse_fixture_cli_receipt_stdout_ignores_log_prefix() -> None:
    receipt_line = json.dumps(
        {
            "passed": False,
            "qualified_for_gate_a": False,
            "manifest_hash": "m",
            "command_hash": "c",
            "sandbox_backend": "darwin_seatbelt",
            "probes": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    stdout = (
        "WARNING omnigent.inner.seatbelt_sandbox: auto-widened write grant\n"
        f"{receipt_line}\n"
    )
    parsed = _parse_fixture_cli_receipt_stdout(stdout)
    assert parsed["passed"] is False
    assert parsed["qualified_for_gate_a"] is False


def test_validate_rejects_shell_and_traversal(tmp_path: Path) -> None:
    child = child_script_path()
    python = Path(sys.executable)
    checkout = tmp_path / "co"
    checkout.mkdir()
    base = OrderManifest(
        order_id="o1",
        verb="positive",
        argv=(str(python), str(child), "positive"),
        cwd=str(checkout),
    )
    validate_manifest_before_spawn(
        base,
        bound_order_id="o1",
        expected_cwd=checkout,
        expected_child_script=child,
        expected_python=python,
        prior_order_id=None,
    )

    sh_c = OrderManifest(
        order_id="o1",
        verb="probe_outside_read",
        argv=(str(python), str(child), "probe_outside_read", "/tmp/evil;rm"),
        cwd=str(checkout),
    )
    with pytest.raises(ManifestValidationError, match="metacharacter"):
        validate_manifest_before_spawn(
            sh_c,
            bound_order_id="o1",
            expected_cwd=checkout,
            expected_child_script=child,
            expected_python=python,
            prior_order_id=None,
        )

    traversal = OrderManifest(
        order_id="o1",
        verb="probe_outside_read",
        argv=(str(python), str(child), "probe_outside_read", str(tmp_path / ".." / "etc" / "passwd")),
        cwd=str(checkout),
    )
    with pytest.raises(ManifestValidationError, match="traversal"):
        validate_manifest_before_spawn(
            traversal,
            bound_order_id="o1",
            expected_cwd=checkout,
            expected_child_script=child,
            expected_python=python,
            prior_order_id=None,
        )


def test_validation_smoke() -> None:
    run_validation_smoke()


def test_negative_probe_requires_explicit_denial() -> None:
    target = "/tmp/home-sentinel"
    assert _negative_probe_ok(
        {
            "verb": "probe_home_read",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "read_denial_errno": errno.EPERM,
            "leaked": False,
            "target_path": target,
            "detail": "read denied",
        },
        "probe_home_read",
        expected_target=target,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_home_read",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "read_denial_errno": errno.EPERM,
            "target_path": target,
            "detail": "read denied",
        },
        "probe_home_read",
        expected_target=target,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_home_read",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "read_denial_errno": errno.EPERM,
            "leaked": True,
            "target_path": target,
            "detail": "read denied",
        },
        "probe_home_read",
        expected_target=target,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_home_read",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "read_denial_errno": errno.ENOENT,
            "target_path": target,
            "detail": "read failed errno=2",
        },
        "probe_home_read",
        expected_target=target,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_home_read",
            "ok": True,
            "executed": False,
            "read_denied": True,
            "read_denial_errno": errno.EPERM,
            "target_path": target,
            "detail": "read denied",
        },
        "probe_home_read",
        expected_target=target,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_home_read",
            "ok": True,
            "executed": True,
            "read_denied": False,
            "read_denial_errno": None,
            "target_path": target,
            "detail": "skipped",
        },
        "probe_home_read",
        expected_target=target,
    )
    outside = "/tmp/outside-canary"
    assert _negative_probe_ok(
        {
            "verb": "probe_outside_write",
            "ok": True,
            "executed": True,
            "write_denied": True,
            "write_denial_errno": errno.EPERM,
            "target_path": outside,
            "detail": "write denied",
        },
        "probe_outside_write",
        expected_target=outside,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_outside_write",
            "ok": True,
            "executed": True,
            "write_denied": True,
            "write_denial_errno": errno.ENOENT,
            "target_path": outside,
            "detail": "write failed errno=2",
        },
        "probe_outside_write",
        expected_target=outside,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_outside_write",
            "ok": True,
            "executed": True,
            "write_denied": True,
            "write_denial_errno": errno.EPERM,
            "target_path": "/wrong/path",
            "detail": "write denied",
        },
        "probe_outside_write",
        expected_target=outside,
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_outside_read",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "read_denial_errno": errno.EPERM,
            "leaked": False,
            "target_path": outside,
            "detail": "read denied",
        },
        "probe_outside_read",
        expected_target="/other",
    )
    assert _negative_probe_ok(
        {
            "verb": "probe_local_connect",
            "ok": True,
            "executed": True,
            "connected": False,
            "connect_denial_errno": errno.EPERM,
            "detail": "connect denied",
        },
        "probe_local_connect",
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_local_connect",
            "ok": True,
            "executed": True,
            "connect_denial_errno": errno.EPERM,
            "detail": "connect denied",
        },
        "probe_local_connect",
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_local_connect",
            "ok": True,
            "executed": True,
            "connected": False,
            "connect_denial_errno": errno.ECONNREFUSED,
            "detail": "connect failed errno=61",
        },
        "probe_local_connect",
    )
    assert _negative_probe_ok(
        {
            "verb": "probe_env_secret",
            "ok": True,
            "executed": True,
            "leaked": False,
            "detail": "env secret denied to child",
        },
        "probe_env_secret",
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_env_secret",
            "ok": True,
            "leaked": False,
            "detail": "env secret denied to child",
        },
        "probe_env_secret",
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_env_secret",
            "ok": True,
            "executed": True,
            "detail": "env secret denied to child",
        },
        "probe_env_secret",
    )
    assert _negative_probe_ok(
        {
            "verb": "probe_home_symlink",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "write_denied": True,
            "read_denial_errno": errno.EPERM,
            "write_denial_errno": errno.EPERM,
            "leaked": False,
            "target_path": target,
            "link_name": "home_sentinel_link",
            "detail": "symlink read and write denied",
        },
        "probe_home_symlink",
        expected_target=target,
        expected_link_name="home_sentinel_link",
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_home_symlink",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "write_denied": True,
            "read_denial_errno": errno.EPERM,
            "write_denial_errno": errno.EPERM,
            "target_path": target,
            "link_name": "home_sentinel_link",
            "detail": "symlink read and write denied",
        },
        "probe_home_symlink",
        expected_target=target,
        expected_link_name="home_sentinel_link",
    )
    assert not _negative_probe_ok(
        {
            "verb": "probe_home_symlink",
            "ok": True,
            "executed": True,
            "read_denied": True,
            "write_denied": True,
            "read_denial_errno": errno.EPERM,
            "write_denial_errno": errno.EPERM,
            "leaked": True,
            "target_path": target,
            "link_name": "home_sentinel_link",
            "detail": "symlink read and write denied",
        },
        "probe_home_symlink",
        expected_target=target,
        expected_link_name="home_sentinel_link",
    )


def test_qualified_for_gate_a_requires_full_settle_and_passed_probes() -> None:
    base = FixtureReceipt(
        passed=True,
        failure_reason=None,
        order_id="o",
        manifest_hash="m",
        command_hash="c",
        platform="darwin",
        machine="arm64",
        sandbox_backend="darwin_seatbelt",
        probes=[ProbeRecord("positive", True, "ok")],
        settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
    )
    assert _qualified_for_gate_a(base, cleanup_succeeded=True)

    short = FixtureReceipt(
        passed=True,
        failure_reason=None,
        order_id="o",
        manifest_hash="m",
        command_hash="c",
        platform="darwin",
        machine="arm64",
        sandbox_backend="darwin_seatbelt",
        probes=[ProbeRecord("positive", True, "ok")],
        settle_observed_seconds=0.25,
    )
    assert not _qualified_for_gate_a(short, cleanup_succeeded=True)

    failed_probe = FixtureReceipt(
        passed=False,
        failure_reason="probe failures: positive",
        order_id="o",
        manifest_hash="m",
        command_hash="c",
        platform="darwin",
        machine="arm64",
        sandbox_backend="darwin_seatbelt",
        probes=[ProbeRecord("positive", False, "bad")],
        settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
    )
    assert not _qualified_for_gate_a(failed_probe, cleanup_succeeded=True)


def test_child_outside_probes_nonexistent_path_fails(capsys: pytest.CaptureFixture[str]) -> None:
    missing = "/no/such/omnigent-seatbelt-outside-probe-path"
    rc = child_main.main(["probe_outside_read", missing])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert rc != 0
    assert payload.get("ok") is False
    assert payload.get("read_denied") is False
    assert payload.get("read_denial_errno") == errno.ENOENT
    assert payload.get("target_path") == missing

    rc = child_main.main(["probe_outside_write", missing])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert rc != 0
    assert payload.get("write_denied") is False
    assert payload.get("write_denial_errno") == errno.ENOENT
    assert payload.get("target_path") == missing


def test_child_home_read_nonexistent_path_fails(capsys: pytest.CaptureFixture[str]) -> None:
    rc = child_main.main(["probe_home_read", "/no/such/omnigent-seatbelt-probe-path"])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert rc != 0
    assert payload.get("ok") is False
    assert payload.get("read_denied") is False
    assert payload.get("read_denial_errno") == errno.ENOENT


def test_create_home_sentinel_cleans_probe_dir_on_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    real_write_text = Path.write_text

    def failing_write(self: Path, *args: object, **kwargs: object) -> int:
        if (
            self.name == HOME_SENTINEL_FILENAME
            and HOME_PROBE_DIR_PREFIX in self.parent.name
        ):
            raise OSError("injected sentinel write failure")
        return real_write_text(self, *args, **kwargs)

    before_probe_dirs = {
        p.name
        for p in Path.home().iterdir()
        if p.is_dir() and p.name.startswith(HOME_PROBE_DIR_PREFIX)
    }
    monkeypatch.setattr(Path, "write_text", failing_write)
    with pytest.raises(OSError, match="injected"):
        _create_home_sentinel()

    after_probe_dirs = {
        p.name
        for p in Path.home().iterdir()
        if p.is_dir() and p.name.startswith(HOME_PROBE_DIR_PREFIX)
    }
    assert after_probe_dirs == before_probe_dirs


def test_child_home_probes_require_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert child_main.main(["probe_home_read"]) == 2
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert payload.get("executed") is False

    assert child_main.main(["probe_home_symlink", "/tmp/x"]) == 2
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert payload.get("executed") is False


def test_validate_home_probe_argv_lengths(tmp_path: Path) -> None:
    child = child_script_path()
    python = Path(sys.executable)
    checkout = tmp_path / "co"
    checkout.mkdir()
    home_manifest = OrderManifest(
        order_id="o1",
        verb="probe_home_read",
        argv=(str(python), str(child), "probe_home_read"),
        cwd=str(checkout),
    )
    with pytest.raises(ManifestValidationError, match="probe_home_read"):
        validate_manifest_before_spawn(
            home_manifest,
            bound_order_id="o1",
            expected_cwd=checkout,
            expected_child_script=child,
            expected_python=python,
            prior_order_id=None,
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_fixture_cleanup_failure_marks_receipt_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    real_rmtree = shutil.rmtree
    home_probe_path: Path | None = None

    def rmtree(path: Path | str, *args: object, **kwargs: object) -> None:
        nonlocal home_probe_path
        target = Path(path)
        if home_probe_path is not None and target == home_probe_path:
            raise OSError("injected home_probe rmtree failure")
        real_rmtree(path, *args, **kwargs)

    import dev.factory.seatbelt_fixture.runner as runner_mod

    original_create = runner_mod._create_home_sentinel

    def capture_home_sentinel() -> tuple[Path, Path]:
        nonlocal home_probe_path
        home_probe_path, sentinel = original_create()
        return home_probe_path, sentinel

    monkeypatch.setattr(runner_mod, "_create_home_sentinel", capture_home_sentinel)
    monkeypatch.setattr(shutil, "rmtree", rmtree)

    receipt = run_seatbelt_fixture(settle_seconds=0.1, order_id="pytest-cleanup-home-fail")
    assert not receipt.passed
    assert receipt.failure_reason is not None
    assert "cleanup failures" in receipt.failure_reason
    assert "home_probe rmtree" in receipt.failure_reason
    assert home_probe_path is not None
    assert home_probe_path.exists(), "injected home_probe cleanup failure must leave the probe dir"
    real_rmtree(home_probe_path, ignore_errors=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_fixture_listener_stop_failure_still_cleans_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    import dev.factory.seatbelt_fixture.runner as runner_mod

    def failing_stop(self: runner_mod._ListenerCounter) -> None:
        raise OSError("injected listener.stop failure")

    monkeypatch.setattr(runner_mod._ListenerCounter, "stop", failing_stop)

    before_probe_dirs = {
        p.name
        for p in Path.home().iterdir()
        if p.is_dir() and p.name.startswith(HOME_PROBE_DIR_PREFIX)
    }
    receipt = run_seatbelt_fixture(settle_seconds=0.1, order_id="pytest-listener-stop-fail")
    after_probe_dirs = {
        p.name
        for p in Path.home().iterdir()
        if p.is_dir() and p.name.startswith(HOME_PROBE_DIR_PREFIX)
    }
    assert not receipt.passed
    assert receipt.failure_reason is not None
    assert "listener.stop" in receipt.failure_reason
    assert after_probe_dirs == before_probe_dirs


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_receipt_secret_hygiene_fails_emitted_passed_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    import dev.factory.seatbelt_fixture.runner as runner_mod

    real_hygiene = runner_mod._output_contains_secrets

    def hygiene_gate(text: str) -> bool:
        if '"sandbox_backend"' in text and '"settle_seconds"' in text:
            return True
        return real_hygiene(text)

    monkeypatch.setattr(runner_mod, "_output_contains_secrets", hygiene_gate)

    receipt = run_seatbelt_fixture(settle_seconds=0.1, order_id="pytest-receipt-hygiene")
    line = receipt.emit_json_line()
    parsed = json.loads(line)
    assert parsed["passed"] is False
    assert receipt.failure_reason is not None
    assert "secret token present in receipt" in receipt.failure_reason


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_fixture_local_connect_fails_when_listener_not_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    import dev.factory.seatbelt_fixture.runner as runner_mod

    monkeypatch.setattr(
        runner_mod._ListenerCounter,
        "is_accepting_connections",
        lambda self: False,
    )

    receipt = run_seatbelt_fixture(settle_seconds=0.1, order_id="pytest-listener-down")
    probe = next(p for p in receipt.probes if p.name == "probe_local_connect")
    assert not probe.passed
    assert "not accepting" in probe.detail


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_seatbelt_fixture_integration_fast_settle() -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    receipt = run_seatbelt_fixture(settle_seconds=0.25, order_id="pytest-seatbelt-fixture")
    line = receipt.emit_json_line()
    parsed = json.loads(line)
    assert "manifest_hash" in parsed
    assert "command_hash" in parsed
    assert parsed["settle_seconds"] == 0.25
    assert parsed["sandbox_backend"] == "darwin_seatbelt"
    assert CANARY_FILE_TOKEN not in line
    assert HOME_SENTINEL_TOKEN not in line
    assert not any(CANARY_FILE_TOKEN in p["detail"] for p in parsed["probes"])
    probe_names = {p["name"] for p in parsed["probes"]}
    assert "probe_home_read" in probe_names
    assert "probe_home_symlink" in probe_names
    assert "home_sentinel_unchanged" in probe_names
    assert receipt.passed, parsed
    assert parsed["qualified_for_gate_a"] is False
    assert receipt.qualified_for_gate_a is False
    assert parsed["settle_observed_seconds"] < GATE_A_MIN_SETTLE_SECONDS


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
@pytest.mark.skipif(
    os.environ.get("FIXTURE_RUN_GATE_A_FULL") != "1",
    reason="set FIXTURE_RUN_GATE_A_FULL=1 for the 90-second Gate A receipt",
)
def test_seatbelt_fixture_qualified_for_gate_a_full_settle() -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    receipt = run_seatbelt_fixture(
        settle_seconds=GATE_A_MIN_SETTLE_SECONDS,
        order_id="pytest-seatbelt-gate-a-full",
    )
    parsed = json.loads(receipt.emit_json_line())
    assert receipt.passed, parsed
    assert receipt.qualified_for_gate_a is True
    assert parsed["qualified_for_gate_a"] is True
    assert parsed["settle_observed_seconds"] >= GATE_A_MIN_SETTLE_SECONDS


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_cli_stdout_passed_false_when_fixture_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing — unsupported Seatbelt must fail the gate")

    import dev.factory.seatbelt_fixture.runner as runner_mod

    real_hygiene = runner_mod._output_contains_secrets

    def hygiene_gate(text: str) -> bool:
        if '"sandbox_backend"' in text and '"settle_seconds"' in text:
            return True
        return real_hygiene(text)

    monkeypatch.setattr(runner_mod, "_output_contains_secrets", hygiene_gate)

    rc = fixture_cli.main(["--settle-seconds", "0.1", "--order-id", "pytest-cli-hygiene"])
    out = capsys.readouterr().out
    parsed = _parse_fixture_cli_receipt_stdout(out)
    assert rc == 1
    assert parsed["passed"] is False
    assert parsed["qualified_for_gate_a"] is False
