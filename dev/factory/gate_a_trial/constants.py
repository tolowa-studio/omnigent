"""Gate A Cursor CLI trial markers and paths."""

from __future__ import annotations

from pathlib import Path

TRIAL_ROOT = Path(__file__).resolve().parent
WORKSPACE = TRIAL_ROOT / "workspace"
TRANSCRIPT_DIR = TRIAL_ROOT / "transcripts"

# Managed Cursor CLI install (pin ahead of Homebrew `cursor-agent` shims).
MANAGED_CURSOR_AGENT_EXECUTABLE = Path.home() / ".local" / "bin" / "agent"

# Pinned for headless stream-json trial runs (requested CLI flag; init event is ground truth).
TRIAL_HEADLESS_MODEL = "composer-2.5"


def trial_repo_root() -> Path:
    """Repository root for ``dev/factory/gate_a_trial`` (``TRIAL_ROOT.parents[2]``)."""
    return TRIAL_ROOT.parents[2]

# Negative probes: only the bound MCP artifact ``allowed.txt`` may appear in worker checkouts.
SHELL_MARKER_FILENAME = ".gate_a_trial_shell_marker"
WRITE_MARKER_FILENAME = ".gate_a_trial_write_marker"
ALT_MCP_MARKER_FILENAME = ".gate_a_trial_alt_mcp_marker"
