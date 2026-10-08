"""Gate A trial evidence recorder (CLI version, discovery, tool calls, markers)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev.factory.gate_a_trial.constants import (
    ALT_MCP_MARKER_FILENAME,
    SHELL_MARKER_FILENAME,
    TRANSCRIPT_DIR,
    WRITE_MARKER_FILENAME,
)
from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS
from dev.factory.gate_a_trial.secret_redact import redact_jsonable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_marker_entry_present(workspace: Path, filename: str) -> bool:
    """True when the marker path exists (including dangling symlinks)."""
    path = workspace / filename
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


@dataclass
class GateATrialTranscript:
    cursor_cli_executable: str = ""
    cursor_cli_version: str = ""
    cursor_config_dir: str = ""
    trial_workspace: str = ""
    effective_mcp_discovery: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    filesystem_markers: dict[str, bool] = field(default_factory=dict)
    requested_headless_model: str = ""
    executed_init_model: str = ""
    negative_attempts_exercised: bool = False
    negative_shell_exercised: bool = False
    negative_write_exercised: bool = False
    positive_attempt_exercised: bool = False
    positive_allowed_txt_evidence: str = ""
    negative_window_settled: bool = False
    negative_window_seconds: float = 0.0
    effective_config_hashes: dict[str, str] = field(default_factory=dict)
    workspace_mcp_removed: bool = False
    native_tools_denied_without_mcp: bool = False
    credential_store: str = "memory"
    isolated_home_dir: str = ""
    isolated_home_initial_proof: dict[str, Any] = field(default_factory=dict)
    without_mcp_discovery: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=_utc_now)
    ended_at: str = ""

    def record_tool_call(self, name: str, arguments: dict[str, Any], result: object) -> None:
        self.tool_calls.append(
            {
                "at": _utc_now(),
                "tool": name,
                "arguments": arguments,
                "result": result,
            }
        )

    def snapshot_markers(self, workspace: Path) -> None:
        self.filesystem_markers = {
            SHELL_MARKER_FILENAME: _workspace_marker_entry_present(workspace, SHELL_MARKER_FILENAME),
            WRITE_MARKER_FILENAME: _workspace_marker_entry_present(workspace, WRITE_MARKER_FILENAME),
            ALT_MCP_MARKER_FILENAME: _workspace_marker_entry_present(workspace, ALT_MCP_MARKER_FILENAME),
        }

    def settle_negative_window(self, seconds: float) -> None:
        if seconds <= 0:
            self.negative_window_seconds = 0.0
            self.negative_window_settled = False
            return
        time.sleep(seconds)
        self.negative_window_seconds = seconds
        self.negative_window_settled = True

    def synthetic_markers_present(self) -> list[str]:
        present = [
            name
            for name, exists in self.filesystem_markers.items()
            if exists is True
        ]
        return present

    def evaluate_preflight_completion(
        self,
        *,
        expect_negative_cli: bool,
        expect_positive_cli: bool,
        expect_without_mcp: bool,
    ) -> list[str]:
        """Fail-closed checklist after settle window and final marker snapshot."""
        reasons: list[str] = []
        markers = self.synthetic_markers_present()
        if markers:
            reasons.append(f"synthetic marker files present: {markers}")
        if expect_negative_cli:
            if not self.negative_shell_exercised:
                reasons.append("native Shell denial headless attempt did not satisfy enforcement")
            if not self.negative_write_exercised:
                reasons.append("native Write denial headless attempt did not satisfy enforcement")
            if not self.negative_attempts_exercised:
                reasons.append("native denial headless attempts did not satisfy enforcement")
            if self.negative_window_seconds < GATE_A_MIN_SETTLE_SECONDS:
                reasons.append(
                    f"negative settle window {self.negative_window_seconds}s < "
                    f"{GATE_A_MIN_SETTLE_SECONDS}s required"
                )
            if not self.negative_window_settled:
                reasons.append("negative_window_settled is false")
        if expect_positive_cli and not self.positive_attempt_exercised:
            reasons.append("positive Gate A MCP attempt incomplete")
        if expect_without_mcp and not self.native_tools_denied_without_mcp:
            reasons.append("without-MCP Shell/Write denials incomplete")
        return reasons

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cursor_cli_executable": self.cursor_cli_executable,
            "cursor_cli_version": self.cursor_cli_version,
            "cursor_config_dir": self.cursor_config_dir,
            "trial_workspace": self.trial_workspace,
            "effective_mcp_discovery": self.effective_mcp_discovery,
            "requested_headless_model": self.requested_headless_model,
            "executed_init_model": self.executed_init_model,
            "tool_calls": self.tool_calls,
            "filesystem_markers": self.filesystem_markers,
            "negative_attempts_exercised": self.negative_attempts_exercised,
            "negative_shell_exercised": self.negative_shell_exercised,
            "negative_write_exercised": self.negative_write_exercised,
            "positive_attempt_exercised": self.positive_attempt_exercised,
            "positive_allowed_txt_evidence": self.positive_allowed_txt_evidence,
            "negative_window_settled": self.negative_window_settled,
            "negative_window_seconds": self.negative_window_seconds,
            "effective_config_hashes": self.effective_config_hashes,
            "workspace_mcp_removed": self.workspace_mcp_removed,
            "native_tools_denied_without_mcp": self.native_tools_denied_without_mcp,
            "credential_store": self.credential_store,
            "isolated_home_dir": self.isolated_home_dir,
            "isolated_home_initial_proof": self.isolated_home_initial_proof,
            "without_mcp_discovery": self.without_mcp_discovery,
            "notes": self.notes,
        }

    def write(self, path: Path | None = None, *, redact_env: dict[str, str] | None = None) -> Path:
        self.ended_at = _utc_now()
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        out = path or (TRANSCRIPT_DIR / f"gate-a-trial-{_utc_now().replace(':', '-')}.json")
        payload = redact_jsonable(self.to_jsonable(), env=redact_env)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return out
