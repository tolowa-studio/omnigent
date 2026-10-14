"""Focused security and behavior checks for Claude's runner-owned hook IPC."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native.bridge import (
    _write_json_file,
    prepare_bridge_dir,
    read_active_session_id,
    read_transcript_items_since,
    read_transcript_path,
    record_hook_event,
)
from omnigent.harnesses.claude_native.forwarder import (
    _diagnostic_file_size,
    _log_new_observer_hook_stderr,
    _managed_rotation_target,
)
from omnigent.harnesses.claude_native.status import sync_raw_status_context
from omnigent.runner.claude_native_ipc import start_claude_native_ipc
from omnigent.runner.native_hook_ipc import get_native_hook_ipc, post_native_hook_ipc
from omnigent.runner.turn_routing import (
    trace_turn_routing,
    turn_routing_marker_session,
    write_turn_routing_marker,
)


@pytest.mark.asyncio
async def test_claude_ipc_relay_tool_requires_runner_policy_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import claude_native_ipc

    socket_root = Path(tempfile.mkdtemp(prefix="nhi-", dir="/tmp"))
    monkeypatch.setenv("OMNIGENT_NATIVE_IPC_ROOT", str(socket_root))
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = tmp_path / "root" / "bridge"
    bridge_dir.mkdir(parents=True)
    _write_json_file(
        bridge_dir / "bridge.json",
        {"bridge_id": "bridge-a", "active_session_id": "session-a"},
    )
    monkeypatch.setattr(
        claude_native_ipc,
        "_read_relay_tool_specs",
        lambda _bridge: [{"name": "sys_privileged"}],
    )
    dispatched: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_native_ipc,
        "_call_relay_tool",
        lambda _bridge, name, args: (
            dispatched.append((name, args)) or {"content": [{"type": "text", "text": "done"}]}
        ),
    )
    verdict = "POLICY_ACTION_DENY"
    evaluated: list[dict[str, object]] = []

    def respond(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "runner_id": "runner-a",
                    "labels": {claude_bridge.BRIDGE_ID_LABEL_KEY: "bridge-a"},
                },
            )
        assert req.url.path == "/v1/sessions/session-a/policies/evaluate"
        evaluated.append(json.loads(req.content))
        return httpx.Response(200, json={"result": verdict})

    async with httpx.AsyncClient(
        base_url="http://ap.test", transport=httpx.MockTransport(respond)
    ) as client:
        ipc = start_claude_native_ipc(
            bridge_dir=bridge_dir,
            bridge_id="bridge-a",
            session_id="session-a",
            expected_runner_id="runner-a",
            server_client=client,
            server_url="http://ap.test",
        )
        try:
            payload = {"name": "sys_privileged", "arguments": {"command": "run"}}
            denied = await asyncio.to_thread(
                post_native_hook_ipc, bridge_dir, "/tool-call", payload
            )
            assert "denied" in json.dumps(denied)
            assert dispatched == []
            assert evaluated[-1]["event"]["data"] == {
                "name": "sys_privileged",
                "arguments": {"command": "run"},
            }
            verdict = "POLICY_ACTION_ALLOW"
            allowed = await asyncio.to_thread(
                post_native_hook_ipc, bridge_dir, "/tool-call", payload
            )
            assert allowed == {"result": {"content": [{"type": "text", "text": "done"}]}}
            assert dispatched == [("sys_privileged", {"command": "run"})]
            unknown = await asyncio.to_thread(
                post_native_hook_ipc,
                bridge_dir,
                "/tool-call",
                {"name": "outside", "arguments": {}},
            )
            assert unknown is None
            assert len(dispatched) == 1
        finally:
            await asyncio.to_thread(ipc.close)
            shutil.rmtree(socket_root)


def test_claude_ipc_keeps_local_mcp_tools_without_exposing_bridge_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import native_hook_ipc

    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "hook-ipc").mkdir(parents=True)
    _write_json_file(
        bridge_dir / "hook-ipc" / "local-tools.json",
        {"workspace": str(tmp_path)},
    )
    monkeypatch.setenv("OMNIGENT_NATIVE_HOOK_IPC", "1")
    monkeypatch.setattr(
        native_hook_ipc,
        "post_native_hook_ipc",
        lambda _bridge, path, _payload, **_kwargs: {"tools": []} if path == "/tool-list" else None,
    )

    class LocalTool:
        def get_schema(self) -> dict[str, object]:
            return {"function": {"name": "sys_local", "parameters": {"type": "object"}}}

        def invoke(self, raw: str, context: object) -> str:
            assert json.loads(raw) == {"value": 1}
            assert context.workspace == tmp_path
            return "local result"

    tools = {"sys_local": LocalTool()}
    schemas = claude_bridge._combined_mcp_tool_schemas(tools, bridge_dir)
    assert [row["name"] for row in schemas] == ["sys_local"]
    assert claude_bridge._call_mcp_tool(
        {"name": "sys_local", "arguments": {"value": 1}}, tools, bridge_dir
    ) == {"content": [{"type": "text", "text": "local result"}]}


def test_native_hook_ipc_rejects_invalid_calls_without_leaking_handler_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.native_hook_ipc import start_native_hook_ipc

    socket_root = Path(tempfile.mkdtemp(prefix="nhi-", dir="/tmp"))
    monkeypatch.setenv("OMNIGENT_NATIVE_IPC_ROOT", str(socket_root))
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    def fail(_session_id: str, _payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("private backend detail")

    ipc = start_native_hook_ipc(
        bridge_dir=bridge_dir,
        bridge_id="bridge-a",
        session_id="session-a",
        handlers={"/tool-call": fail},
    )
    try:
        advert = json.loads((bridge_dir / "hook-ipc" / "runner-ipc.json").read_text())
        auth = {"Authorization": f"Bearer {advert['ipc_capability']}"}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(ipc.socket_path))) as client:
            assert client.post("http://omnigent/tool-call", json={}, headers={}).status_code == 401
            assert client.post("http://omnigent/unknown", json={}, headers=auth).status_code == 404
            with socket.socket(socket.AF_UNIX) as raw:
                raw.connect(str(ipc.socket_path))
                raw.sendall(
                    b"POST /tool-call HTTP/1.1\r\nHost: omnigent\r\n"
                    + f"Authorization: {auth['Authorization']}\r\n".encode()
                    + b"Content-Length: 1048577\r\n\r\n"
                )
                assert b"400" in raw.recv(256).split(b"\r\n", 1)[0]
            failed = client.post("http://omnigent/tool-call", json={}, headers=auth)
            assert failed.status_code == 503
            assert "private backend detail" not in failed.text
    finally:
        ipc.close()
        shutil.rmtree(socket_root)


@pytest.mark.asyncio
async def test_claude_ipc_binds_backend_calls_to_ap_owned_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readable IPC capability cannot retarget a sibling or former owner."""
    socket_root = Path(tempfile.mkdtemp(prefix="nhi-", dir="/tmp"))
    monkeypatch.setenv("OMNIGENT_NATIVE_IPC_ROOT", str(socket_root))
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = tmp_path / "root" / "bridge"
    bridge_dir.mkdir(parents=True)
    (bridge_dir / "hook-output").mkdir()
    _write_json_file(
        bridge_dir / "bridge.json",
        {"bridge_id": "bridge-a", "active_session_id": "session-a", "token": "backend-secret"},
    )
    owner = "runner-a"
    seen: list[tuple[str, str]] = []

    def respond(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "runner_id": owner,
                    "labels": {claude_bridge.BRIDGE_ID_LABEL_KEY: "bridge-a"},
                },
            )
        if req.url.path.endswith("/policies/evaluate"):
            return httpx.Response(200, json={"action": "POLICY_ACTION_ALLOW"})
        if req.url.path.endswith("/hooks/permission-request"):
            return httpx.Response(200, json={"hookSpecificOutput": {"behavior": "allow"}})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        base_url="http://ap.test", transport=httpx.MockTransport(respond)
    ) as client:
        ipc = start_claude_native_ipc(
            bridge_dir=bridge_dir,
            bridge_id="bridge-a",
            session_id="session-a",
            expected_runner_id="runner-a",
            server_client=client,
            server_url="http://ap.test",
        )
        try:
            advert = json.loads((bridge_dir / "hook-ipc" / "runner-ipc.json").read_text())
            assert "backend-secret" not in json.dumps(advert)
            local_config = json.loads((bridge_dir / "hook-ipc" / "local-tools.json").read_text())
            assert "backend-secret" not in json.dumps(local_config)
            assert local_config == {}
            mcp_config = claude_bridge.build_mcp_config(bridge_dir)
            mcp_server = next(iter(mcp_config["mcpServers"].values()))
            assert mcp_server["env"]["OMNIGENT_NATIVE_HOOK_IPC"] == "1"
            assert mcp_server["env"]["OMNIGENT_CLAUDE_MCP_SOCKET_DIR"] == str(
                ipc.socket_dir / "mcp"
            )
            assert await asyncio.to_thread(get_native_hook_ipc, bridge_dir) is not None
            result = await asyncio.to_thread(
                post_native_hook_ipc,
                bridge_dir,
                "/policy-evaluate",
                {"evaluation_request": {"event": {"phase": "PHASE_TOOL_CALL"}}},
            )
            assert result == {"evaluation_response": {"action": "POLICY_ACTION_ALLOW"}}
            assert ("POST", "/v1/sessions/session-a/policies/evaluate") in seen
            owner = "runner-peer"
            denied = await asyncio.to_thread(
                post_native_hook_ipc,
                bridge_dir,
                "/policy-evaluate",
                {"evaluation_request": {"event": {"phase": "PHASE_TOOL_CALL"}}},
            )
            assert denied is None
            assert seen.count(("POST", "/v1/sessions/session-a/policies/evaluate")) == 1
        finally:
            await asyncio.to_thread(ipc.close)
            shutil.rmtree(socket_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clear", "fork"])
async def test_claude_ipc_rotation_transfers_before_switching_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    socket_root = Path(tempfile.mkdtemp(prefix="nhi-", dir="/tmp"))
    monkeypatch.setenv("OMNIGENT_NATIVE_IPC_ROOT", str(socket_root))
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = tmp_path / "root" / "bridge"
    bridge_dir.mkdir(parents=True)
    _write_json_file(
        bridge_dir / "bridge.json",
        {"bridge_id": "bridge-a", "active_session_id": "session-a"},
    )
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    route_calls: list[str] = []
    session_b_labels = (
        {"team": "alpha"} if kind == "fork" else {claude_bridge.BRIDGE_ID_LABEL_KEY: "bridge-a"}
    )
    from omnigent.harnesses.claude_native import hook as claude_hook
    from omnigent.runner import turn_routing

    old_router = SimpleNamespace(url="http://old-router", token="old-token")
    new_router = SimpleNamespace(url="http://new-router", token="new-token")
    monkeypatch.setattr(turn_routing, "ensure_session_turn_router", lambda *a, **k: new_router)
    monkeypatch.setattr(turn_routing, "shutdown_session_turn_router", lambda *a, **k: None)

    def route_post(url: str, token: str, *args: object) -> dict[str, object]:
        route_calls.append(url)
        assert token == "new-token"
        return {"action": "route", "decision_id": "route-b", "model": "claude-sonnet"}

    monkeypatch.setattr(claude_hook, "_route_turn_post", route_post)

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal session_b_labels
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        if req.method == "GET" and req.url.path == "/v1/sessions/session-a":
            return httpx.Response(
                200,
                json={
                    "id": "session-a",
                    "agent_id": "agent-a",
                    "runner_id": "runner-a",
                    "workspace": "/tmp/owned-workspace",
                    "labels": {claude_bridge.BRIDGE_ID_LABEL_KEY: "bridge-a", "team": "alpha"},
                },
            )
        if req.method == "GET" and req.url.path == "/v1/sessions/session-b":
            return httpx.Response(
                200,
                json={
                    "id": "session-b",
                    "agent_id": "agent-a",
                    "runner_id": "runner-a",
                    "labels": session_b_labels,
                },
            )
        if req.method == "POST" and req.url.path in {
            "/v1/sessions",
            "/v1/sessions/session-a/fork",
        }:
            return httpx.Response(201, json={"id": "session-b", "labels": session_b_labels})
        if req.method == "PATCH" and req.url.path == "/v1/sessions/session-b":
            if kind == "fork":
                assert body == {
                    "runner_id": "runner-a",
                    "labels": {
                        "team": "alpha",
                        claude_bridge.BRIDGE_ID_LABEL_KEY: "bridge-a",
                    },
                }
                session_b_labels = body["labels"]
            else:
                assert body == {"runner_id": "runner-a"}
            assert read_active_session_id(bridge_dir) == "session-a"
            return httpx.Response(200, json={"id": "session-b"})
        if req.method == "POST" and req.url.path.endswith("/transfer"):
            assert body == {"target_session_id": "session-b"}
            assert read_active_session_id(bridge_dir) == "session-a"
            return httpx.Response(200, json={"id": "terminal_claude_main"})
        if req.method == "PATCH" and req.url.path == "/v1/sessions/session-a":
            assert read_active_session_id(bridge_dir) == "session-b"
            return httpx.Response(200, json={"id": "session-a"})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    async with httpx.AsyncClient(
        base_url="http://ap.test", transport=httpx.MockTransport(respond)
    ) as client:
        ipc = start_claude_native_ipc(
            bridge_dir=bridge_dir,
            bridge_id="bridge-a",
            session_id="session-a",
            expected_runner_id="runner-a",
            server_client=client,
            server_url="http://ap.test",
            turn_router=old_router,
        )
        try:
            result = await asyncio.to_thread(
                post_native_hook_ipc, bridge_dir, "/rotate-session", {"kind": kind}
            )
            assert result == {"session_id": "session-b"}
            assert read_active_session_id(bridge_dir) == "session-b"
            assert json.loads(
                claude_bridge.rotation_receipt_path(bridge_dir, "session-a").read_text()
            ) == {"kind": kind, "old_session_id": "session-a", "new_session_id": "session-b"}
            routed = await asyncio.to_thread(
                post_native_hook_ipc, bridge_dir, "/route-turn", {"prompt": "new task"}
            )
            assert routed == {
                "action": "route",
                "decision_id": "route-b",
                "model": "claude-sonnet",
                "block": True,
            }
            assert route_calls == ["http://new-router/v1/sessions/session-b/route-turn"]
            assert (
                json.loads((bridge_dir / "turn_routing_done").read_text())["session_id"]
                == "session-b"
            )
            assert not (bridge_dir / "hook-output" / "turn_routing_done").exists()
        finally:
            await asyncio.to_thread(ipc.close)
            shutil.rmtree(socket_root)
    if kind == "clear":
        assert (
            "POST",
            "/v1/sessions",
            {
                "agent_id": "agent-a",
                "workspace": "/tmp/owned-workspace",
                "labels": {claude_bridge.BRIDGE_ID_LABEL_KEY: "bridge-a", "team": "alpha"},
            },
        ) in calls
    else:
        assert ("POST", "/v1/sessions/session-a/fork", {}) in calls


def test_claude_observer_discovers_plain_assistant_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    claude_root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    transcript = claude_root / "projects" / "owned" / "claude-a.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello from Claude"}],
                },
            }
        )
        + "\n"
    )
    bridge_dir = tmp_path / "root" / "bridge"
    bridge_dir.mkdir(parents=True)
    (bridge_dir / "hook-ipc").mkdir()
    (bridge_dir / "hook-ipc" / "managed.json").write_text('{"version":1}')
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "Stop", "session_id": "claude-a", "transcript_path": str(transcript)},
    )
    assert read_transcript_path(bridge_dir) == transcript
    _cursor, _response_id, items = read_transcript_items_since(
        transcript, 0, agent_name="claude-native-ui"
    )
    assert [
        (item.item_type, item.data.get("content"))
        for item in items
        if item.item_type == "message" and item.data.get("role") == "assistant"
    ] == [("message", [{"type": "output_text", "text": "Hello from Claude"}])]


def test_ipc_bridge_rejects_untrusted_transcript_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writable hook state cannot point the trusted forwarder at another file."""
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = tmp_path / "root" / "bridge"
    (bridge_dir / "hook-output").mkdir(parents=True)
    (bridge_dir / "hook-ipc").mkdir()
    (bridge_dir / "hook-ipc" / "managed.json").write_text('{"version":1}')
    claude_root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    project = claude_root / "projects" / "project-a"
    project.mkdir(parents=True)
    own = project / "claude-a.jsonl"
    own.write_text("{}\n")
    state_path = bridge_dir / "hook-output" / "state.json"
    _write_json_file(state_path, {"claude_session_id": "claude-a", "transcript_path": str(own)})
    assert read_transcript_path(bridge_dir) == own
    _write_json_file(
        state_path,
        {"claude_session_id": "claude-a", "transcript_path": str(tmp_path / "secret.jsonl")},
    )
    assert read_transcript_path(bridge_dir) is None
    link = project / "claude-a.jsonl"
    link.unlink()
    link.symlink_to(tmp_path / "secret.jsonl")
    _write_json_file(state_path, {"claude_session_id": "claude-a", "transcript_path": str(link)})
    assert read_transcript_path(bridge_dir) is None


def test_managed_observer_accepts_launcher_isolated_claude_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = tmp_path / "root" / "bridge"
    (bridge_dir / "hook-ipc").mkdir(parents=True)
    (bridge_dir / "hook-ipc" / "managed.json").write_text('{"version":1}')
    project = home / ".claude-lean" / "projects" / "workspace"
    project.mkdir(parents=True)
    transcript = project / "claude-a.jsonl"
    transcript.write_text("{}\n")

    record_hook_event(
        bridge_dir,
        {"hook_event_name": "Stop", "session_id": "claude-a", "transcript_path": str(transcript)},
    )
    assert read_transcript_path(bridge_dir) == transcript

    other = home / "other" / "projects" / "workspace"
    other.mkdir(parents=True)
    untrusted = other / "claude-a.jsonl"
    untrusted.write_text("{}\n")
    _write_json_file(
        bridge_dir / "hook-output" / "state.json",
        {"claude_session_id": "claude-a", "transcript_path": str(untrusted)},
    )
    assert read_transcript_path(bridge_dir) is None

    linked = home / ".claude-linked"
    linked.symlink_to(other)
    _write_json_file(
        bridge_dir / "hook-output" / "state.json",
        {
            "claude_session_id": "claude-a",
            "transcript_path": str(linked / "projects/workspace/claude-a.jsonl"),
        },
    )
    assert read_transcript_path(bridge_dir) is None


def test_runner_routing_writes_cannot_follow_hook_symlinks(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    output_dir = bridge_dir / "hook-output"
    output_dir.mkdir(parents=True)
    (bridge_dir / "hook-ipc").mkdir()
    (bridge_dir / "hook-ipc" / "managed.json").write_text('{"version":1}')
    target = tmp_path / "protected.txt"
    target.write_text("keep me")
    (output_dir / "turn_routing.log").symlink_to(target)
    (bridge_dir / "turn_routing_done").symlink_to(target)

    trace_turn_routing(bridge_dir, "route", "test")
    assert target.read_text() == "keep me"
    assert turn_routing_marker_session(bridge_dir) is None
    assert write_turn_routing_marker(bridge_dir, session_id="session-a")
    assert target.read_text() == "keep me"
    assert turn_routing_marker_session(bridge_dir) == "session-a"
    assert not (bridge_dir / "turn_routing_done").is_symlink()


def test_runner_observer_readers_reject_hook_output_symlinks(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    output_dir = bridge_dir / "hook-output"
    output_dir.mkdir(parents=True)
    protected = tmp_path / "protected.txt"
    protected.write_text('{"model":"forged","context_window_size":999999}')
    (output_dir / "context.json").symlink_to(protected)
    (output_dir / "context.raw").symlink_to(protected)
    (output_dir / claude_bridge.OBSERVER_HOOK_STDERR_FILE).symlink_to(protected)
    assert claude_bridge.read_claude_context_state(bridge_dir) is None
    assert claude_bridge.read_claude_status_model(bridge_dir) is None
    assert sync_raw_status_context(bridge_dir, None) is None
    assert _diagnostic_file_size(output_dir / "context.raw") is None
    assert (
        _log_new_observer_hook_stderr(bridge_dir=bridge_dir, session_id="session-a", byte_offset=0)
        == 0
    )


def test_forged_hook_rotation_requires_runner_owned_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = tmp_path / "root" / "bridge"
    bridge_dir.mkdir(parents=True)
    _write_json_file(bridge_dir / "bridge.json", {"active_session_id": "session-a"})
    assert (
        _managed_rotation_target(
            bridge_dir,
            kind="clear",
            previous_session_id="session-a",
            journal_target="session-peer",
        )
        is None
    )
    _write_json_file(
        bridge_dir / "rotation.json",
        {"kind": "clear", "old_session_id": "session-a", "new_session_id": "session-b"},
    )
    assert (
        _managed_rotation_target(
            bridge_dir, kind="clear", previous_session_id="session-a", journal_target="session-b"
        )
        is None
    )
    _write_json_file(bridge_dir / "bridge.json", {"active_session_id": "session-b"})
    assert (
        _managed_rotation_target(
            bridge_dir, kind="clear", previous_session_id="session-a", journal_target="session-b"
        )
        == "session-b"
    )
    assert (
        _managed_rotation_target(
            bridge_dir, kind="fork", previous_session_id="session-a", journal_target="session-b"
        )
        is None
    )
    assert (
        _managed_rotation_target(
            bridge_dir, kind="clear", previous_session_id="session-b", journal_target="session-b"
        )
        is None
    )
    _write_json_file(
        bridge_dir / "rotation.json",
        {"kind": "clear", "old_session_id": "session-b", "new_session_id": "session-c"},
    )
    _write_json_file(
        claude_bridge.rotation_receipt_path(bridge_dir, "session-a"),
        {"kind": "clear", "old_session_id": "session-a", "new_session_id": "session-b"},
    )
    _write_json_file(
        claude_bridge.rotation_receipt_path(bridge_dir, "session-b"),
        {"kind": "clear", "old_session_id": "session-b", "new_session_id": "session-c"},
    )
    _write_json_file(bridge_dir / "bridge.json", {"active_session_id": "session-c"})
    assert (
        _managed_rotation_target(
            bridge_dir, kind="clear", previous_session_id="session-a", journal_target="session-b"
        )
        == "session-b"
    )
    assert (
        _managed_rotation_target(
            bridge_dir, kind="clear", previous_session_id="session-b", journal_target="session-c"
        )
        == "session-c"
    )


def test_approval_marker_refreshes_and_creates_narrow_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "root" / "approval-waits")
    marker = claude_bridge.approval_wait_marker_path("session-a")
    assert not marker.parent.exists()
    claude_bridge.touch_approval_wait_marker(marker)
    assert marker.is_file()
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))
    claude_bridge.touch_approval_wait_marker(marker)
    assert marker.stat().st_mtime_ns > 1_000_000_000
    assert claude_bridge.approval_wait_is_fresh("session-a")


def test_overlapping_runner_approval_waits_keep_distinct_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = tmp_path / "root" / "bridge"
    (bridge_dir / "hook-ipc").mkdir(parents=True)
    (bridge_dir / "hook-ipc" / "managed.json").write_text('{"version":1}')
    first = claude_bridge.approval_wait_marker_path("session-a", bridge_dir=bridge_dir)
    second = claude_bridge.approval_wait_marker_path("session-a", bridge_dir=bridge_dir)
    assert first != second
    with claude_bridge.hold_approval_wait_marker(first):
        with claude_bridge.hold_approval_wait_marker(second):
            assert first.is_file() and second.is_file()
        assert first.is_file() and not second.exists()
        assert claude_bridge.approval_wait_is_fresh("session-a", bridge_dir=bridge_dir)
    assert not first.exists()


@pytest.mark.asyncio
async def test_managed_fork_never_seeds_from_untrusted_transcript_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.claude_native import forwarder

    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    claude_root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    bridge_dir = tmp_path / "root" / "bridge"
    (bridge_dir / "hook-ipc").mkdir(parents=True)
    (bridge_dir / "hook-ipc" / "managed.json").write_text('{"version":1}')
    _write_json_file(bridge_dir / "bridge.json", {"active_session_id": "session-b"})
    _write_json_file(
        bridge_dir / "rotation.json",
        {"kind": "fork", "old_session_id": "session-a", "new_session_id": "session-b"},
    )
    outside = tmp_path / "secret.jsonl"
    outside.write_text("secret\n")
    record = SimpleNamespace(
        event_cursor=1,
        byte_offset=10,
        fork_rotated_to="session-b",
        transcript_path=outside,
        claude_session_id="claude-b",
    )
    monkeypatch.setattr(
        forwarder,
        "_read_hook_events_for_state",
        lambda _bridge, _state: SimpleNamespace(records=[record]),
    )
    monkeypatch.setattr(forwarder, "_jsonl_cursor_fingerprint", lambda *_a: None)

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    seeded: list[Path | None] = []

    async def seed(*, bridge_dir: Path, transcript_path: Path | None) -> None:
        seeded.append(transcript_path)

    monkeypatch.setattr(forwarder, "_write_hook_state_async", noop)
    monkeypatch.setattr(forwarder, "_seed_fork_transcript_forward_state", seed)
    async with httpx.AsyncClient(base_url="http://ap.test") as client:
        rotated = await forwarder._maybe_rotate_session_on_fork(
            client=client,
            session_id="session-b",
            previous_session_id="session-a",
            bridge_dir=bridge_dir,
            state=forwarder.HookForwardState(event_cursor=0),
        )
        assert rotated == "session-b"
        assert seeded == []
        own = claude_root / "projects" / "owned" / "claude-b.jsonl"
        own.parent.mkdir(parents=True)
        own.write_text("{}\n")
        record.transcript_path = own
        rotated = await forwarder._maybe_rotate_session_on_fork(
            client=client,
            session_id="session-b",
            previous_session_id="session-a",
            bridge_dir=bridge_dir,
            state=forwarder.HookForwardState(event_cursor=0),
        )
        assert rotated == "session-b"
        assert seeded == [own]


def test_runner_notifier_rejects_spoofed_mcp_socket(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    output_dir = bridge_dir / "hook-output"
    output_dir.mkdir(parents=True)
    ipc_dir = bridge_dir / "hook-ipc"
    ipc_dir.mkdir()
    socket_dir = Path(tempfile.mkdtemp(prefix="nhi-", dir="/tmp"))
    mcp_dir = socket_dir / "mcp"
    mcp_dir.mkdir()
    (ipc_dir / "runner-ipc.json").write_text(
        json.dumps({"socket_path": str(socket_dir / "runner-ipc.sock")})
    )
    outside = socket_dir / "attacker.sock"
    owned = mcp_dir / "mcp-123.sock"
    attacker_socket = socket.socket(socket.AF_UNIX)
    control_socket = socket.socket(socket.AF_UNIX)
    try:
        attacker_socket.bind(str(outside))
        control_socket.bind(str(owned))
        (output_dir / "server.json").write_text(
            json.dumps({"socket": str(outside), "token": "local-control-only"})
        )
        with pytest.raises(RuntimeError, match="not ready"):
            claude_bridge._wait_for_server_info(bridge_dir, timeout_s=0.05)
        (output_dir / "server.json").write_text(
            json.dumps({"socket": str(owned), "token": "local-control-only"})
        )
        assert claude_bridge._wait_for_server_info(bridge_dir, timeout_s=0.05)["socket"] == str(
            owned
        )
    finally:
        attacker_socket.close()
        control_socket.close()
        shutil.rmtree(socket_dir)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt probe")
@pytest.mark.asyncio
async def test_claude_hook_records_under_narrow_seatbelt_grants(tmp_path: Path) -> None:
    """Run the real Python hook inside Seatbelt with bridge root unreadable."""
    import filelock

    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.inner.seatbelt_sandbox import SeatbeltSandboxBackend, _build_profile

    session_id = f"probe-{uuid.uuid4().hex}"
    workspace = Path(__file__).resolve().parents[2]
    bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
    socket_root = Path(tempfile.mkdtemp(prefix="nhi-", dir="/tmp"))
    previous_socket_root = os.environ.get("OMNIGENT_NATIVE_IPC_ROOT")
    os.environ["OMNIGENT_NATIVE_IPC_ROOT"] = str(socket_root)
    async with httpx.AsyncClient(base_url="http://ap.test") as client:
        ipc = start_claude_native_ipc(
            bridge_dir=bridge_dir,
            bridge_id=session_id,
            session_id=session_id,
            expected_runner_id="runner-probe",
            server_client=client,
            server_url="http://ap.test",
        )
        try:
            sandbox = OSEnvSandboxSpec(
                type="darwin_seatbelt",
                read_paths=[
                    str(bridge_dir / "claude-settings.json"),
                    str(bridge_dir / "hook-output"),
                    str(bridge_dir / "hook-ipc"),
                    str(ipc.socket_dir),
                    str(Path(sys.prefix)),
                    str(Path(filelock.__file__).parents[1]),
                    str(Path(sys.executable).resolve().parents[1]),
                ],
                write_paths=[str(bridge_dir / "hook-output")],
            )
            policy = SeatbeltSandboxBackend().resolve(
                OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=sandbox), workspace
            )
            profile = _build_profile(policy, workspace, argv=[sys.executable])
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "/usr/bin/sandbox-exec",
                    "-p",
                    profile,
                    sys.executable,
                    "-I",
                    "-m",
                    "omnigent.harnesses.claude_native.hook",
                    "--bridge-dir",
                    str(bridge_dir),
                ],
                input=json.dumps(
                    {"hook_event_name": "Stop", "session_id": "claude-probe"}
                ).encode(),
                capture_output=True,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(Path.home()),
                    "TMPDIR": str(bridge_dir / "hook-output" / "tmp"),
                    "OMNIGENT_CLAUDE_TRUSTED_TMP_PARENT": str(claude_bridge._TRUSTED_PARENT),
                    "OMNIGENT_NATIVE_HOOK_IPC": "1",
                },
                timeout=20,
            )
            assert result.returncode == 0, result.stderr.decode()
            assert (bridge_dir / "hook-output" / "hooks.jsonl").exists()
            protected = await asyncio.to_thread(
                subprocess.run,
                [
                    "/usr/bin/sandbox-exec",
                    "-p",
                    profile,
                    "/bin/cat",
                    str(bridge_dir / "bridge.json"),
                ],
                capture_output=True,
                check=False,
            )
            assert protected.returncode != 0
        finally:
            await asyncio.to_thread(ipc.close)
            shutil.rmtree(bridge_dir)
            shutil.rmtree(socket_root)
            if previous_socket_root is None:
                os.environ.pop("OMNIGENT_NATIVE_IPC_ROOT", None)
            else:
                os.environ["OMNIGENT_NATIVE_IPC_ROOT"] = previous_socket_root
