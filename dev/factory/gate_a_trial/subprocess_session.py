"""Subprocess helpers with process-group teardown for Gate A CLI trials."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Mapping

_GRACE_SECONDS = 5.0
_REAP_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class SubprocessResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None


def _process_group_alive(pgid: int) -> bool:
    """Whether any process still belongs to *pgid* (signal 0 probe)."""
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _try_reap_direct_child(pid: int) -> bool:
    """Return True once *pid* is no longer our waitable direct child."""
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return waited == pid


def _reap_direct_child(pid: int) -> None:
    deadline = time.monotonic() + _GRACE_SECONDS
    while time.monotonic() < deadline:
        if _try_reap_direct_child(pid):
            return
        time.sleep(_REAP_POLL_SECONDS)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        return


def _kill_process_group(leader_pid: int) -> None:
    """
    TERM then KILL the session *leader_pid* started (pgid == leader at spawn).

    Keeps escalating while the group exists even if the leader exited first.
    """
    pgid = leader_pid
    if pgid <= 1:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        _reap_direct_child(leader_pid)
        return

    deadline = time.monotonic() + _GRACE_SECONDS
    while time.monotonic() < deadline:
        _try_reap_direct_child(leader_pid)
        if not _process_group_alive(pgid):
            _reap_direct_child(leader_pid)
            return
        time.sleep(_REAP_POLL_SECONDS)

    if _process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _reap_direct_child(leader_pid)


def run_in_new_session(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float,
    text: bool = True,
) -> SubprocessResult:
    """
    Run *argv* in a new session so timeouts can SIGTERM the whole process group.

    On timeout, kills the group and reaps the leader; returncode is -9 when killed.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SubprocessResult(argv=argv, returncode=1, stdout="", stderr="", error=str(exc))

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc.pid)
        stdout_b, stderr_b = proc.communicate()
        stdout = stdout_b if isinstance(stdout_b, str) else (stdout_b or b"").decode(
            "utf-8", errors="replace"
        )
        stderr = stderr_b if isinstance(stderr_b, str) else (stderr_b or b"").decode(
            "utf-8", errors="replace"
        )
        return SubprocessResult(
            argv=argv,
            returncode=-9,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )

    return SubprocessResult(
        argv=argv,
        returncode=int(proc.returncode or 0),
        stdout=stdout or "",
        stderr=stderr or "",
    )
