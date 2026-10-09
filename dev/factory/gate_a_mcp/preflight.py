"""Seatbelt preflight outside MCP CallTool (90s fixture; single-use admit at tool call)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID
from dev.factory.seatbelt_fixture.manifest import (
    GATE_A_MIN_SETTLE_SECONDS,
    FixtureReceipt,
)
from dev.factory.seatbelt_fixture.runner import run_seatbelt_fixture

PREFLIGHT_READY_MARKER = ".gate_a_preflight_ready"
PREFLIGHT_TTL_SECONDS = 600.0


class GateAPreflightError(RuntimeError):
    """Seatbelt preflight or single-use receipt admission failed closed."""

FixtureRunner = Callable[..., FixtureReceipt]


@dataclass
class _PreflightSession:
    receipt: FixtureReceipt | None = None
    minted_monotonic: float = 0.0
    consumed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_session = _PreflightSession()
_preflight_thread: threading.Thread | None = None
_preflight_finished = threading.Event()
_preflight_failure: GateAPreflightError | None = None


def _home_dir() -> Path | None:
    raw = os.environ.get("HOME")
    if not raw:
        return None
    return Path(raw).resolve()


def _skip_startup_preflight() -> bool:
    if os.environ.get("GATE_A_MCP_SKIP_STARTUP_PREFLIGHT"):
        return True
    if os.environ.get("GATE_A_MCP_FIXTURE_SENTINEL"):
        return True
    return False


def _write_ready_marker(receipt: FixtureReceipt) -> None:
    home = _home_dir()
    if home is None:
        return
    payload = {
        "order_id": receipt.order_id,
        "qualified_for_gate_a": receipt.qualified_for_gate_a,
        "minted_monotonic": _session.minted_monotonic,
        "settle_observed_seconds": receipt.settle_observed_seconds,
        "server_pid": os.getpid(),
    }
    marker = home / PREFLIGHT_READY_MARKER
    marker.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _run_startup_seatbelt_preflight_sync(
    *,
    fixture_runner: FixtureRunner | None = None,
    settle_seconds: float = GATE_A_MIN_SETTLE_SECONDS,
    order_id: str = INTERNAL_STAGE_ORDER_ID,
) -> None:
    global _preflight_failure
    if _skip_startup_preflight():
        _preflight_finished.set()
        return
    with _session.lock:
        if _session.receipt is not None:
            _preflight_finished.set()
            return
    try:
        run_fixture = fixture_runner if fixture_runner is not None else run_seatbelt_fixture
        receipt = run_fixture(settle_seconds=settle_seconds, order_id=order_id)
        if not receipt.qualified_for_gate_a:
            reason = receipt.failure_reason or "fixture not qualified_for_gate_a"
            raise GateAPreflightError(f"seatbelt preflight gate failed: {reason}")
        with _session.lock:
            _session.receipt = receipt
            _session.minted_monotonic = time.monotonic()
            _session.consumed = False
        _write_ready_marker(receipt)
    except GateAPreflightError as exc:
        _preflight_failure = exc
    finally:
        _preflight_finished.set()


def run_startup_seatbelt_preflight_blocking(
    *,
    fixture_runner: FixtureRunner | None = None,
    settle_seconds: float = GATE_A_MIN_SETTLE_SECONDS,
    order_id: str = INTERNAL_STAGE_ORDER_ID,
) -> FixtureReceipt | None:
    """Run the 90s Seatbelt fixture synchronously (prestarted HTTP server path)."""
    if _skip_startup_preflight():
        return None
    with _session.lock:
        if _session.receipt is not None:
            return _session.receipt
    _run_startup_seatbelt_preflight_sync(
        fixture_runner=fixture_runner,
        settle_seconds=settle_seconds,
        order_id=order_id,
    )
    if _preflight_failure is not None:
        raise _preflight_failure
    return _session.receipt


def start_startup_seatbelt_preflight_background(
    *,
    fixture_runner: FixtureRunner | None = None,
    settle_seconds: float = GATE_A_MIN_SETTLE_SECONDS,
    order_id: str = INTERNAL_STAGE_ORDER_ID,
) -> None:
    """Start the 90s Seatbelt fixture without blocking MCP stdio initialization."""
    global _preflight_thread
    if _preflight_thread is not None and _preflight_thread.is_alive():
        return
    if _preflight_finished.is_set() and _session.receipt is not None:
        return
    if _skip_startup_preflight():
        _preflight_finished.set()
        return

    def _worker() -> None:
        _run_startup_seatbelt_preflight_sync(
            fixture_runner=fixture_runner,
            settle_seconds=settle_seconds,
            order_id=order_id,
        )

    _preflight_thread = threading.Thread(
        target=_worker,
        name="gate-a-seatbelt-preflight",
        daemon=True,
    )
    _preflight_thread.start()


def _wait_for_preflight_completion(timeout_seconds: float) -> None:
    if not _preflight_finished.wait(timeout=timeout_seconds):
        raise GateAPreflightError(
            f"gate preflight still running after {timeout_seconds:.0f}s",
        )
    if _preflight_failure is not None:
        raise _preflight_failure


def consume_preflight_receipt(
    expected_order_id: str,
    *,
    wait_timeout_seconds: float = 120.0,
) -> FixtureReceipt:
    """Single-use admit: binds the startup fixture receipt to one MCP tool execution."""
    _wait_for_preflight_completion(wait_timeout_seconds)
    with _session.lock:
        if _session.consumed:
            raise GateAPreflightError("gate preflight receipt already consumed (replay)")
        receipt = _session.receipt
        if receipt is None:
            raise GateAPreflightError(
                "gate preflight receipt missing; Seatbelt fixture must complete at server startup",
            )
        age = time.monotonic() - _session.minted_monotonic
        if age > PREFLIGHT_TTL_SECONDS:
            raise GateAPreflightError(
                f"gate preflight receipt expired ({age:.1f}s > {PREFLIGHT_TTL_SECONDS}s)",
            )
        if receipt.order_id != expected_order_id:
            raise GateAPreflightError(
                f"gate preflight order_id mismatch: {receipt.order_id!r} != {expected_order_id!r}",
            )
        if not receipt.qualified_for_gate_a:
            raise GateAPreflightError("gate preflight receipt not qualified_for_gate_a")
        _session.consumed = True
        return receipt


def preflight_ready_marker_path(home: str | Path) -> Path:
    return Path(home).resolve() / PREFLIGHT_READY_MARKER


def wait_for_preflight_ready_marker(
    home: str | Path,
    *,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 0.25,
    expected_server_pid: int | None = None,
) -> bool:
    """
    Trial harness: wait for MCP disposable HOME preflight marker (no receipt secrets).

    When *expected_server_pid* is set, reject markers that do not bind to that process
    (stale file from an earlier stdio child).
    """
    marker = preflight_ready_marker_path(home)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if marker.is_file():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(poll_interval_seconds)
                continue
            if isinstance(data, dict) and data.get("qualified_for_gate_a") is True:
                if expected_server_pid is not None:
                    marker_pid = data.get("server_pid")
                    if marker_pid != expected_server_pid:
                        time.sleep(poll_interval_seconds)
                        continue
                return True
        time.sleep(poll_interval_seconds)
    return False


def reset_preflight_session_for_tests() -> None:
    """Pytest-only: clear in-process preflight state."""
    global _preflight_thread, _preflight_failure
    with _session.lock:
        _session.receipt = None
        _session.minted_monotonic = 0.0
        _session.consumed = False
    _preflight_failure = None
    _preflight_finished.clear()
    _preflight_thread = None
    _preflight_finished.set()


def install_preflight_receipt_for_tests(receipt: FixtureReceipt) -> None:
    """Pytest-only: seed a trusted synthetic receipt without running the fixture."""
    with _session.lock:
        _session.receipt = receipt
        _session.minted_monotonic = time.monotonic()
        _session.consumed = False
    _preflight_finished.set()
