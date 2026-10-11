"""Stdio MCP server: exactly one tool for the internal stage work order."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from dev.factory.gate_a_mcp.bridge import (
    GateAMcpArgumentError,
    GateAMcpBridgeError,
    run_bound_internal_stage_order,
    validate_tool_arguments,
)
from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME

_ZERO_ARG_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_server = Server(MCP_SERVER_NAME)


def mcp_server_instance() -> Server:
    """Shared low-level MCP server used by stdio and prestarted HTTP transports."""
    return _server


def _coerce_arguments(raw: Any) -> Mapping[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise GateAMcpArgumentError("tool arguments must be an object")
    return raw


def _argument_error_result(detail: str) -> CallToolResult:
    message = f"Error executing tool {TOOL_NAME}: {detail}"
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        isError=True,
    )


def _handle_tool_call(arguments: Mapping[str, object]) -> CallToolResult:
    try:
        validate_tool_arguments(arguments)
        payload = run_bound_internal_stage_order(arguments)
    except GateAMcpArgumentError as exc:
        return _argument_error_result(str(exc))
    except GateAMcpBridgeError as exc:
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"ok": False, "error": "bridge_failed", "detail": str(exc)},
                    ),
                )
            ],
            isError=True,
        )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, sort_keys=True))],
        isError=False,
    )


@_server.list_tools()
async def _list_tools() -> list[Tool]:
    return [
        Tool(
            name=TOOL_NAME,
            description=(
                "Run the closed internal stage positive control under Seatbelt. "
                "No parameters are accepted; execution binding is server-owned."
            ),
            inputSchema=_ZERO_ARG_INPUT_SCHEMA,
        )
    ]


@_server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    if name != TOOL_NAME:
        return CallToolResult(
            content=[TextContent(type="text", text=f"unknown tool: {name}")],
            isError=True,
        )
    try:
        normalized = _coerce_arguments(arguments)
    except GateAMcpArgumentError as exc:
        return _argument_error_result(str(exc))
    return _handle_tool_call(normalized)


async def _run_async() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await _server.run(
            read_stream,
            write_stream,
            _server.create_initialization_options(),
        )


def run_stdio_server() -> None:
    asyncio.run(_run_async())


if __name__ == "__main__":
    run_stdio_server()
