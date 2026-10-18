"""Gate A real-operator task path (local, opt-in)."""

from __future__ import annotations

import os
from pathlib import Path

REAL_TASK_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_TASK"
REAL_TASK_ARTIFACTS_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS"
REAL_TASK_ARTIFACTS_ROOT_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT"
KEEP_LAST_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_KEEP_LAST"
KEEP_HOURS_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_KEEP_HOURS"
DEFAULT_ARTIFACTS_KEEP_LAST = 10
DEFAULT_ARTIFACTS_KEEP_HOURS = 48
REAL_TASK_RUN_PR_NUMBER_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_RUN_PR_NUMBER"
REAL_TASK_RUN_PR_STATE_ENV = "OMNIGENT_FACTORY_GATE_A_REAL_RUN_PR_STATE"

DEFAULT_CURSOR_EXECUTABLE = Path.home() / ".local/bin/motion-cursor-agent"

BUILD_MODEL = "composer-2.5"
REVIEW_MODEL = "grok-4.7-high"

REVIEW_PASS_MARKER = "REVIEW: PASS"

FORBIDDEN_CLI_FLAGS = frozenset({"--yolo", "--force", "-f"})

BUILD_TIMEOUT_SECONDS = 3600.0
REVIEW_TIMEOUT_SECONDS = 900.0
VERIFY_TIMEOUT_SECONDS = 600.0

REAL_TASK_CONFIG_KEYS = frozenset({"cli-config.json", "mcp.json"})

FORBIDDEN_REVIEW_TOOL_NAMES = frozenset(
    {
        "shell",
        "write",
        "edit",
        "delete",
        "webfetch",
        "mcp",
        "getmcptools",
    }
)

ALLOWED_REVIEW_TOOL_NAMES = frozenset({"read", "grep", "glob"})


def resolve_cursor_executable() -> str:
    override = os.environ.get("GATE_A_REAL_CURSOR_EXECUTABLE", "").strip()
    if override:
        return override
    if DEFAULT_CURSOR_EXECUTABLE.is_file():
        return str(DEFAULT_CURSOR_EXECUTABLE.resolve())
    raise FileNotFoundError(
        f"real-task Gate A requires {DEFAULT_CURSOR_EXECUTABLE} "
        "(or set GATE_A_REAL_CURSOR_EXECUTABLE)"
    )
