"""Focused tests for the Gate A MCP bridge (synthetic fixture; no 90s settle)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.checkout import create_disposable_worktree
from dev.factory.gate_a_mcp.bridge import (
    GateAMcpArgumentError,
    GateAMcpBridgeConfig,
    GateAMcpBridgeError,
    run_bound_internal_stage_order,
    validate_tool_arguments,
)
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME, MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_mcp.stdio_launch import gate_a_stdio_mcp_launch
from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID, STAGE_WORKER_ENV
from dev.factory.order_scoped.probe_trust import fingerprint_probe_program
from dev.factory.order_scoped.receipt_admission import GATE_A_EXACT_PROBE_NAMES
from dev.factory.seatbelt_fixture.manifest import (
    GATE_A_MIN_SETTLE_SECONDS,
    FixtureReceipt,
    ProbeRecord,
    child_script_path,
)
from tests.dev.factory.gate_admission_test_support import bind_trusted_gate_receipt_for_admission


def _assert_zero_arg_tool_parameters(parameters: dict) -> None:
    """No-parameter MCP inputSchema (must not use a ``kwargs`` wrapper)."""
    assert parameters.get("type") == "object"
    assert parameters.get("properties") == {}
    assert list(parameters.get("required") or []) == []
    assert "kwargs" not in parameters.get("properties", {})


def _qualified_fixture_receipt() -> FixtureReceipt:
    probes = [ProbeRecord(name, True, "ok") for name in sorted(GATE_A_EXACT_PROBE_NAMES)]
    receipt = FixtureReceipt(
        passed=True,
        qualified_for_gate_a=True,
        failure_reason=None,
        order_id=INTERNAL_STAGE_ORDER_ID,
        manifest_hash="deadbeef",
        command_hash="cafebabe",
        platform="darwin",
        machine="arm64",
        sandbox_backend="darwin_seatbelt",
        probes=probes,
        started_at="2020-01-01T00:00:00+00:00",
        ended_at="2020-01-01T00:02:00+00:00",
        settle_seconds=GATE_A_MIN_SETTLE_SECONDS,
        settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
    )
    return bind_trusted_gate_receipt_for_admission(receipt)


def test_validate_tool_arguments_rejects_forbidden_keys() -> None:
    with pytest.raises(GateAMcpArgumentError, match="forbidden"):
        validate_tool_arguments({"command": "echo hi"})
    with pytest.raises(GateAMcpArgumentError, match="forbidden"):
        validate_tool_arguments({"worktree": "/tmp/evil"})
    with pytest.raises(GateAMcpArgumentError, match="forbidden"):
        validate_tool_arguments({"receipt": {}})


def test_validate_tool_arguments_allows_empty() -> None:
    validate_tool_arguments({})
    validate_tool_arguments(None)


def test_server_tool_malformed_arguments_fail_closed_json() -> None:
    def _fixture_must_not_run(**_kwargs: object) -> FixtureReceipt:
        pytest.fail("seatbelt fixture must not run for rejected arguments")

    with pytest.raises(GateAMcpArgumentError):
        run_bound_internal_stage_order(
            {"command": "id"},
            config=GateAMcpBridgeConfig(python_executable=Path(sys.executable)),
            fixture_runner=_fixture_must_not_run,
        )


def test_bridge_rejects_unqualified_fixture(tmp_path: Path) -> None:
    bad = FixtureReceipt(
        passed=True,
        qualified_for_gate_a=False,
        failure_reason=None,
        order_id=INTERNAL_STAGE_ORDER_ID,
        manifest_hash="",
        command_hash="",
        platform=sys.platform,
        machine="test",
        sandbox_backend="darwin_seatbelt",
        probes=[],
        settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
    )

    def _fixture(**_kwargs: object) -> FixtureReceipt:
        return bad

    with pytest.raises(GateAMcpBridgeError, match="qualified"):
        run_bound_internal_stage_order(
            {},
            config=GateAMcpBridgeConfig(python_executable=Path(sys.executable)),
            fixture_runner=_fixture,
            worktree_factory=lambda: tmp_path / "co",
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_bridge_positive_with_synthetic_qualified_receipt(tmp_path: Path) -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.fail("sandbox-exec missing")

    def _fixture(**_kwargs: object) -> FixtureReceipt:
        return _qualified_fixture_receipt()

    def _worktree() -> Path:
        return create_disposable_worktree(parent=tmp_path)

    result = run_bound_internal_stage_order(
        {},
        config=GateAMcpBridgeConfig(
            python_executable=Path(sys.executable),
            worker_environ={STAGE_WORKER_ENV: "1", "PATH": os.environ.get("PATH", "")},
        ),
        fixture_runner=_fixture,
        worktree_factory=_worktree,
    )
    assert result["ok"] is True
    assert result["artifact"] == BOUND_ARTIFACT_FILENAME
    assert result["probe_sha256"] == fingerprint_probe_program(child_script_path())
    assert Path(result["artifact_path"]).is_file()
    assert len(result["artifact_sha256"]) == 64


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
@pytest.mark.asyncio
async def test_stdio_mcp_tool_round_trip_real_fixture() -> None:
    """MCP subprocess runs Seatbelt preflight at startup (~90s settle) then CallTool."""
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.spec.types import AgentSpec, MCPServerConfig

    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec required for worker execute")

    command, args, env = gate_a_stdio_mcp_launch()
    mcp = MCPServerConfig(
        name=MCP_SERVER_NAME,
        transport="stdio",
        command=command,
        args=args,
        env=env,
    )
    spec = AgentSpec(spec_version=1, mcp_servers=[mcp])
    tool_full_name = f"{MCP_SERVER_NAME}__{TOOL_NAME}"
    manager = RunnerMcpManager()
    try:
        result = await manager.schemas_for(spec)
        assert tool_full_name in result.tool_names
        tool_schema = next(s for s in result.schemas if s["name"] == tool_full_name)
        _assert_zero_arg_tool_parameters(tool_schema["parameters"])
        raw = await manager.call_tool(spec, tool_full_name, {})
        assert raw.startswith("{"), repr(raw[:1200])
        payload = json.loads(raw)
        assert payload.get("ok") is True
        assert payload.get("artifact") == BOUND_ARTIFACT_FILENAME
        assert Path(payload["artifact_path"]).is_file()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stdio_mcp_malformed_arguments_fail_closed() -> None:
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.spec.types import AgentSpec, MCPServerConfig

    extra_args = {"command": "id"}

    command, args, env = gate_a_stdio_mcp_launch(include_worker_env=False)
    env = {
        **env,
        "GATE_A_MCP_FIXTURE_SENTINEL": "must-not-run",
        "GATE_A_MCP_SKIP_STARTUP_PREFLIGHT": "1",
    }
    mcp = MCPServerConfig(
        name=MCP_SERVER_NAME,
        transport="stdio",
        command=command,
        args=args,
        env=env,
    )
    spec = AgentSpec(spec_version=1, mcp_servers=[mcp])
    tool_full_name = f"{MCP_SERVER_NAME}__{TOOL_NAME}"
    manager = RunnerMcpManager()
    try:
        schemas = await manager.schemas_for(spec)
        tool_schema = next(s for s in schemas.schemas if s["name"] == tool_full_name)
        _assert_zero_arg_tool_parameters(tool_schema["parameters"])

        started = time.monotonic()
        raw = await asyncio.wait_for(
            manager.call_tool(spec, tool_full_name, extra_args),
            timeout=10.0,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 30.0, "malformed MCP args must not run the Seatbelt fixture"

        assert raw.startswith("Error:")
        assert "Additional properties" in raw
        assert "command" in raw

        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

        assert "artifact_path" not in raw
        assert "artifact_sha256" not in raw
        assert '"ok": true' not in raw.replace(" ", "").casefold()
    finally:
        await manager.shutdown()
