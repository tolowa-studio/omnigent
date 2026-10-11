"""Process-bound Gate A MCP readiness witness (no receipt secrets)."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

WITNESS_FILENAME = "qualified_witness.json"
CAPABILITY_FILENAME = "http_capability"
ENDPOINT_FILENAME = "endpoint.json"
CONTROL_DIR_NAME_PREFIX = "gate-a-mcp-control-"
_LOOPBACK_BIND_HOST = "127.0.0.1"


class ProcessWitnessError(RuntimeError):
    """Witness missing, stale, or inconsistent with the live prestarted server."""


class LoopbackTransportNotReady(ProcessWitnessError):
    """Loopback MCP is not accepting authenticated transport yet (startup retry only)."""


@dataclass(frozen=True)
class QualifiedProcessWitness:
    """Harness-visible proof that one MCP server process finished Seatbelt preflight."""

    witness_nonce: str
    server_pid: int
    listen_host: str
    listen_port: int
    mcp_url_path: str
    qualified_for_gate_a: bool
    minted_monotonic: float
    settle_observed_seconds: float
    order_id: str

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def _witness_path(control_dir: str | Path) -> Path:
    return Path(control_dir).resolve() / WITNESS_FILENAME


def capability_path(control_dir: str | Path) -> Path:
    return Path(control_dir).resolve() / CAPABILITY_FILENAME


def endpoint_path(control_dir: str | Path) -> Path:
    return Path(control_dir).resolve() / ENDPOINT_FILENAME


def mint_capability_token() -> str:
    return secrets.token_urlsafe(32)


def write_http_capability(control_dir: str | Path, token: str) -> Path:
    path = capability_path(control_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def read_http_capability(control_dir: str | Path) -> str:
    raw = capability_path(control_dir).read_text(encoding="utf-8").strip()
    if not raw:
        raise ProcessWitnessError("http capability file empty")
    return raw


def write_qualified_witness(control_dir: str | Path, witness: QualifiedProcessWitness) -> Path:
    path = _witness_path(control_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(witness.to_json_dict(), sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_qualified_witness(control_dir: str | Path) -> QualifiedProcessWitness:
    path = _witness_path(control_dir)
    if not path.is_file():
        raise ProcessWitnessError(f"witness missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProcessWitnessError(f"witness unreadable: {path}") from exc
    if not isinstance(data, dict):
        raise ProcessWitnessError("witness payload must be an object")
    try:
        return QualifiedProcessWitness(
            witness_nonce=str(data["witness_nonce"]),
            server_pid=int(data["server_pid"]),
            listen_host=str(data["listen_host"]),
            listen_port=int(data["listen_port"]),
            mcp_url_path=str(data["mcp_url_path"]),
            qualified_for_gate_a=bool(data["qualified_for_gate_a"]),
            minted_monotonic=float(data["minted_monotonic"]),
            settle_observed_seconds=float(data["settle_observed_seconds"]),
            order_id=str(data["order_id"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProcessWitnessError("witness payload missing required fields") from exc


def stamp_gate_a_mcp_success_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the serving process identity to a success payload (witness nonce is not a secret)."""
    witness_nonce = os.environ.get("GATE_A_MCP_WITNESS_NONCE")
    if not witness_nonce:
        return payload
    stamped = dict(payload)
    stamped["witness_nonce"] = witness_nonce
    stamped["server_pid"] = os.getpid()
    return stamped


def write_endpoint_descriptor(
    control_dir: str | Path,
    *,
    url: str,
    witness_nonce: str,
    server_pid: int,
) -> Path:
    payload = {
        "url": url,
        "witness_nonce": witness_nonce,
        "server_pid": server_pid,
    }
    path = endpoint_path(control_dir)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_endpoint_descriptor(control_dir: str | Path) -> dict[str, Any]:
    path = endpoint_path(control_dir)
    if not path.is_file():
        raise ProcessWitnessError(f"endpoint descriptor missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProcessWitnessError(f"endpoint descriptor unreadable: {path}") from exc
    if not isinstance(data, dict):
        raise ProcessWitnessError("endpoint descriptor must be an object")
    return data


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def validate_live_witness(
    witness: QualifiedProcessWitness,
    *,
    expected_nonce: str | None = None,
    expected_pid: int | None = None,
) -> None:
    if not witness.qualified_for_gate_a:
        raise ProcessWitnessError("witness reports qualified_for_gate_a=false")
    if expected_nonce is not None and witness.witness_nonce != expected_nonce:
        raise ProcessWitnessError("witness nonce mismatch (stale or replaced server)")
    if expected_pid is not None and witness.server_pid != expected_pid:
        raise ProcessWitnessError("witness pid mismatch (process replacement)")
    if not _pid_alive(witness.server_pid):
        raise ProcessWitnessError(f"witness server pid {witness.server_pid} is not alive")


def _loopback_port_accepting(
    host: str,
    port: int,
    *,
    timeout_seconds: float = 0.25,
) -> bool:
    if host not in (_LOOPBACK_BIND_HOST, "localhost"):
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def wait_for_qualified_witness(
    control_dir: str | Path,
    *,
    expected_nonce: str,
    expected_pid: int | None = None,
    capability_token: str | None = None,
    timeout_seconds: float = 200.0,
    poll_interval_seconds: float = 0.25,
    transport_startup_retry_seconds: float = 30.0,
) -> QualifiedProcessWitness:
    """Poll until the prestarted server is process-bound, listening, and MCP-ready."""
    deadline = time.monotonic() + timeout_seconds
    last_error = "witness not yet published"
    while time.monotonic() < deadline:
        try:
            witness = load_qualified_witness(control_dir)
            validate_live_witness(
                witness,
                expected_nonce=expected_nonce,
                expected_pid=expected_pid,
            )
            if not _loopback_port_accepting(witness.listen_host, witness.listen_port):
                raise LoopbackTransportNotReady(
                    f"loopback port {witness.listen_host}:{witness.listen_port} "
                    "not accepting connections",
                )
            if capability_token is not None:
                probe_authenticated_loopback_mcp_transport(
                    mcp_streamable_http_url(witness),
                    capability_token,
                    witness=witness,
                    startup_retry_seconds=transport_startup_retry_seconds,
                )
            return witness
        except LoopbackTransportNotReady as exc:
            last_error = str(exc)
        except ProcessWitnessError as exc:
            if _is_fail_closed_witness_error(exc):
                raise
            last_error = str(exc)
        time.sleep(poll_interval_seconds)
    raise ProcessWitnessError(
        f"timed out after {timeout_seconds:.0f}s waiting for qualified witness: {last_error}",
    )


def _is_fail_closed_witness_error(exc: ProcessWitnessError) -> bool:
    if isinstance(exc, LoopbackTransportNotReady):
        return False
    msg = str(exc)
    fail_closed_markers = (
        "not alive",
        "nonce mismatch",
        "pid mismatch",
        "qualified_for_gate_a=false",
        "unauthorized",
        "http_status=401",
        "http_status=421",
        "Host header",
        "initialize/list-tools failed",
    )
    return any(marker in msg for marker in fail_closed_markers)


def mcp_streamable_http_url(witness: QualifiedProcessWitness) -> str:
    host = witness.listen_host
    path = (
        witness.mcp_url_path
        if witness.mcp_url_path.startswith("/")
        else f"/{witness.mcp_url_path}"
    )
    return f"http://{host}:{witness.listen_port}{path}"


def loopback_allowed_hosts_for_port(
    listen_port: int, *, listen_host: str = _LOOPBACK_BIND_HOST
) -> list[str]:
    """Exact Host header values permitted for streamable HTTP (DNS rebinding protection)."""
    if listen_host not in (_LOOPBACK_BIND_HOST, "localhost"):
        raise ValueError("gate A prestarted MCP must bind loopback 127.0.0.1")
    if listen_port <= 0 or listen_port > 65535:
        raise ValueError("invalid listen port for loopback MCP")
    return [f"{_LOOPBACK_BIND_HOST}:{listen_port}"]


def _control_dir_within_parent(control_dir: Path, allowed_parent: Path) -> bool:
    try:
        control_dir.resolve().relative_to(allowed_parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def dispose_gate_a_mcp_control_dir(
    control_dir: str | Path,
    *,
    allowed_parent: str | Path,
) -> bool:
    """
    Remove a disposable Gate A MCP control directory after server shutdown.

    Only deletes directories created by ``materialize_control_dir`` under *allowed_parent*.
    """
    root = Path(control_dir)
    if not root.name.startswith(CONTROL_DIR_NAME_PREFIX):
        return False
    parent = Path(allowed_parent)
    if not _control_dir_within_parent(root, parent):
        return False
    resolved = root.resolve()
    if not resolved.is_dir():
        return False
    shutil.rmtree(resolved)
    return True


def _authorized_headers(capability_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {capability_token.strip()}"}


def _connection_refused(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in {61, 111}:
        return True
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        return _connection_refused(reason)
    return False


def _http_get_status(url: str, headers: dict[str, str], *, timeout_seconds: float = 10.0) -> int:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except urllib.error.URLError as exc:
        if _connection_refused(exc):
            raise LoopbackTransportNotReady(
                "loopback MCP transport not accepting connections"
            ) from None
        raise ProcessWitnessError(
            f"loopback MCP transport unreachable ({type(exc.reason).__name__})",
        ) from None


async def _mcp_initialize_and_list_tools(url: str, headers: dict[str, str]) -> None:
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamablehttp_client(url=url, headers=headers, timeout=30.0),
        )
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        await session.list_tools()


def probe_authenticated_loopback_mcp_transport(
    url: str,
    capability_token: str,
    *,
    witness: QualifiedProcessWitness | None = None,
    startup_retry_seconds: float = 30.0,
    retry_interval_seconds: float = 0.1,
) -> None:
    """
    Reject false-ready witnesses: loopback HTTP must accept auth and complete
    MCP initialize/list-tools.
    """
    headers = _authorized_headers(capability_token)
    deadline = time.monotonic() + startup_retry_seconds
    last_not_ready = "loopback MCP transport not ready"
    while True:
        if witness is not None:
            validate_live_witness(witness)
            if not _loopback_port_accepting(witness.listen_host, witness.listen_port):
                last_not_ready = (
                    f"loopback port {witness.listen_host}:{witness.listen_port} "
                    "not accepting connections"
                )
                if time.monotonic() >= deadline:
                    raise LoopbackTransportNotReady(last_not_ready)
                time.sleep(retry_interval_seconds)
                continue
        try:
            status = _http_get_status(url, headers)
            if status == 421:
                endpoint = (
                    f"http://{_LOOPBACK_BIND_HOST}:{witness.listen_port}/mcp"
                    if witness
                    else "loopback MCP endpoint"
                )
                raise ProcessWitnessError(
                    "loopback MCP transport rejected Host header "
                    f"(http_status=421 endpoint={endpoint})",
                )
            if status == 401:
                raise ProcessWitnessError("loopback MCP transport unauthorized (http_status=401)")
            import anyio

            anyio.run(_mcp_initialize_and_list_tools, url, headers)
            return
        except LoopbackTransportNotReady as exc:
            last_not_ready = str(exc)
            if time.monotonic() >= deadline:
                raise LoopbackTransportNotReady(last_not_ready) from None
            time.sleep(retry_interval_seconds)
        except ProcessWitnessError:
            raise
        except Exception as exc:  # noqa: BLE001 — retry until deadline on transport startup races
            endpoint = (
                f"http://{_LOOPBACK_BIND_HOST}:{witness.listen_port}{witness.mcp_url_path}"
                if witness
                else "loopback MCP endpoint"
            )
            if _connection_refused(exc) and time.monotonic() < deadline:
                last_not_ready = "loopback MCP transport not accepting connections"
                time.sleep(retry_interval_seconds)
                continue
            raise ProcessWitnessError(
                "loopback MCP initialize/list-tools failed "
                f"(endpoint={endpoint} detail={type(exc).__name__})",
            ) from None


def qualify_process_witness_transport(
    witness: QualifiedProcessWitness,
    capability_token: str,
    *,
    startup_retry_seconds: float = 30.0,
) -> None:
    """Process-bound witness is not ready until the live server passes transport probe."""
    validate_live_witness(witness)
    url = mcp_streamable_http_url(witness)
    probe_authenticated_loopback_mcp_transport(
        url,
        capability_token,
        witness=witness,
        startup_retry_seconds=startup_retry_seconds,
    )
