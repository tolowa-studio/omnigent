"""Claude Code ``PreToolUse`` hook that routes native subagent spawns.

Registered by ``build_hook_settings`` on the ``Task|Agent`` matcher and
run as a subprocess per spawn. Reads the hook payload from stdin, asks
the runner's ``route-subagent`` endpoint what to do, and writes the
decision to stdout.

Always exits ``0``: routing must never be the reason a spawn fails. When
the endpoint is unadvertised, unreachable, or answers ``allow``, the hook
emits nothing and Claude proceeds unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from omnigent.inner.hook_scripts.subagent_router import run_route_subagent_main

_HARNESS = "claude-native"
_LABEL = "omnigent claude router hook"
_PROG = "omnigent-claude-router-hook"


def main(argv: list[str] | None = None) -> int:
    """
    Run the hook.

    :param argv: Command-line arguments, excluding the program name.
        ``None`` uses :data:`sys.argv`.
    :returns: Always ``0`` so a routing failure never blocks a spawn.
    """
    if os.environ.get("OMNIGENT_NATIVE_HOOK_IPC") == "1":
        from omnigent.inner.hook_scripts.subagent_router import parse_hook_args
        from omnigent.runner.native_hook_ipc import post_native_hook_ipc

        args = parse_hook_args(_PROG, list(sys.argv[1:] if argv is None else argv))
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            return 0
        if not isinstance(payload, dict) or not args.bridge_dir:
            return 0
        result = post_native_hook_ipc(
            Path(args.bridge_dir), "/route-subagent", {"hook": payload}, timeout=15
        )
        output = (result or {}).get("output")
        if isinstance(output, dict):
            sys.stdout.write(json.dumps(output))
        return 0
    return run_route_subagent_main(
        list(sys.argv[1:] if argv is None else argv),
        prog=_PROG,
        harness=_HARNESS,
        label=_LABEL,
    )


if __name__ == "__main__":
    sys.exit(main())
