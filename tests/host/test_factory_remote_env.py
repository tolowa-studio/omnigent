"""factory-gate-a-real opt-in env survives remote CLI → daemon → runner hops."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from omnigent.cli import _build_host_daemon_env
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_ARTIFACTS_ROOT_ENV,
    REAL_TASK_CHAT_ENV,
    REAL_TASK_SPEC_DIR_ENV,
)
from omnigent.host.connect import _build_runner_env

_REMOTE_SERVER = "https://example.databricksapps.com"
_FACTORY_VARS = (
    REAL_TASK_ENV,
    REAL_TASK_CHAT_ENV,
    REAL_TASK_SPEC_DIR_ENV,
    REAL_TASK_ARTIFACTS_ROOT_ENV,
)


def test_factory_gate_a_real_opt_in_survives_remote_daemon_and_runner_hops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pinned factory-gate-a-real selectors reach the runner; secrets do not."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_DIR_ENV, str(tmp_path / "specs"))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ROOT_ENV, str(tmp_path / "artifacts"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-forward-remotely")
    monkeypatch.setenv("OMNIGENT_FACTORY_GATE_A_REAL_UNRELATED_SECRET", "also-stripped")
    monkeypatch.delenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)

    daemon_env = _build_host_daemon_env(server_url=_REMOTE_SERVER)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=_REMOTE_SERVER,
        runner_id="runner_factory_remote",
        binding_token="tok",
        workspace=str(tmp_path),
        parent_pid=42,
    )

    expected = {
        REAL_TASK_ENV: "1",
        REAL_TASK_CHAT_ENV: "1",
        REAL_TASK_SPEC_DIR_ENV: str(tmp_path / "specs"),
        REAL_TASK_ARTIFACTS_ROOT_ENV: str(tmp_path / "artifacts"),
    }
    for env in (daemon_env, runner_env):
        for name, value in expected.items():
            assert env[name] == value
        assert "ANTHROPIC_API_KEY" not in env
        assert "OMNIGENT_FACTORY_GATE_A_REAL_UNRELATED_SECRET" not in env


def test_factory_gate_a_real_opt_in_absent_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unpinned factory-gate-a-real selectors are not injected on the remote path."""
    monkeypatch.setenv("PATH", "/usr/bin")
    for name in _FACTORY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)

    daemon_env = _build_host_daemon_env(server_url=_REMOTE_SERVER)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=_REMOTE_SERVER,
        runner_id="runner_factory_remote_absent",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    for env in (daemon_env, runner_env):
        for name in _FACTORY_VARS:
            assert name not in env
