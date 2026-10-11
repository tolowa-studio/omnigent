"""Stable stdio MCP subprocess launch (``python -m dev.factory.gate_a_mcp``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dev.factory.order_scoped.binding import STAGE_WORKER_ENV

_SERVER_MODULE = "dev.factory.gate_a_mcp"


def gate_a_repo_root() -> Path:
    """Repository root for ``dev/factory/gate_a_mcp`` (three parents above ``factory``)."""
    return Path(__file__).resolve().parents[3]


def _env_executable() -> str:
    return shutil.which("env") or "/usr/bin/env"


def _minimal_stdio_env(
    *,
    disposable_home: str | Path | None,
    include_worker_env: bool,
) -> dict[str, str]:
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(gate_a_repo_root()),
    }
    if disposable_home is not None:
        env["HOME"] = str(Path(disposable_home).resolve())
    if include_worker_env:
        env[STAGE_WORKER_ENV] = "1"
    return env


def gate_a_stdio_mcp_launch(
    *,
    python_executable: str | None = None,
    include_worker_env: bool = True,
    disposable_home: str | Path | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """
    Return ``(command, args, env)`` for a stdio Gate A MCP server subprocess.

    Cursor merges parent env with ``mcp.json`` ``env``; use ``env -i`` in *args*
    so the stdio child cannot inherit ``CURSOR_API_KEY`` or user auth. The
    returned ``env`` map is empty so the CLI does not re-inject secrets.
    """
    python = python_executable or sys.executable
    minimal = _minimal_stdio_env(
        disposable_home=disposable_home,
        include_worker_env=include_worker_env,
    )
    env_bin = _env_executable()
    args: list[str] = ["-i"]
    for key in sorted(minimal):
        args.append(f"{key}={minimal[key]}")
    args.extend([python, "-m", _SERVER_MODULE])
    return env_bin, args, {}


def prove_stdio_mcp_child_env_clean(
    *,
    python_executable: str | None = None,
    disposable_home: str | Path,
    include_worker_env: bool = True,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """
    Spawn the same ``env -i`` launch with an env probe instead of the MCP server.

    Verifies the effective child environment has no inherited Cursor auth keys.
    """
    python = python_executable or sys.executable
    minimal = _minimal_stdio_env(
        disposable_home=disposable_home,
        include_worker_env=include_worker_env,
    )
    env_bin = _env_executable()
    probe_code = (
        "import json,os; "
        f"assert os.environ.get('HOME')=={json.dumps(str(Path(disposable_home).resolve()))}; "
        "keys=sorted(os.environ); "
        "assert 'CURSOR_API_KEY' not in os.environ; "
        "assert 'CURSOR_SESSION_TOKEN' not in os.environ; "
        "print(json.dumps({'keys':keys,'home':os.environ.get('HOME')}))"
    )
    argv = ["-i"]
    for key in sorted(minimal):
        argv.append(f"{key}={minimal[key]}")
    argv.extend([python, "-c", probe_code])
    try:
        proc = subprocess.run(
            [env_bin, *argv],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc), "argv": [env_bin, *argv]}
    ok = proc.returncode == 0
    payload: dict[str, object] = {
        "ok": ok,
        "returncode": proc.returncode,
        "argv": [env_bin, *argv],
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }
    if ok and proc.stdout:
        try:
            payload["observed"] = json.loads(proc.stdout)
        except ValueError:
            payload["observed"] = None
    return payload
