"""Runner-owned callbacks for a Codex native bridge's scoped hook IPC."""

from __future__ import annotations

import asyncio
import copy
import json
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.codex_native.bridge import read_codex_config_model
from omnigent.runner.native_hook_ipc import NativeHookHandler
from omnigent.runner.turn_routing import (
    ROUTE_PATH_TEMPLATE,
    TurnRouter,
    turn_routing_marker_present,
    write_turn_routing_marker,
)


def codex_native_ipc_handlers(
    *,
    bridge_dir: Path,
    bridge_id: str,
    session_id: str,
    server_client: httpx.AsyncClient | None,
    runner_loop: asyncio.AbstractEventLoop,
    turn_router: TurnRouter | None,
) -> Mapping[str, NativeHookHandler]:
    """Bind operations to runner session state, never caller-supplied identity.

    The IPC capability is readable by the model's shell. Every operation here
    is therefore scoped to this session and applies the existing Omnigent
    policy or router decision. No callback returns a relay or AP bearer.
    """

    def active_session(_bound: str, _body: dict[str, Any]) -> dict[str, Any]:
        return {"session_id": session_id, "bridge_id": bridge_id}

    def policy_evaluate(_bound: str, body: dict[str, Any]) -> dict[str, Any]:
        untrusted_request = body.get("evaluation_request")
        if server_client is None or not isinstance(untrusted_request, dict):
            return {"error": "policy evaluation unavailable"}
        request = copy.deepcopy(untrusted_request)
        event = request.get("event")
        if not isinstance(event, dict):
            return {"error": "invalid policy request"}
        context = event.get("context")
        if isinstance(context, dict):
            context["harness"] = "codex-native"
            model = read_codex_config_model(bridge_dir)
            if model:
                context["model"] = model
            else:
                context.pop("model", None)
        url = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/policies/evaluate"
        pending = asyncio.run_coroutine_threadsafe(
            server_client.post(url, json=request), runner_loop
        )
        try:
            response = pending.result(timeout=86400)
            if response.status_code != 200:
                return {"error": f"policy server returned {response.status_code}"}
            verdict = response.json()
        except (ValueError, httpx.HTTPError, TimeoutError):
            return {"error": "policy evaluation unavailable"}
        if not isinstance(verdict, dict) or not verdict.get("result"):
            return {"error": "invalid policy verdict"}
        return {"evaluation_response": verdict}

    def route_turn(_bound: str, body: dict[str, Any]) -> dict[str, Any]:
        if turn_router is None:
            return {"action": "allow", "block": False}
        if turn_routing_marker_present(bridge_dir, session_id):
            return {"action": "allow", "block": False}
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"action": "allow", "block": False}
        request = {
            "harness": "codex-native",
            "prompt": prompt,
            "turn_id": body.get("turn_id") if isinstance(body.get("turn_id"), str) else None,
            # Codex's hook payload carries the live thread selection;
            # config.toml can still contain the launch model after /model.
            "model": body.get("model") if isinstance(body.get("model"), str) else None,
        }
        url = turn_router.url + ROUTE_PATH_TEMPLATE.format(
            session_id=urllib.parse.quote(session_id, safe="")
        )
        try:
            response = httpx.post(
                url,
                json=request,
                headers={"Authorization": f"Bearer {turn_router.token}"},
                timeout=15,
            )
            response.raise_for_status()
            decision = response.json()
        except (ValueError, httpx.HTTPError):
            return {"action": "allow", "block": False}
        if not isinstance(decision, dict):
            return {"action": "allow", "block": False}
        model = decision.get("model")
        if decision.get("action") == "route" and isinstance(model, str) and model:
            # The switch and marker are runner-side; the model shell cannot
            # forge a marker file or write a routing verdict in the bridge.
            from omnigent.harnesses.codex_native.hook import _apply_thread_model

            declined = _apply_thread_model(bridge_dir, model)
            if declined is not None:
                return {"action": "allow", "block": False, "reason": declined}
            decision_id = decision.get("decision_id")
            marked = write_turn_routing_marker(
                bridge_dir,
                session_id=session_id,
                decision_id=decision_id if isinstance(decision_id, str) else None,
            )
            return {"action": "route", "block": marked, "model": model}
        if decision.get("terminal"):
            decision_id = decision.get("decision_id")
            write_turn_routing_marker(
                bridge_dir,
                session_id=session_id,
                decision_id=decision_id if isinstance(decision_id, str) else None,
            )
        return {"action": "allow", "block": False}

    def route_status(_bound: str, _body: dict[str, Any]) -> dict[str, Any]:
        return {"marked": turn_routing_marker_present(bridge_dir, session_id)}

    def route_subagent(_bound: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = body.get("payload")
        if not isinstance(payload, dict):
            return {"output": None}
        # The legacy router advertisement and relay tool catalog contain
        # bearers, so only the runner executes this existing decision path.
        return {"output": _route_codex_subagent(payload, bridge_dir, session_id)}

    def _relay_info() -> dict[str, Any] | None:
        try:
            relay = json.loads((bridge_dir / "tool_relay.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(relay, dict) or relay.get("session_id") != session_id:
            return None
        return relay

    def tool_list(_bound: str, _body: dict[str, Any]) -> dict[str, Any]:
        relay = _relay_info()
        tools = relay.get("tools") if relay is not None else None
        return {"tools": tools if isinstance(tools, list) else []}

    def tool_call(_bound: str, body: dict[str, Any]) -> dict[str, Any]:
        relay = _relay_info()
        if relay is None:
            return {"error": "tool relay unavailable"}
        name = body.get("name")
        arguments = body.get("arguments")
        url = relay.get("url")
        token = relay.get("token")
        tool_specs = relay.get("tools")
        names = (
            {spec.get("name") for spec in tool_specs if isinstance(spec, dict)}
            if isinstance(tool_specs, list)
            else set()
        )
        if (
            not isinstance(name, str)
            or name not in names
            or not isinstance(arguments, dict)
            or not isinstance(url, str)
            or not isinstance(token, str)
        ):
            return {"error": "tool call unavailable"}
        try:
            response = httpx.post(
                url.rstrip("/") + "/tool",
                json={"name": name, "arguments": arguments},
                headers={"Authorization": f"Bearer {token}"},
                timeout=90,
            )
            response.raise_for_status()
            result = response.json()
        except (ValueError, httpx.HTTPError):
            return {"error": "tool relay unavailable"}
        return {"result": result}

    return {
        "/active-session": active_session,
        "/policy-evaluate": policy_evaluate,
        "/route-turn": route_turn,
        "/route-status": route_status,
        "/route-subagent": route_subagent,
        "/tool-list": tool_list,
        "/tool-call": tool_call,
    }


def _route_codex_subagent(
    payload: dict[str, Any], bridge_dir: Path, session_id: str
) -> dict[str, Any] | None:
    from omnigent.inner.hook_scripts.codex_router_hook import ROUTE_SEAMS
    from omnigent.inner.hook_scripts.subagent_router import (
        read_router_endpoint,
        route_pre_tool_use,
    )

    endpoint = read_router_endpoint(bridge_dir)
    if endpoint is None or endpoint.session_id != session_id:
        return None

    return route_pre_tool_use(
        payload,
        harness="codex-native",
        router_dir=bridge_dir,
        bridge_dir=bridge_dir,
        session_id=session_id,
        **ROUTE_SEAMS,
    )
