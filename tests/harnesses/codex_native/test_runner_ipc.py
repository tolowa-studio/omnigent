"""Focused runner-side Codex native IPC callback checks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses.codex_native import hook
from omnigent.harnesses.codex_native.runner_ipc import codex_native_ipc_handlers
from omnigent.runner.turn_routing import turn_routing_marker_present


def _handlers(
    bridge: Path,
    *,
    server_client: httpx.AsyncClient | None = None,
    turn_router: object | None = None,
) -> dict:
    return dict(
        codex_native_ipc_handlers(
            bridge_dir=bridge,
            bridge_id="own-bridge",
            session_id="own-session",
            server_client=server_client,
            runner_loop=asyncio.get_running_loop(),
            turn_router=turn_router,
        )
    )


@pytest.mark.asyncio
async def test_policy_evaluate_binds_session_and_runner_model(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge"
    home = bridge / "codex-home"
    home.mkdir(parents=True)
    (home / "config.toml").write_text('model = "gpt-5.6-luna"\n', encoding="utf-8")
    seen: list[httpx.Request] = []

    def ap(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": "POLICY_ACTION_ALLOW"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(ap), base_url="http://ap"
    ) as client:
        handlers = _handlers(bridge, server_client=client)
        claimed = {"event": {"context": {"harness": "forged", "model": "free"}}}
        result = await asyncio.to_thread(
            handlers["/policy-evaluate"], "forged-peer", {"evaluation_request": claimed}
        )
    assert result == {"evaluation_response": {"result": "POLICY_ACTION_ALLOW"}}
    assert seen[0].url.path == "/v1/sessions/own-session/policies/evaluate"
    sent = json.loads(seen[0].content)
    assert sent["event"]["context"] == {"harness": "codex-native", "model": "gpt-5.6-luna"}
    assert claimed["event"]["context"] == {"harness": "forged", "model": "free"}


@pytest.mark.asyncio
async def test_route_marker_requires_switch_and_skips_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "bridge"
    (bridge / "hook-output").mkdir(parents=True)
    calls: list[tuple[str, object]] = []

    def route(url: str, *, json: dict, headers: dict, timeout: int) -> httpx.Response:
        calls.append((url, json))
        assert headers == {"Authorization": "Bearer runner-only"}
        assert timeout == 15
        return httpx.Response(
            200,
            json={"action": "route", "model": "gpt-5.6-luna", "decision_id": "decision-1"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", route)
    monkeypatch.setattr(hook, "_apply_thread_model", lambda *_: "switch declined")
    handlers = _handlers(
        bridge,
        turn_router=SimpleNamespace(url="http://router", token="runner-only"),
    )
    body = {"prompt": "do the work", "session_id": "forged-peer", "model": "live-model"}
    declined = await asyncio.to_thread(handlers["/route-turn"], "forged-peer", body)
    assert declined["block"] is False
    assert not turn_routing_marker_present(bridge, "own-session")
    assert calls[0][0].endswith("/sessions/own-session/route-turn")
    assert calls[0][1]["model"] == "live-model"

    monkeypatch.setattr(hook, "_apply_thread_model", lambda *_: None)
    switched = await asyncio.to_thread(handlers["/route-turn"], "forged-peer", body)
    assert switched == {"action": "route", "block": True, "model": "gpt-5.6-luna"}
    assert turn_routing_marker_present(bridge, "own-session")
    assert handlers["/route-status"]("forged-peer", {}) == {"marked": True}
    replay = await asyncio.to_thread(handlers["/route-turn"], "forged-peer", body)
    assert replay == {"action": "allow", "block": False}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_tool_call_uses_runner_relay_and_advertised_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "tool_relay.json").write_text(
        json.dumps(
            {
                "session_id": "own-session",
                "url": "http://relay",
                "token": "runner-only",
                "tools": [{"name": "allowed", "description": "one tool"}],
            }
        ),
        encoding="utf-8",
    )
    called: list[str] = []

    def relay(url: str, *, json: dict, headers: dict, timeout: int) -> httpx.Response:
        called.append(url)
        assert headers == {"Authorization": "Bearer runner-only"}
        assert json == {"name": "allowed", "arguments": {"x": 1}}
        assert timeout == 90
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", relay)
    handlers = _handlers(bridge)
    assert handlers["/tool-list"]("forged-peer", {}) == {
        "tools": [{"name": "allowed", "description": "one tool"}]
    }
    assert handlers["/tool-call"]("forged-peer", {"name": "unknown", "arguments": {}}) == {
        "error": "tool call unavailable"
    }
    allowed = handlers["/tool-call"]("forged-peer", {"name": "allowed", "arguments": {"x": 1}})
    assert allowed == {"result": {"content": [{"type": "text", "text": "ok"}]}}
    assert called == ["http://relay/tool"]
    assert "runner-only" not in json.dumps(allowed)


@pytest.mark.asyncio
async def test_subagent_route_rejects_stale_peer_advertisement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner.hook_scripts import subagent_router

    bridge = tmp_path / "bridge"
    bridge.mkdir()
    monkeypatch.setattr(
        subagent_router,
        "read_router_endpoint",
        lambda *_: SimpleNamespace(session_id="peer-session"),
    )
    monkeypatch.setattr(
        subagent_router,
        "route_pre_tool_use",
        lambda *_args, **_kwargs: pytest.fail("stale peer router was called"),
    )
    handlers = _handlers(bridge)
    assert handlers["/route-subagent"](
        "peer-session", {"payload": {"tool_name": "spawn_agent"}}
    ) == {"output": None}
