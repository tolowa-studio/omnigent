"""Cursor CLI invocation helpers for real-task Gate A."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dev.factory.gate_a_real.constants import FORBIDDEN_CLI_FLAGS
from dev.factory.gate_a_trial.stream_json import (
    headless_stream_final_result_acceptable,
    parse_stream_json,
)
from dev.factory.gate_a_trial.subprocess_session import _kill_process_group

_GRACE_SECONDS = 5.0
_READ_CHUNK = 4096


@dataclass(frozen=True)
class AgentRunCapture:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    session_ids: tuple[str, ...]
    timed_out: bool
    api_key_inherited: bool
    unexpected_mcp_activity: bool
    pid: int | None = None
    pgid: int | None = None
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None


def assert_argv_safe(argv: list[str]) -> None:
    for arg in argv:
        if arg in FORBIDDEN_CLI_FLAGS:
            raise ValueError(f"forbidden Cursor CLI flag: {arg}")
        if arg.startswith("--api-key"):
            raise ValueError("CURSOR_API_KEY must not be passed via argv")


def extract_session_ids(stdout: str) -> tuple[str, ...]:
    seen: list[str] = []
    known: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id and session_id not in known:
            known.add(session_id)
            seen.append(session_id)
    return tuple(seen)


def _result_field_as_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def stream_terminal_result_text(stdout: str) -> str | None:
    """Text from the last successful terminal ``type=result`` event only."""
    terminal: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        subtype = event.get("subtype")
        if subtype == "success" and event.get("is_error") is False:
            terminal = _result_field_as_text(event.get("result"))
            continue
        if subtype is not None or event.get("is_error") is True:
            return None
    return terminal


def review_stdout_passes(stdout: str) -> bool:
    """Pass only when stream-json terminal result contains a standalone REVIEW: PASS line."""
    final_ok, _ = headless_stream_final_result_acceptable(stdout)
    if not final_ok:
        return False
    text = stream_terminal_result_text(stdout)
    if not text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (
        bool(lines)
        and lines[-1] == "REVIEW: PASS"
        and not any(line.startswith("REVIEW: FAIL") for line in lines)
    )


def stdout_inherited_api_key(stdout: str) -> bool:
    summary = parse_stream_json(stdout)
    source = (summary.api_key_source or "").casefold()
    return source in {"env", "environment", "envvar"}


def stdout_has_mcp_tool_activity(stdout: str) -> bool:
    summary = parse_stream_json(stdout)
    for call in summary.tool_calls:
        if call.mcp_server:
            return True
        if call.name.casefold() in {"mcp", "getmcptools"}:
            return True
    return False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _pump_stream(
    pipe: object,
    log_path: Path,
    buffer: list[bytes],
    lock: threading.Lock,
) -> None:
    if pipe is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        while True:
            chunk = getattr(pipe, "read1", pipe.read)(_READ_CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            handle.flush()
            with lock:
                buffer.append(chunk)


def _run_with_streaming_logs(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    stdout_log: Path,
    stderr_log: Path,
) -> tuple[int, bool, int | None, int | None]:
    for log_path in (stdout_log, stderr_log):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(b"")
    process_path = stdout_log.with_name(stdout_log.name + ".process.json")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr_log.write_text(f"spawn failed: {exc}\n", encoding="utf-8")
        process_path.write_text(json.dumps({"state": "spawn_failed", "error": str(exc)}) + "\n")
        return 1, False, None, None

    pid = proc.pid
    pgid: int | None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    process_path.write_text(
        json.dumps({"state": "running", "pid": pid, "pgid": pgid, "started_at_epoch": time.time()})
        + "\n"
    )

    stdout_buf: list[bytes] = []
    stderr_buf: list[bytes] = []
    lock = threading.Lock()
    threads = [
        threading.Thread(
            target=_pump_stream,
            args=(proc.stdout, stdout_log, stdout_buf, lock),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_stream,
            args=(proc.stderr, stderr_log, stderr_buf, lock),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_process_group(pid)
            break
        time.sleep(0.05)

    for thread in threads:
        thread.join(timeout=_GRACE_SECONDS)

    if any(thread.is_alive() for thread in threads):
        _kill_process_group(pid)
        for thread in threads:
            thread.join(timeout=_GRACE_SECONDS)
        timed_out = True

    process_path.write_text(
        json.dumps(
            {
                "state": "finished",
                "pid": pid,
                "pgid": pgid,
                "exit_code": -9 if timed_out else int(proc.returncode or 0),
                "timed_out": timed_out,
                "finished_at_epoch": time.time(),
            }
        )
        + "\n"
    )

    if timed_out:
        return -9, True, pid, pgid
    return int(proc.returncode or 0), False, pid, pgid


def run_agent_capture(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    sandbox_argv_wrapper: list[str] | None = None,
    stdout_log: Path | None = None,
    stderr_log: Path | None = None,
) -> AgentRunCapture:
    assert_argv_safe(argv)
    if "CURSOR_API_KEY" in env or "CURSOR_SESSION_TOKEN" in env:
        raise ValueError("child env must not inherit Cursor API credentials")
    full_argv = list(sandbox_argv_wrapper or []) + argv

    if stdout_log is None or stderr_log is None:
        raise ValueError("stdout_log and stderr_log are required for real-task capture")

    exit_code, timed_out, pid, pgid = _run_with_streaming_logs(
        full_argv,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
    stdout_sha = _sha256_file(stdout_log)
    stderr_sha = _sha256_file(stderr_log)

    return AgentRunCapture(
        argv=full_argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        session_ids=extract_session_ids(stdout),
        timed_out=timed_out,
        api_key_inherited=stdout_inherited_api_key(stdout),
        unexpected_mcp_activity=stdout_has_mcp_tool_activity(stdout),
        pid=pid,
        pgid=pgid,
        stdout_log_path=str(stdout_log),
        stderr_log_path=str(stderr_log),
        stdout_sha256=stdout_sha,
        stderr_sha256=stderr_sha,
    )
