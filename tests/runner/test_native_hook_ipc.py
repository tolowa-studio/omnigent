"""Focused transport checks for native hook IPC."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.runner.native_hook_ipc import (
    get_native_hook_ipc,
    post_native_hook_ipc,
    start_native_hook_ipc,
)


def test_native_hook_ipc_binds_session_without_exposing_bearer(tmp_path: Path) -> None:
    bridge = tmp_path / "own"
    bridge.mkdir()
    (bridge / "bridge.json").write_text('{"token":"runner-secret"}', encoding="utf-8")
    seen: list[tuple[str, dict[str, object]]] = []

    def policy(session_id: str, body: dict[str, object]) -> dict[str, object]:
        seen.append((session_id, body))
        return {"bound_session_id": session_id, "decision": "deny"}

    ipc = start_native_hook_ipc(
        bridge_dir=bridge,
        bridge_id="bridge-own",
        session_id="session-own",
        handlers={
            "/policy-evaluate": policy,
            "/active-session": lambda sid, _body: {"session_id": sid},
        },
    )
    try:
        advert = json.loads((bridge / "hook-ipc" / "runner-ipc.json").read_text(encoding="utf-8"))
        assert advert["socket_path"] == str(ipc.socket_path)
        assert advert["bridge_id"] == "bridge-own"
        assert isinstance(advert["ipc_capability"], str)
        assert ipc.socket_dir.stat().st_mode & 0o777 == 0o700
        assert "runner-secret" not in json.dumps(advert)
        assert get_native_hook_ipc(bridge) == {"session_id": "session-own"}
        assert post_native_hook_ipc(
            bridge,
            "/policy-evaluate",
            {"session_id": "forged-peer"},
        ) == {"bound_session_id": "session-own", "decision": "deny"}
        assert seen == [("session-own", {"session_id": "forged-peer"})]
        assert post_native_hook_ipc(tmp_path / "peer", "/policy-evaluate", {}) is None
        with pytest.raises(ValueError, match="unknown"):
            post_native_hook_ipc(bridge, "/raw-proxy", {})
    finally:
        ipc.close()
    assert not ipc.socket_path.exists()
    assert not (bridge / "hook-ipc" / "runner-ipc.json").exists()


def test_native_hook_ipc_rejects_unknown_callback_and_public_symlink(tmp_path: Path) -> None:
    bridge = tmp_path / "own"
    bridge.mkdir()
    with pytest.raises(ValueError, match="unknown"):
        start_native_hook_ipc(
            bridge_dir=bridge,
            bridge_id="bridge-own",
            session_id="session-own",
            handlers={"/raw-proxy": lambda _sid, _body: {}},
        )
    peer = tmp_path / "peer"
    peer.mkdir()
    (bridge / "hook-ipc").symlink_to(peer, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        start_native_hook_ipc(
            bridge_dir=bridge,
            bridge_id="bridge-own",
            session_id="session-own",
            handlers={},
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt enforcement probe")
def test_native_hook_ipc_seatbelt_can_reach_only_own_socket(tmp_path: Path) -> None:
    """The IPC grants expose the own socket but no peer socket or bearer file."""
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.inner.seatbelt_sandbox import SeatbeltSandboxBackend, _build_profile

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    own_bridge = tmp_path / "own"
    peer_bridge = tmp_path / "peer"
    own_bridge.mkdir()
    peer_bridge.mkdir()
    bearer = own_bridge / "bridge.json"
    bearer.write_text('{"token":"runner-secret"}', encoding="utf-8")
    own = start_native_hook_ipc(
        bridge_dir=own_bridge,
        bridge_id="own",
        session_id="own-session",
        handlers={"/policy-evaluate": lambda sid, _body: {"session_id": sid}},
    )
    peer = start_native_hook_ipc(
        bridge_dir=peer_bridge,
        bridge_id="peer",
        session_id="peer-session",
        handlers={"/policy-evaluate": lambda sid, _body: {"session_id": sid}},
    )
    try:
        sandbox = OSEnvSandboxSpec(
            type="darwin_seatbelt",
            read_paths=[str(own_bridge / "hook-ipc"), str(own.socket_dir)],
        )
        policy = SeatbeltSandboxBackend().resolve(
            OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=sandbox), workspace
        )
        profile = _build_profile(policy, workspace, argv=["/usr/bin/curl"])

        own_capability = json.loads(
            (own_bridge / "hook-ipc" / "runner-ipc.json").read_text(encoding="utf-8")
        )["ipc_capability"]

        def probe(socket: Path) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    "/usr/bin/sandbox-exec",
                    "-p",
                    profile,
                    "/usr/bin/curl",
                    "--unix-socket",
                    str(socket),
                    "--silent",
                    "--show-error",
                    "--fail-with-body",
                    "--max-time",
                    "2",
                    "--request",
                    "POST",
                    "--header",
                    "Content-Type: application/json",
                    "--header",
                    f"Authorization: Bearer {own_capability}",
                    "--data",
                    "{}",
                    "http://omnigent/policy-evaluate",
                ],
                capture_output=True,
                check=False,
            )

        own_result = probe(own.socket_path)
        assert own_result.returncode == 0, own_result.stderr.decode()
        assert json.loads(own_result.stdout) == {"session_id": "own-session"}
        assert probe(peer.socket_path).returncode != 0
        protected = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/cat", str(bearer)],
            capture_output=True,
            check=False,
        )
        assert protected.returncode != 0
        peer_advert = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                "/bin/cat",
                str(peer_bridge / "hook-ipc" / "runner-ipc.json"),
            ],
            capture_output=True,
            check=False,
        )
        assert peer_advert.returncode != 0
    finally:
        own.close()
        peer.close()
