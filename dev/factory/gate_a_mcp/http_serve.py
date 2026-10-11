"""Loopback streamable-HTTP Gate A MCP (prestarted, process-bound preflight)."""

from __future__ import annotations

import os
import socket
import time
from contextlib import asynccontextmanager

import anyio
import uvicorn
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from dev.factory.gate_a_mcp.preflight import (
    GateAPreflightError,
    run_startup_seatbelt_preflight_blocking,
)
from dev.factory.gate_a_mcp.process_witness import (
    QualifiedProcessWitness,
    loopback_allowed_hosts_for_port,
    mcp_streamable_http_url,
    read_http_capability,
    write_endpoint_descriptor,
    write_qualified_witness,
)
from dev.factory.gate_a_mcp.server import mcp_server_instance
from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID

_STREAMABLE_HTTP_PATH = "/mcp"
_BIND_HOST = "127.0.0.1"


def _pick_ephemeral_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((_BIND_HOST, 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class _AuthorizedStreamableHTTP:
    def __init__(self, inner: StreamableHTTPASGIApp, *, capability_token: str) -> None:
        self._inner = inner
        self._capability = capability_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        auth = request.headers.get("authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix) or auth[len(prefix) :].strip() != self._capability:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._inner(scope, receive, send)


def run_prestarted_http_server() -> None:
    """
    Run sync Seatbelt preflight, publish a process witness, then serve MCP over loopback HTTP.

    Environment:
    - ``GATE_A_MCP_CONTROL_DIR``: writable directory outside the trial workspace
    - ``GATE_A_MCP_HTTP_CAPABILITY``: bearer token (also stored under control dir)
    - ``GATE_A_MCP_WITNESS_NONCE``: nonce the harness expects in the witness
    """
    control_dir = os.environ.get("GATE_A_MCP_CONTROL_DIR")
    witness_nonce = os.environ.get("GATE_A_MCP_WITNESS_NONCE")
    if not control_dir or not witness_nonce:
        raise SystemExit("GATE_A_MCP_CONTROL_DIR and GATE_A_MCP_WITNESS_NONCE are required")
    capability = os.environ.get("GATE_A_MCP_HTTP_CAPABILITY") or read_http_capability(control_dir)

    receipt = run_startup_seatbelt_preflight_blocking()
    if receipt is None:
        raise SystemExit("preflight skipped or produced no receipt")
    if not receipt.qualified_for_gate_a:
        raise GateAPreflightError("seatbelt preflight not qualified_for_gate_a")

    port = _pick_ephemeral_port()
    server = mcp_server_instance()
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=loopback_allowed_hosts_for_port(port, listen_host=_BIND_HOST),
        ),
    )
    streamable = StreamableHTTPASGIApp(session_manager)
    endpoint = _AuthorizedStreamableHTTP(streamable, capability_token=capability)

    witness = QualifiedProcessWitness(
        witness_nonce=witness_nonce,
        server_pid=os.getpid(),
        listen_host=_BIND_HOST,
        listen_port=port,
        mcp_url_path=_STREAMABLE_HTTP_PATH,
        qualified_for_gate_a=True,
        minted_monotonic=time.monotonic(),
        settle_observed_seconds=receipt.settle_observed_seconds,
        order_id=receipt.order_id or INTERNAL_STAGE_ORDER_ID,
    )
    test_delay_bind_seconds = float(
        os.environ.get("GATE_A_MCP_TEST_DELAY_BIND_SECONDS", "0") or "0"
    )
    if test_delay_bind_seconds > 0:
        write_qualified_witness(control_dir, witness)
        write_endpoint_descriptor(
            control_dir,
            url=mcp_streamable_http_url(witness),
            witness_nonce=witness.witness_nonce,
            server_pid=witness.server_pid,
        )
        time.sleep(test_delay_bind_seconds)

    @asynccontextmanager
    async def _lifespan(_app: Starlette):
        if test_delay_bind_seconds <= 0:
            write_qualified_witness(control_dir, witness)
            write_endpoint_descriptor(
                control_dir,
                url=mcp_streamable_http_url(witness),
                witness_nonce=witness.witness_nonce,
                server_pid=witness.server_pid,
            )
        async with session_manager.run():
            yield

    starlette_app = Starlette(
        routes=[Route(_STREAMABLE_HTTP_PATH, endpoint=endpoint)],
        lifespan=_lifespan,
    )

    async def _serve() -> None:
        config = uvicorn.Config(
            starlette_app,
            host=_BIND_HOST,
            port=port,
            log_level="warning",
        )
        uvicorn_server = uvicorn.Server(config)
        await uvicorn_server.serve()

    anyio.run(_serve)
