"""Parent-side orchestration for the Seatbelt factory fixture."""

from __future__ import annotations

import contextlib
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .executor import OrderScopedManifestExecutor, decode_child_payload
from .manifest import (
    CANARY_FILE_TOKEN,
    GATE_A_MIN_SETTLE_SECONDS,
    HOME_PROBE_DIR_PREFIX,
    HOME_SENTINEL_FILENAME,
    HOME_SENTINEL_TOKEN,
    SEATBELT_CONNECT_DENIAL_ERRNOS,
    SEATBELT_FILE_DENIAL_ERRNOS,
    FixtureReceipt,
    OrderManifest,
    ProbeRecord,
    child_script_path,
)
from .trusted_admission import bind_trusted_gate_admission_for_runner
from .validate import ManifestValidationError, validate_manifest_before_spawn


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_init_no_remote(checkout: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(checkout),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=str(checkout),
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "fixture"],
        cwd=str(checkout),
        env=env,
        check=True,
        capture_output=True,
    )


class _ListenerCounter:
    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        self.port = server.getsockname()[1]
        self._server = server

        def _serve() -> None:
            while True:
                try:
                    conn, _addr = server.accept()
                except OSError:
                    break
                with self._lock:
                    self._count += 1
                conn.close()

        self._thread = threading.Thread(target=_serve, name="fixture-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()

    def count(self) -> int:
        with self._lock:
            return self._count

    def is_accepting_connections(self) -> bool:
        if self.port <= 0:
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2.0) as conn:
                conn.close()
        except OSError:
            return False
        return True


def _snapshot_file(path: Path) -> tuple[str, float]:
    if not path.exists():
        return ("", 0.0)
    stat = path.stat()
    return (path.read_text(encoding="utf-8"), stat.st_mtime)


def _list_checkout_files(checkout: Path) -> set[str]:
    return {str(p.relative_to(checkout)) for p in checkout.rglob("*") if p.is_file()}


def _create_home_sentinel() -> tuple[Path, Path]:
    """Disposable synthetic directory under $HOME; removed by the caller."""
    probe_dir = Path.home() / f"{HOME_PROBE_DIR_PREFIX}{secrets.token_hex(8)}"
    probe_dir.mkdir(mode=0o700, parents=False)
    sentinel = probe_dir / HOME_SENTINEL_FILENAME
    try:
        sentinel.write_text(HOME_SENTINEL_TOKEN + "\n", encoding="utf-8")
    except OSError:
        with contextlib.suppress(OSError):
            shutil.rmtree(probe_dir)
        raise
    return probe_dir, sentinel


def _output_contains_secrets(text: str) -> bool:
    return CANARY_FILE_TOKEN in text or HOME_SENTINEL_TOKEN in text


def _errno_is_seatbelt_denial(value: object) -> bool:
    if not isinstance(value, int):
        return False
    return value in SEATBELT_FILE_DENIAL_ERRNOS


def _errno_is_seatbelt_connect_denial(value: object) -> bool:
    if not isinstance(value, int):
        return False
    return value in SEATBELT_CONNECT_DENIAL_ERRNOS


def _target_probe_binding_ok(
    payload: dict[str, object],
    *,
    verb: str,
    expected_target: str,
    expected_link_name: str | None = None,
) -> bool:
    if payload.get("verb") != verb:
        return False
    if payload.get("target_path") != expected_target:
        return False
    if expected_link_name is not None and payload.get("link_name") != expected_link_name:
        return False
    return True


def _home_probe_binding_ok(
    payload: dict[str, object],
    *,
    verb: str,
    expected_target: str,
    expected_link_name: str | None = None,
) -> bool:
    if payload.get("verb") != verb:
        return False
    if payload.get("target_path") != expected_target:
        return False
    if expected_link_name is not None and payload.get("link_name") != expected_link_name:
        return False
    return True


def _qualified_for_gate_a(receipt: FixtureReceipt, *, cleanup_succeeded: bool) -> bool:
    """True only for a full Gate A darwin_seatbelt run with the minimum settle window."""
    if receipt.sandbox_backend != "darwin_seatbelt":
        return False
    if not cleanup_succeeded:
        return False
    if receipt.settle_observed_seconds < GATE_A_MIN_SETTLE_SECONDS:
        return False
    if not receipt.probes or not all(p.passed for p in receipt.probes):
        return False
    return receipt.passed and receipt.failure_reason is None


def _negative_probe_ok(
    payload: dict[str, object],
    verb: str,
    *,
    expected_target: str | None = None,
    expected_link_name: str | None = None,
) -> bool:
    if payload.get("executed") is not True:
        return False
    if payload.get("ok") is not True:
        return False
    detail = str(payload.get("detail", ""))
    if "denied" not in detail.lower():
        return False
    if verb == "probe_home_read":
        if expected_target is None:
            return False
        if not _home_probe_binding_ok(payload, verb=verb, expected_target=expected_target):
            return False
        return (
            payload.get("read_denied") is True
            and payload.get("leaked") is False
            and _errno_is_seatbelt_denial(payload.get("read_denial_errno"))
        )
    if verb == "probe_home_symlink":
        if expected_target is None or expected_link_name is None:
            return False
        if not _home_probe_binding_ok(
            payload,
            verb=verb,
            expected_target=expected_target,
            expected_link_name=expected_link_name,
        ):
            return False
        return (
            payload.get("read_denied") is True
            and payload.get("write_denied") is True
            and payload.get("leaked") is False
            and _errno_is_seatbelt_denial(payload.get("read_denial_errno"))
            and _errno_is_seatbelt_denial(payload.get("write_denial_errno"))
        )
    if verb == "probe_outside_read":
        if expected_target is None:
            return False
        if not _target_probe_binding_ok(payload, verb=verb, expected_target=expected_target):
            return False
        return (
            payload.get("read_denied") is True
            and payload.get("leaked") is False
            and _errno_is_seatbelt_denial(payload.get("read_denial_errno"))
        )
    if verb == "probe_outside_write":
        if expected_target is None:
            return False
        if not _target_probe_binding_ok(payload, verb=verb, expected_target=expected_target):
            return False
        return payload.get("write_denied") is True and _errno_is_seatbelt_denial(
            payload.get("write_denial_errno")
        )
    if verb == "probe_local_connect":
        return payload.get("connected") is False and _errno_is_seatbelt_connect_denial(
            payload.get("connect_denial_errno")
        )
    if verb == "probe_env_secret":
        return payload.get("leaked") is False
    return False


def _record_negative_probe(
    probes: list[ProbeRecord],
    verb: str,
    run_stdout: str,
    run_stderr: str,
    returncode: int,
    payload: dict[str, object],
    *,
    expected_target: str | None = None,
    expected_link_name: str | None = None,
) -> None:
    ok = returncode == 0 and _negative_probe_ok(
        payload,
        verb,
        expected_target=expected_target,
        expected_link_name=expected_link_name,
    )
    if _output_contains_secrets(run_stdout) or _output_contains_secrets(run_stderr):
        ok = False
        payload_detail = "fixture secret token in child output"
    else:
        payload_detail = str(payload.get("detail", ""))
    probes.append(ProbeRecord(verb, ok, f"rc={returncode} {payload_detail}"))


def _dispose_fixture_paths(
    *,
    listener: _ListenerCounter,
    checkout_dir: Path,
    outside_dir: Path,
    home_probe_dir: Path | None,
) -> list[str]:
    """Attempt cleanup of each disposable path; never swallow errors silently."""
    errors: list[str] = []
    try:
        listener.stop()
    except OSError as exc:
        errors.append(f"listener.stop: {exc}")

    for label, path in (
        ("checkout", checkout_dir),
        ("outside", outside_dir),
        ("home_probe", home_probe_dir),
    ):
        if path is None:
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            errors.append(f"{label} rmtree: {exc}")
        else:
            if path.exists():
                errors.append(f"{label} still present after rmtree: {path}")

    if home_probe_dir is not None and home_probe_dir.exists():
        errors.append(f"home_probe directory remains: {home_probe_dir}")

    return errors


def _merge_failure_reason(existing: str | None, extra: str) -> str:
    if existing:
        return f"{existing}; {extra}"
    return extra


def run_seatbelt_fixture(
    *,
    settle_seconds: float | None = None,
    order_id: str = "factory-seatbelt-fixture-order",
) -> FixtureReceipt:
    settle = float(settle_seconds if settle_seconds is not None else os.environ.get("FIXTURE_SETTLE_SECONDS", "90"))
    started_at = _utc_now()
    probes: list[ProbeRecord] = []
    failure_reason: str | None = None
    sandbox_backend = "darwin_seatbelt"
    positive_manifest_hash = ""
    positive_command_hash = ""

    if sys.platform != "darwin":
        failure_reason = "darwin_seatbelt unsupported on this platform"
        sandbox_backend = "unavailable"
    elif shutil.which("sandbox-exec") is None:
        failure_reason = "sandbox-exec missing; darwin_seatbelt gate fails closed"

    receipt = FixtureReceipt(
        passed=False,
        failure_reason=failure_reason,
        order_id=order_id,
        manifest_hash=positive_manifest_hash,
        command_hash=positive_command_hash,
        platform=sys.platform,
        machine=platform.machine(),
        sandbox_backend=sandbox_backend,
        probes=probes,
        started_at=started_at,
        settle_seconds=settle,
    )

    if failure_reason:
        receipt.ended_at = _utc_now()
        return receipt

    checkout_dir = Path(tempfile.mkdtemp(prefix="omnigent-seatbelt-fixture-"))
    outside_dir = Path(tempfile.mkdtemp(prefix="omnigent-seatbelt-outside-"))
    home_probe_dir: Path | None = None
    home_sentinel: Path | None = None
    listener = _ListenerCounter()
    child_script_source = child_script_path()
    probe_phase_passed = False

    try:
        home_probe_dir, home_sentinel = _create_home_sentinel()
        _git_init_no_remote(checkout_dir)
        fixture_child = checkout_dir / "fixture_child_main.py"
        shutil.copy(child_script_source, fixture_child)
        baseline_files = _list_checkout_files(checkout_dir)

        child_script = fixture_child

        outside_canary = outside_dir / "canary_secret.txt"
        outside_canary.write_text(CANARY_FILE_TOKEN + "\n", encoding="utf-8")
        canary_before = _snapshot_file(outside_canary)
        home_sentinel_before = _snapshot_file(home_sentinel)

        listener.start()
        listener_before = listener.count()
        listener_parent_connects = 0

        executor = OrderScopedManifestExecutor(
            order_id=order_id,
            checkout=checkout_dir,
            child_script=child_script,
        )

        # Positive control
        positive = executor.build_manifest("positive")
        positive_manifest_hash = positive.manifest_hash()
        positive_command_hash = positive.command_hash()
        receipt.manifest_hash = positive_manifest_hash
        receipt.command_hash = positive_command_hash

        positive_run = executor.execute(positive)
        try:
            positive_payload = decode_child_payload(positive_run.stdout)
        except ValueError as exc:
            probes.append(ProbeRecord("positive", False, f"invalid child output: {exc}"))
        else:
            files_after = _list_checkout_files(checkout_dir)
            new_files = files_after - baseline_files
            only_allowed = new_files == {"allowed.txt"}
            ok = (
                positive_run.returncode == 0
                and bool(positive_payload.get("ok"))
                and only_allowed
            )
            detail = (
                f"rc={positive_run.returncode} new_files={sorted(new_files)}"
                if ok
                else f"rc={positive_run.returncode} payload={positive_payload!r} new={sorted(new_files)}"
            )
            probes.append(ProbeRecord("positive", ok, detail))

        if _output_contains_secrets(positive_run.stdout) or _output_contains_secrets(positive_run.stderr):
            probes.append(
                ProbeRecord("positive_output_hygiene", False, "fixture secret leaked to child output")
            )

        # Negative probes
        assert home_sentinel is not None
        home_sentinel_str = str(home_sentinel)
        outside_canary_str = str(outside_canary)
        probe_specs: list[tuple[str, tuple[str, ...], str | None, str | None]] = [
            ("probe_outside_write", (outside_canary_str,), outside_canary_str, None),
            ("probe_outside_read", (outside_canary_str,), outside_canary_str, None),
            ("probe_home_read", (home_sentinel_str,), home_sentinel_str, None),
            (
                "probe_home_symlink",
                (home_sentinel_str, "home_sentinel_link"),
                home_sentinel_str,
                "home_sentinel_link",
            ),
            ("probe_local_connect", (str(listener.port),), None, None),
            ("probe_env_secret", (), None, None),
        ]
        for verb, extra, expected_target, expected_link in probe_specs:
            if verb == "probe_local_connect":
                if not listener.is_accepting_connections():
                    probes.append(
                        ProbeRecord(
                            verb,
                            False,
                            "local listener not accepting connections before child probe",
                        )
                    )
                    continue
                listener_parent_connects += 1
            manifest = executor.build_manifest(verb, extra)
            run = executor.execute(manifest)
            try:
                payload = decode_child_payload(run.stdout)
            except ValueError as exc:
                probes.append(ProbeRecord(verb, False, f"invalid child output: {exc}"))
                continue
            _record_negative_probe(
                probes,
                verb,
                run.stdout,
                run.stderr,
                run.returncode,
                payload,
                expected_target=expected_target,
                expected_link_name=expected_link,
            )

        settle_started = time.monotonic()
        time.sleep(settle)
        receipt.settle_observed_seconds = time.monotonic() - settle_started

        canary_after = _snapshot_file(outside_canary)
        home_sentinel_after = _snapshot_file(home_sentinel)
        listener_after = listener.count()
        outside_unchanged = canary_before == canary_after
        home_sentinel_unchanged = home_sentinel_before == home_sentinel_after
        listener_unchanged = listener_after == listener_before + listener_parent_connects

        probes.append(
            ProbeRecord(
                "outside_canary_unchanged",
                outside_unchanged,
                "mtime/content stable after settle",
            )
        )
        probes.append(
            ProbeRecord(
                "home_sentinel_unchanged",
                home_sentinel_unchanged,
                "mtime/content stable after settle",
            )
        )
        probes.append(
            ProbeRecord(
                "listener_no_connections",
                listener_unchanged,
                f"count={listener_after}",
            )
        )

        probe_phase_passed = all(p.passed for p in probes)
        if not probe_phase_passed:
            failed = [p.name for p in probes if not p.passed]
            if failure_reason is None:
                failure_reason = f"probe failures: {', '.join(failed)}"

        receipt.failure_reason = failure_reason
        receipt.probes = probes
    finally:
        cleanup_errors = _dispose_fixture_paths(
            listener=listener,
            checkout_dir=checkout_dir,
            outside_dir=outside_dir,
            home_probe_dir=home_probe_dir,
        )
        receipt.ended_at = _utc_now()
        if cleanup_errors:
            receipt.passed = False
            receipt.failure_reason = _merge_failure_reason(
                receipt.failure_reason,
                f"cleanup failures: {'; '.join(cleanup_errors)}",
            )
        elif failure_reason is None:
            receipt.passed = probe_phase_passed
            final_line = receipt.emit_json_line()
            if _output_contains_secrets(final_line):
                receipt.passed = False
                receipt.failure_reason = "fixture secret token present in receipt"
        else:
            receipt.passed = False

        receipt.qualified_for_gate_a = _qualified_for_gate_a(
            receipt,
            cleanup_succeeded=not cleanup_errors,
        )
        if receipt.qualified_for_gate_a:
            bind_trusted_gate_admission_for_runner(receipt)

    return receipt


def run_validation_smoke() -> None:
    """Exercise pre-spawn rejection paths without spawning."""
    checkout = Path(tempfile.mkdtemp())
    try:
        executor = OrderScopedManifestExecutor(
            order_id="order-a",
            checkout=checkout,
            child_script=child_script_path(),
        )
        foreign = OrderScopedManifestExecutor(
            order_id="order-b",
            checkout=checkout,
            child_script=child_script_path(),
        ).build_manifest("positive")
        try:
            executor.execute(foreign)
        except ManifestValidationError:
            pass
        else:
            raise AssertionError("expected cross-order rejection")
        shell_manifest = OrderManifest(
            order_id="order-a",
            verb="positive",
            argv=("/bin/sh", "-c", "echo hi"),
            cwd=str(checkout),
        )
        try:
            validate_manifest_before_spawn(
                shell_manifest,
                bound_order_id="order-a",
                expected_cwd=checkout,
                expected_child_script=child_script_path(),
                expected_python=Path(sys.executable),
                prior_order_id=None,
            )
        except ManifestValidationError:
            pass
        else:
            raise AssertionError("expected shell rejection")
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
