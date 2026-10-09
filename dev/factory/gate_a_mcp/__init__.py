"""Disposable Gate A stdio MCP bridge (not wired into Omnigent runtime)."""

from .bridge import (
    GateAMcpArgumentError,
    GateAMcpBridgeError,
    run_bound_internal_stage_order,
    validate_tool_arguments,
)
from .constants import MCP_SERVER_NAME, TOOL_NAME

__all__ = [
    "MCP_SERVER_NAME",
    "TOOL_NAME",
    "GateAMcpArgumentError",
    "GateAMcpBridgeError",
    "run_bound_internal_stage_order",
    "validate_tool_arguments",
]
