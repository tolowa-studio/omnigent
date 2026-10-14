"""Runner-owned operations for one sandboxed Claude terminal.

The IPC capability only separates peer sockets. It is readable by the model's
shell, so every callback binds to the runner's active session and verifies the
AP owner and bridge label before touching backend state.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    _call_relay_tool,
    _mcp_error,
    _mcp_tool_schema_from_spec,
    _read_json_file,
    _read_relay_tool_specs,
    _write_json_file,
    approval_wait_marker_path,
    hold_approval_wait_marker,
    read_active_session_id,
    write_active_session_id,
)
from omnigent.runner.native_hook_ipc import NativeHookIPC, start_native_hook_ipc

_LOG = logging.getLogger(__name__)
_PERMISSION_TIMEOUT_S = 86400.0


def start_claude_native_ipc(
    *,
    bridge_dir: Path,
    bridge_id: str,
    session_id: str,
    expected_runner_id: str,
    server_client: httpx.AsyncClient,
    server_url: str,
    turn_router: Any = None,
    turn_routers: dict[str, Any] | None = None,
) -> NativeHookIPC:
    """Start a credential-free child interface backed by the trusted runner."""
    loop = asyncio.get_running_loop()
    routers = turn_routers if turn_routers is not None else {}
    if turn_router is not None:
        routers[session_id] = turn_router
    routing_enabled = turn_router is not None
    rotation_lock = asyncio.Lock()
    recent_rotations: list[float] = []

    def await_on_runner(coro: Any, timeout: float = 75.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    async def verify_active() -> str:
        active = read_active_session_id(bridge_dir)
        if not active or not expected_runner_id:
            raise RuntimeError("native session owner unavailable")
        response = await server_client.get(f"/v1/sessions/{quote(active, safe='')}", timeout=15)
        response.raise_for_status()
        snapshot = response.json()
        if not isinstance(snapshot, dict):
            raise RuntimeError("invalid session snapshot")
        labels = snapshot.get("labels")
        if (
            snapshot.get("runner_id") != expected_runner_id
            or not isinstance(labels, dict)
            or labels.get(BRIDGE_ID_LABEL_KEY) != bridge_id
        ):
            raise RuntimeError("native session no longer belongs to this runner bridge")
        return active

    async def active_metadata() -> dict[str, Any]:
        active = read_active_session_id(bridge_dir)
        if not active:
            raise RuntimeError("active session missing")
        from omnigent.conversation_browser import conversation_url

        return {
            "session_id": active,
            "conversation_url": conversation_url(server_url, active),
        }

    async def permission(body: dict[str, Any]) -> dict[str, Any]:
        active = await verify_active()
        hook = body.get("hook")
        if not isinstance(hook, dict):
            raise ValueError("permission hook must be an object")
        request_body = {
            **hook,
            "_omnigent_elicitation_id": f"elicit_claude_{secrets.token_hex(16)}",
        }
        path = f"/v1/sessions/{quote(active, safe='')}/hooks/permission-request"
        deadline = time.monotonic() + _PERMISSION_TIMEOUT_S
        failures = 0
        with hold_approval_wait_marker(approval_wait_marker_path(active, bridge_dir=bridge_dir)):
            while time.monotonic() < deadline:
                attempt_started = time.monotonic()
                try:
                    response = await server_client.post(path, json=request_body, timeout=360)
                    response.raise_for_status()
                    return {"output": response.text}
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code < 500:
                        raise
                    hard_failure = time.monotonic() - attempt_started < 10
                except httpx.HTTPError as exc:
                    hard_failure = (
                        isinstance(
                            exc,
                            (
                                httpx.ConnectError,
                                httpx.ConnectTimeout,
                                httpx.PoolTimeout,
                                httpx.ProxyError,
                            ),
                        )
                        or time.monotonic() - attempt_started < 10
                    )
                failures = failures + 1 if hard_failure else 0
                if failures >= 8:
                    break
                await asyncio.sleep(min(2**failures, 30) if hard_failure else 1)
        return {"output": ""}

    async def policy(body: dict[str, Any]) -> dict[str, Any]:
        active = await verify_active()
        evaluation = body.get("evaluation_request")
        if not isinstance(evaluation, dict):
            raise ValueError("evaluation request must be an object")
        response = await server_client.post(
            f"/v1/sessions/{quote(active, safe='')}/policies/evaluate",
            json=evaluation,
            timeout=_PERMISSION_TIMEOUT_S,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("invalid policy response")
        return {"evaluation_response": result}

    async def rotate(body: dict[str, Any]) -> dict[str, Any]:
        # The scoped IPC capability is visible to this session's shell. It
        # cannot authorize an unbounded AP create/transfer loop.
        async with rotation_lock:
            now = time.monotonic()
            recent_rotations[:] = [stamp for stamp in recent_rotations if now - stamp < 3600]
            if len(recent_rotations) >= 12:
                raise RuntimeError("Claude session rotation rate limit reached")
            recent_rotations.append(now)
            return await _rotate(body)

    async def _rotate(body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("kind")
        if kind not in {"clear", "fork"}:
            raise ValueError("invalid rotation kind")
        old_id = await verify_active()
        old_response = await server_client.get(f"/v1/sessions/{quote(old_id, safe='')}")
        old_response.raise_for_status()
        old = old_response.json()
        if not isinstance(old, dict):
            raise ValueError("invalid old session")
        if kind == "fork":
            created_response = await server_client.post(
                f"/v1/sessions/{quote(old_id, safe='')}/fork", json={}
            )
        else:
            agent_id = old.get("agent_id")
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("old session has no agent")
            labels = old.get("labels")
            if not isinstance(labels, dict):
                raise ValueError("old session has no labels")
            create_body: dict[str, Any] = {
                "agent_id": agent_id,
                "labels": {**labels, BRIDGE_ID_LABEL_KEY: bridge_id},
            }
            workspace = old.get("workspace")
            if isinstance(workspace, str) and workspace:
                create_body["workspace"] = workspace
            created_response = await server_client.post(
                "/v1/sessions",
                json=create_body,
            )
        created_response.raise_for_status()
        created = created_response.json()
        new_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(new_id, str) or not new_id:
            raise ValueError("rotation returned no session id")
        bind = await server_client.patch(
            f"/v1/sessions/{quote(new_id, safe='')}",
            json={"runner_id": expected_runner_id},
        )
        bind.raise_for_status()
        from omnigent.entities.session_resources import terminal_resource_id

        terminal_id = terminal_resource_id("claude", "main")
        transfer = await server_client.post(
            f"/v1/sessions/{quote(old_id, safe='')}/resources/terminals/"
            f"{quote(terminal_id, safe='')}/transfer",
            json={"target_session_id": new_id},
        )
        transfer.raise_for_status()
        _write_json_file(
            bridge_dir / "rotation.json",
            {"kind": kind, "old_session_id": old_id, "new_session_id": new_id},
        )
        write_active_session_id(bridge_dir, new_id)
        if routing_enabled:
            from omnigent.runner.turn_routing import (
                ensure_session_turn_router,
                shutdown_session_turn_router,
            )

            replacement = ensure_session_turn_router(
                new_id,
                bridge_dir=bridge_dir,
                server_client=server_client,
                harness="claude-native",
            )
            if replacement is not None:
                routers[new_id] = replacement
            old_router = routers.pop(old_id, None)
            if old_router is not None:
                await asyncio.to_thread(shutdown_session_turn_router, old_id, old_router)
        old_labels = {BRIDGE_ID_LABEL_KEY: f"{old_id}-cleared"} if kind == "clear" else {}
        unbind = await server_client.patch(
            f"/v1/sessions/{quote(old_id, safe='')}",
            json={"runner_id": "", "labels": old_labels} if old_labels else {"runner_id": ""},
        )
        if unbind.status_code >= 400:
            _LOG.warning("Claude rotation could not clear old runner binding: %s", old_id)
        return {"session_id": new_id}

    def route(body: dict[str, Any]) -> dict[str, Any]:
        active = await_on_runner(verify_active())
        current_router = routers.get(active)
        if current_router is None:
            return {"action": "allow", "block": False}
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"action": "allow", "block": False}
        from omnigent.harnesses.claude_native.hook import _route_turn_post
        from omnigent.runner.turn_routing import (
            HOOK_REQUEST_TIMEOUT_S,
            ROUTE_PATH_TEMPLATE,
            write_turn_routing_marker,
        )

        decision = _route_turn_post(
            current_router.url + ROUTE_PATH_TEMPLATE.format(session_id=quote(active, safe="")),
            current_router.token,
            {
                "harness": "claude-native",
                "prompt": prompt,
                "turn_id": None,
                "model": body.get("model"),
            },
            HOOK_REQUEST_TIMEOUT_S,
        )
        if not isinstance(decision, dict):
            return {"action": "allow", "block": False}
        model = decision.get("model")
        if decision.get("action") == "route" and isinstance(model, str) and model:
            decision_id = decision.get("decision_id")
            recorded = write_turn_routing_marker(
                bridge_dir,
                session_id=active,
                decision_id=decision_id if isinstance(decision_id, str) else None,
            )
            return {**decision, "block": recorded}
        if decision.get("terminal"):
            write_turn_routing_marker(bridge_dir, session_id=active)
        return {**decision, "block": False}

    def route_subagent(body: dict[str, Any]) -> dict[str, Any]:
        active = await_on_runner(verify_active())
        hook = body.get("hook")
        if not isinstance(hook, dict):
            raise ValueError("invalid subagent hook")
        from omnigent.inner.hook_scripts.subagent_router import route_pre_tool_use

        output = route_pre_tool_use(
            hook,
            harness="claude-native",
            router_dir=bridge_dir,
            bridge_dir=bridge_dir,
            session_id=active,
        )
        return {"output": output}

    async def tool_list() -> dict[str, Any]:
        await verify_active()
        specs = _read_relay_tool_specs(bridge_dir)
        return {"tools": [_mcp_tool_schema_from_spec(row) for row in specs]}

    async def tool_call(body: dict[str, Any]) -> dict[str, Any]:
        active = await verify_active()
        name = body.get("name")
        arguments = body.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("invalid tool call")
        allowed = {
            row.get("name") for row in _read_relay_tool_specs(bridge_dir) if isinstance(row, dict)
        }
        if name not in allowed:
            raise ValueError("tool not in this session's active relay")
        # The IPC capability is readable by the model's shell. Never let it
        # dispatch a trusted relay tool without a fresh runner-side policy
        # verdict bound to the AP-owned session and the exact arguments.
        try:
            with hold_approval_wait_marker(
                approval_wait_marker_path(active, bridge_dir=bridge_dir)
            ):
                verdict = await server_client.post(
                    f"/v1/sessions/{quote(active, safe='')}/policies/evaluate",
                    json={
                        "event": {
                            "type": "PHASE_TOOL_CALL",
                            "data": {"name": name, "arguments": arguments},
                            "context": {"harness": "claude-native"},
                        }
                    },
                    timeout=_PERMISSION_TIMEOUT_S,
                )
                verdict.raise_for_status()
                decision = verdict.json()
        except (httpx.HTTPError, ValueError):
            return {"result": _mcp_error("tool policy unavailable")}
        if not isinstance(decision, dict) or decision.get("result") != "POLICY_ACTION_ALLOW":
            return {"result": _mcp_error("tool policy denied")}
        result = await asyncio.to_thread(_call_relay_tool, bridge_dir, name, arguments)
        return {"result": result}

    def handle(path: str, _bound_session: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/active-session":
            return await_on_runner(active_metadata())
        if path == "/permission-request":
            return await_on_runner(permission(body), _PERMISSION_TIMEOUT_S + 5)
        if path == "/policy-evaluate":
            return await_on_runner(policy(body), _PERMISSION_TIMEOUT_S + 5)
        if path == "/rotate-session":
            return await_on_runner(rotate(body))
        if path == "/route-turn":
            return route(body)
        if path == "/route-subagent":
            return route_subagent(body)
        if path == "/tool-list":
            return await_on_runner(tool_list())
        if path == "/tool-call":
            return await_on_runner(tool_call(body), 3605)
        raise ValueError("unknown operation")

    paths = (
        "/active-session",
        "/permission-request",
        "/policy-evaluate",
        "/rotate-session",
        "/route-turn",
        "/route-subagent",
        "/tool-list",
        "/tool-call",
    )
    handlers: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] = {
        path: (lambda bound, body, path=path: handle(path, bound, body)) for path in paths
    }
    ipc = start_native_hook_ipc(
        bridge_dir=bridge_dir,
        bridge_id=bridge_id,
        session_id=session_id,
        handlers=handlers,
    )
    marker_root = bridge_dir / "approval-waits"
    try:
        config = _read_json_file(bridge_dir / "bridge.json")
        local_config = {key: config[key] for key in ("workspace", "sandbox") if key in config}
        _write_json_file(bridge_dir / "hook-ipc" / "local-tools.json", local_config)
        # A durable runner-owned mode bit keeps marker locations stable after
        # IPC teardown or a crash. The shell can read but cannot write it.
        _write_json_file(bridge_dir / "hook-ipc" / "managed.json", {"version": 1})
        if marker_root.is_symlink():
            raise ValueError("Claude approval marker root must not be a symlink")
        marker_root.mkdir(mode=0o700, exist_ok=True)
    except BaseException:
        ipc.close()
        raise
    return ipc
