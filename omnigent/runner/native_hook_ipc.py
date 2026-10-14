"""Session-bound transport for native harness hook subprocesses.

The terminal and its model-driven shell share one filesystem sandbox. Hook
subprocesses therefore cannot safely read runner bearer files: anything they
can read, the shell can read too. This Unix socket exposes only named,
session-bound operations. Its IPC capability separates peer sessions but is
readable by this session's shell, so handlers must treat every payload as
untrusted and enforce policy before performing an action.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socketserver
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)
_IPC_DIR = "hook-ipc"
_ADVERTISEMENT = "runner-ipc.json"
_SOCKET = "runner-ipc.sock"
_MAX_BODY_BYTES = 1_048_576

# Intentionally no generic proxy endpoint. Each callback is bound to one
# runner-owned session, and must validate its own operation semantics.
OPERATION_PATHS = frozenset(
    {
        "/permission-request",
        "/active-session",
        "/policy-evaluate",
        "/rotate-session",
        "/route-turn",
        "/route-subagent",
        "/route-status",
        "/route-subagent",
        "/tool-list",
        "/tool-call",
        "/tools-changed",
    }
)
NativeHookHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


class _ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


@dataclass
class NativeHookIPC:
    """Runner-owned transport handle for one session and one bridge."""

    bridge_dir: Path
    session_id: str
    socket_path: Path
    socket_dir: Path
    server: _ThreadingUnixHTTPServer
    _thread: threading.Thread
    _socket_inode: int

    def close(self) -> None:
        """Stop this server without removing a successor's rendezvous files."""
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)
        with contextlib.suppress(OSError):
            if self.socket_path.stat().st_ino == self._socket_inode:
                self.socket_path.unlink()
        with contextlib.suppress(OSError):
            self.socket_dir.rmdir()
        advert = self.bridge_dir / _IPC_DIR / _ADVERTISEMENT
        with contextlib.suppress(OSError, ValueError, KeyError):
            payload = json.loads(advert.read_text(encoding="utf-8"))
            if payload["socket_path"] == str(self.socket_path):
                advert.unlink()


def start_native_hook_ipc(
    *,
    bridge_dir: Path,
    bridge_id: str,
    session_id: str,
    handlers: Mapping[str, NativeHookHandler],
) -> NativeHookIPC:
    """Start a Unix HTTP server whose callbacks are fixed to *session_id*.

    The public ``hook-ipc`` subdirectory can be granted read access to the
    terminal sandbox without exposing sibling backend bearer/config files.
    It must stay outside terminal write grants. Its scoped IPC capability
    isolates peers but does not authorize a privileged operation.
    """
    unknown = set(handlers) - OPERATION_PATHS
    if unknown:
        raise ValueError(f"unknown native hook IPC operation: {sorted(unknown)}")
    if bridge_dir.is_symlink() or not bridge_dir.is_dir():
        raise ValueError("native hook IPC requires a real bridge directory")
    public_dir = bridge_dir / _IPC_DIR
    if public_dir.is_symlink():
        raise ValueError("native hook IPC directory must not be a symlink")
    public_dir.mkdir(mode=0o700, exist_ok=True)
    # macOS limits AF_UNIX paths to 104 bytes; a nested bridge path is often
    # longer. A short, random directory avoids collisions. Seatbelt does not
    # restrict Unix-socket connect by file-read grants, so peer isolation is
    # enforced by the per-session IPC capability below.
    socket_root = Path(
        os.environ.get("OMNIGENT_NATIVE_IPC_ROOT") or Path.home() / ".omnigent" / "native-ipc"
    )
    if socket_root.is_symlink():
        raise ValueError("native hook IPC socket root must not be a symlink")
    socket_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_dir = Path(tempfile.mkdtemp(prefix="ipc-", dir=socket_root))
    socket_path = socket_dir / _SOCKET
    if len(os.fsencode(socket_path)) >= 104:
        socket_dir.rmdir()
        raise ValueError("native hook IPC socket path exceeds macOS AF_UNIX limit")

    bound_handlers = dict(handlers)
    ipc_capability = secrets.token_urlsafe(32)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path != "/active-session":
                self._reply(404, {"error": "unknown operation"})
                return
            self._dispatch(path, {})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/active-session":
                self._dispatch(path, {})
                return
            callback = bound_handlers.get(path)
            if callback is None:
                self._reply(404, {"error": "unknown operation"})
                return
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > _MAX_BODY_BYTES:
                    raise ValueError("invalid body length")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
            except (ValueError, json.JSONDecodeError):
                self._reply(400, {"error": "invalid request"})
                return
            self._dispatch(path, body)

        def _authorized(self) -> bool:
            raw = self.headers.get("Authorization", "").encode("latin-1", "replace")
            expected = f"Bearer {ipc_capability}".encode("ascii")
            return secrets.compare_digest(raw, expected)

        def _dispatch(self, path: str, body: dict[str, Any]) -> None:
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            callback = bound_handlers.get(path)
            if callback is None:
                self._reply(404, {"error": "unknown operation"})
                return
            try:
                result = callback(session_id, body)
                if not isinstance(result, dict):
                    raise TypeError("native hook IPC handler must return an object")
            except Exception:  # noqa: BLE001 - never leak callback exceptions to the terminal
                _LOG.exception("native hook IPC operation failed for session %s", session_id)
                self._reply(503, {"error": "operation unavailable"})
                return
            self._reply(200, result)

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def log_message(self, format: str, *args: Any) -> None:
            _LOG.debug("native hook IPC: " + format, *args)

    try:
        server = _ThreadingUnixHTTPServer(str(socket_path), _Handler)
    except Exception:
        socket_dir.rmdir()
        raise
    try:
        os.chmod(socket_path, 0o600)
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_ADVERTISEMENT}.", dir=public_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "socket_path": str(socket_path),
                        "bridge_id": bridge_id,
                        "ipc_capability": ipc_capability,
                    },
                    stream,
                )
                stream.write("\n")
            os.replace(tmp_name, public_dir / _ADVERTISEMENT)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    except Exception:
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()
        with contextlib.suppress(OSError):
            socket_dir.rmdir()
        raise
    return NativeHookIPC(
        bridge_dir=bridge_dir,
        session_id=session_id,
        socket_path=socket_path,
        socket_dir=socket_dir,
        server=server,
        _thread=thread,
        _socket_inode=socket_path.stat().st_ino,
    )


def post_native_hook_ipc(
    bridge_dir: Path,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """POST a named hook request; never read or send a backend credential."""
    if path not in OPERATION_PATHS:
        raise ValueError("unknown native hook IPC operation")
    advert = bridge_dir / _IPC_DIR / _ADVERTISEMENT
    try:
        address = json.loads(advert.read_text(encoding="utf-8"))
        socket_path = address["socket_path"]
        capability = address["ipc_capability"]
        if Path(socket_path).name != _SOCKET:
            return None
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(
                "http://omnigent" + path,
                json=payload,
                headers={"Authorization": f"Bearer {capability}"},
            )
            response.raise_for_status()
            result = response.json()
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        return None
    return result if isinstance(result, dict) else None


def get_native_hook_ipc(
    bridge_dir: Path,
    *,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Read runner-owned active-session metadata without backend credentials."""
    advert = bridge_dir / _IPC_DIR / _ADVERTISEMENT
    try:
        address = json.loads(advert.read_text(encoding="utf-8"))
        socket_path = address["socket_path"]
        capability = address["ipc_capability"]
        if Path(socket_path).name != _SOCKET:
            return None
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.get(
                "http://omnigent/active-session",
                headers={"Authorization": f"Bearer {capability}"},
            )
            response.raise_for_status()
            result = response.json()
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        return None
    return result if isinstance(result, dict) else None
