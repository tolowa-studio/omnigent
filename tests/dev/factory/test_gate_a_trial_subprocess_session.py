"""Process-group timeout teardown for Gate A trial subprocess helper."""

from __future__ import annotations

import os
import subprocess
import sys
import time

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


def test_timeout_kills_term_ignoring_descendant_after_leader_exits(short_grace: None) -> None:
    result = session.run_in_new_session(
        [sys.executable, "-c", _STUBBORN_DESCENDANT],
        timeout_seconds=0.25,
    )
    assert result.timed_out is True
    assert result.returncode == -9
    survivor_pid = int(result.stdout.strip())
    time.sleep(0.1)
    assert not psutil.pid_exists(survivor_pid)


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
