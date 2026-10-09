"""Legacy ``run_trial.py`` wires the canonical Gate A Cursor CLI Seatbelt sandbox."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_trial.run_trial import main


def _minimal_profile(ws: Path, config_dir: Path) -> dict[str, object]:
    wmc = ws / ".cursor" / "mcp.json"
    return {
        "cursor_cli_executable": sys.executable,
        "cursor_cli_version": "test",
        "workspace_mcp_config": str(wmc),
        "allowed_mcp_tools": [f"{MCP_SERVER_NAME}/{TOOL_NAME}"],
        "effective_config_hashes": {},
        "credential_store": "memory",
    }


class _FakePrestarted:
    def witness_proof(self) -> dict[str, object]:
        return {"fake": True}

    def cleanup(self) -> None:
        return None


def _discovery_ok() -> dict[str, object]:
    return {
        "gate_passed": True,
        "configured_servers": [MCP_SERVER_NAME],
        "configured_tools": [TOOL_NAME],
        "effective_config_hashes_post_enable": {},
        "isolated_home_initial_proof": {"cursor_state_absent": True},
    }


def test_run_trial_passes_sandbox_to_discovery_and_zero_server_probe(tmp_path: Path) -> None:
    config_dir = tmp_path / "cursor-config"
    config_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    sandbox = MagicMock(name="gate_a_sandbox")
    sandbox_kw: dict[str, object] = {}
    zero_kw: dict[str, object] = {}
    prepare_homes: list[Path] = []

    def _prepare(home_dir: Path) -> MagicMock:
        prepare_homes.append(home_dir)
        return sandbox

    def _discover(*_args: object, **kwargs: object) -> dict[str, object]:
        sandbox_kw.update(kwargs)
        return _discovery_ok()

    def _zero(*_args: object, **kwargs: object) -> dict[str, object]:
        zero_kw.update(kwargs)
        return {"gate_passed": True, "configured_servers": []}

    with (
        patch(
            "dev.factory.gate_a_trial.run_trial.resolve_trial_cursor_executable",
            return_value=sys.executable,
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch("dev.factory.gate_a_trial.run_trial.dispose_trial_workspace"),
        patch(
            "dev.factory.gate_a_trial.run_trial.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.prove_stdio_mcp_child_env_clean",
            return_value={"ok": True},
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.start_prestarted_gate_a_mcp",
            return_value=_FakePrestarted(),
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.materialize_cursor_config_dir",
            return_value=_minimal_profile(workspace, config_dir),
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.prepare_gate_a_cursor_cli_sandbox",
            side_effect=_prepare,
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.discover_and_gate_gate_a_mcp",
            side_effect=_discover,
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.discover_and_assert_zero_mcp_servers",
            side_effect=_zero,
        ),
        patch("dev.factory.gate_a_trial.run_trial.GateATrialTranscript.write", return_value=tmp_path / "t.json"),
    ):
        assert (
            main(
                [
                    "--cursor-config-dir",
                    str(config_dir),
                    "--inspect-cli",
                    "--probe-workspace-mcp-removal",
                ],
            )
            == 0
        )
    assert prepare_homes == [home]
    assert sandbox_kw.get("gate_a_sandbox") is sandbox
    assert zero_kw.get("gate_a_sandbox") is sandbox
    sandbox.cleanup.assert_called_once()


def test_run_trial_cleans_up_sandbox_when_discovery_gate_fails(tmp_path: Path) -> None:
    from dev.factory.gate_a_trial.cursor_cli_sandbox import GateACursorCliSandbox

    config_dir = tmp_path / "cursor-config"
    config_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    profile_path = home / "gate-a-trial.sb"
    profile_path.write_text("(version 1)\n(allow default)\n", encoding="utf-8")
    sandbox = GateACursorCliSandbox(
        profile_path=profile_path,
        profile_sha256="0" * 64,
        home_dir=home,
    )

    with (
        patch(
            "dev.factory.gate_a_trial.run_trial.resolve_trial_cursor_executable",
            return_value=sys.executable,
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch("dev.factory.gate_a_trial.run_trial.dispose_trial_workspace"),
        patch(
            "dev.factory.gate_a_trial.run_trial.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.prove_stdio_mcp_child_env_clean",
            return_value={"ok": True},
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.start_prestarted_gate_a_mcp",
            return_value=_FakePrestarted(),
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.materialize_cursor_config_dir",
            return_value=_minimal_profile(workspace, config_dir),
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.prepare_gate_a_cursor_cli_sandbox",
            return_value=sandbox,
        ),
        patch(
            "dev.factory.gate_a_trial.run_trial.discover_and_gate_gate_a_mcp",
            return_value={
                "gate_passed": False,
                "gate_failure_reasons": ["synthetic discovery failure"],
            },
        ),
        patch("dev.factory.gate_a_trial.run_trial.GateATrialTranscript.write", return_value=tmp_path / "t.json"),
    ):
        with pytest.raises(SystemExit):
            main(["--cursor-config-dir", str(config_dir), "--inspect-cli"])

    assert not profile_path.exists()
