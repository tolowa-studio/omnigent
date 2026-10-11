"""Prestart a qualified Gate A MCP HTTP server before Cursor CLI discovery."""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev.factory.gate_a_mcp.checkout import canonical_harness_temp_root
from dev.factory.gate_a_mcp.process_witness import (
    ProcessWitnessError,
    dispose_gate_a_mcp_control_dir,
    mcp_streamable_http_url,
    mint_capability_token,
    read_http_capability,
    validate_live_witness,
    wait_for_qualified_witness,
    write_http_capability,
)
from dev.factory.gate_a_mcp.stdio_launch import gate_a_repo_root
from dev.factory.order_scoped.binding import STAGE_WORKER_ENV


@dataclass
class PrestartedGateAMcp:
    """Handle to a loopback MCP server that finished Seatbelt preflight before registration."""

    control_dir: Path
    control_parent: Path
    witness_nonce: str
    server_pid: int
    capability_token: str
    mcp_url: str
    _proc: subprocess.Popen[bytes] | None

    def witness_proof(self) -> dict[str, Any]:
        return {
            "control_dir": str(self.control_dir),
            "witness_nonce": self.witness_nonce,
            "server_pid": self.server_pid,
            "mcp_url_host": "127.0.0.1",
            "mcp_url_path": "/mcp",
            "qualified_process_bound": True,
        }

    def cleanup(self) -> None:
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            self._proc = None
        dispose_gate_a_mcp_control_dir(self.control_dir, allowed_parent=self.control_parent)


def materialize_control_dir(parent: Path) -> Path:
    nonce = secrets.token_hex(8)
    path = parent.resolve() / f"gate-a-mcp-control-{nonce}"
    path.mkdir(parents=False, exist_ok=False)
    return path


def prestarted_gate_a_mcp_sanitized_env(
    *,
    control_dir: Path,
    witness_nonce: str,
    capability: str,
    disposable_home: Path | None = None,
    evidence_root: Path | None = None,
    admitted_brief_hash: str | None = None,
) -> dict[str, str]:
    """Minimal child env for the prestarted HTTP server (no inherited secrets or MCP config)."""
    temp_root = canonical_harness_temp_root()
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(gate_a_repo_root()),
        "TMPDIR": str(temp_root),
        "GATE_A_MCP_CONTROL_DIR": str(control_dir),
        "GATE_A_MCP_WITNESS_NONCE": witness_nonce,
        "GATE_A_MCP_HTTP_CAPABILITY": capability,
        STAGE_WORKER_ENV: "1",
    }
    if disposable_home is not None:
        env["HOME"] = str(disposable_home.resolve())
    if evidence_root is not None:
        env["GATE_A_MCP_EVIDENCE_ROOT"] = str(evidence_root.resolve())
    if admitted_brief_hash:
        env["GATE_A_MCP_ADMITTED_BRIEF_HASH"] = admitted_brief_hash
    return env


def start_prestarted_gate_a_mcp(
    *,
    python_executable: str,
    control_parent: Path,
    disposable_home: Path | None = None,
    evidence_root: Path | None = None,
    admitted_brief_hash: str | None = None,
    witness_timeout_seconds: float = 200.0,
) -> PrestartedGateAMcp:
    """
    Spawn ``python -m dev.factory.gate_a_mcp serve-http`` and wait for a qualified witness.

    The capability token and witness live under *control_parent*, outside the Cursor workspace.
    """
    control_parent = control_parent.resolve()
    control_dir = materialize_control_dir(control_parent)
    witness_nonce = secrets.token_hex(16)
    capability = mint_capability_token()
    write_http_capability(control_dir, capability)

    env = prestarted_gate_a_mcp_sanitized_env(
        control_dir=control_dir,
        witness_nonce=witness_nonce,
        capability=capability,
        disposable_home=disposable_home,
        evidence_root=evidence_root,
        admitted_brief_hash=admitted_brief_hash,
    )

    proc = subprocess.Popen(
        [python_executable, "-m", "dev.factory.gate_a_mcp", "serve-http"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        token = read_http_capability(control_dir)
        witness = wait_for_qualified_witness(
            control_dir,
            expected_nonce=witness_nonce,
            expected_pid=proc.pid,
            capability_token=token,
            timeout_seconds=witness_timeout_seconds,
        )
        validate_live_witness(witness, expected_nonce=witness_nonce, expected_pid=proc.pid)
        if witness.server_pid != proc.pid:
            raise ProcessWitnessError("prestarted server pid does not match subprocess handle")
    except ProcessWitnessError:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        dispose_gate_a_mcp_control_dir(control_dir, allowed_parent=control_parent)
        raise

    return PrestartedGateAMcp(
        control_dir=control_dir,
        control_parent=control_parent,
        witness_nonce=witness_nonce,
        server_pid=proc.pid,
        capability_token=token,
        mcp_url=mcp_streamable_http_url(witness),
        _proc=proc,
    )
