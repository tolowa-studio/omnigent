"""Process-group timeout teardown for Gate A trial subprocess helper."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from unittest.mock import MagicMock

import psutil
import pytest

from dev.factory.gate_a_trial import subprocess_session as session

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")

# Leader exits immediately; descendant ignores SIGTERM and prints its pid.
_STUBBORN_DESCENDANT = """
import os
import signal
import sys
import time

if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stdout.write(str(os.getpid()))
    sys.stdout.flush()
    time.sleep(300)
    os._exit(0)
os._exit(0)
"""


@pytest.fixture
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "_GRACE_SECONDS", 0.4)


def _kill_quietly(proc: subprocess.Popen[object]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


_SURVIVOR_LIVENESS_TIMEOUT_SECONDS = 2.0
_SURVIVOR_LIVENESS_POLL_SECONDS = 0.05


def _process_is_live(pid: int) -> bool:
    """True only while *pid* is a running process (not reaped, not a zombie)."""
    if not psutil.pid_exists(pid):
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return proc.is_running()
    except psutil.NoSuchProcess:
        return False


def _assert_no_live_descendant(pid: int) -> None:
    deadline = time.monotonic() + _SURVIVOR_LIVENESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_is_live(pid):
            return
        time.sleep(_SURVIVOR_LIVENESS_POLL_SECONDS)
    assert not _process_is_live(pid), f"TERM-ignoring descendant still running (pid={pid})"


def test_process_is_live_distinguishes_zombie_from_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zombie_pid = 424242
    live_pid = 424243
    zombie = MagicMock()
    zombie.is_running.return_value = True
    zombie.status.return_value = psutil.STATUS_ZOMBIE
    live = MagicMock()
    live.is_running.return_value = True
    live.status.return_value = psutil.STATUS_RUNNING

    def fake_process(p: int) -> MagicMock:
        if p == zombie_pid:
            return zombie
        if p == live_pid:
            return live
        raise psutil.NoSuchProcess(p)

    monkeypatch.setattr(
        psutil,
        "pid_exists",
        lambda p: p in {zombie_pid, live_pid},
    )
    monkeypatch.setattr(psutil, "Process", fake_process)

    assert _process_is_live(zombie_pid) is False
    assert _process_is_live(live_pid) is True


def test_timeout_kills_term_ignoring_descendant_after_leader_exits(short_grace: None) -> None:
    result = session.run_in_new_session(
        [sys.executable, "-c", _STUBBORN_DESCENDANT],
        timeout_seconds=0.25,
    )
    assert result.timed_out is True
    assert result.returncode == -9
    survivor_pid = int(result.stdout.strip())
    _assert_no_live_descendant(survivor_pid)


def test_timeout_does_not_signal_unrelated_session(short_grace: None) -> None:
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(0.15)
        assert psutil.pid_exists(sibling.pid)
        result = session.run_in_new_session(
            [sys.executable, "-c", _STUBBORN_DESCENDANT],
            timeout_seconds=0.25,
        )
        assert result.timed_out is True
        assert psutil.pid_exists(sibling.pid)
    finally:
        _kill_quietly(sibling)
