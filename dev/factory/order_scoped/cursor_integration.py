"""Cursor native Shell integration status for the stage worker (library-only)."""

from __future__ import annotations

# Precise blocker: Cursor CLI + Omnigent v0.17.0 cannot route accepted work only
# through this adapter while forbidding bypass tools.
CURSOR_SHELL_INTEGRATION_BLOCKER = (
    "Cursor native Shell approval is relayed via tmux keystrokes and optional "
    "preToolUse hooks (omnigent.inner.cursor_policy_hook), but a standing "
    "Cursor shell allowlist entry can still execute after its hook row is removed "
    "from hooks.json — there is no Omnigent-owned API to (1) revoke durable "
    "allowlisted Shell commands, (2) force Shell argv through a closed manifest "
    "executor, or (3) disable Shell/MCP/bash tools independently of user UI state. "
    "Until Cursor exposes a hook that runs on every Shell spawn with deny-by-default "
    "and no persistent allowlist bypass, integration stops at this guarded library."
)


def cursor_shell_routes_only_through_adapter() -> bool:
    """Return False: safe Cursor-only routing is not available in this version."""
    return False
