"""Motion Core order start bridge for factory-gate-a-real chat harness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from omnigent.factory.gate_a.motion_order_start import (
    _START_SUBPROCESS_TIMEOUT_S,
    _STATUS_SUBPROCESS_TIMEOUT_S,
    MOTION_ORDER_APPROVALS_DIR_ENV,
    MOTION_ORDER_START_ENABLE_ENV,
    _cancellation_active,
    _default_subprocess_runner,
    _subprocess_timeout_for_argv,
    format_motion_order_start_safe_summary,
    motion_order_start_enabled,
    start_motion_order,
)
from omnigent.factory.gate_a.motion_order_status import (
    MOTION_CORE_ROOT_ENV,
    MOTION_ORDERS_ROOT_ENV,
)
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_CHAT_ENV,
    OperatorCommand,
    parse_operator_command,
)
from omnigent.inner.datamodel import Message
from omnigent.inner.executor import ExecutorError, TextChunk
from omnigent.inner.factory_gate_a_real_harness import FactoryGateARealExecutor

_ORDER_ID = "ord-start-abc"
_BRIEF_HASH = "a" * 64
_BASE_SHA = "b" * 40
_OTHER_HASH = "c" * 64
_SECRET_APPROVAL = "secret-approver-name-do-not-leak"


def _install_fake_cli(core_root: Path) -> Path:
    bin_dir = core_root / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "motion-order.mjs"
    cli.write_text("// placeholder\n", encoding="utf-8")
    return cli


def _fake_node_dir(tmp_path: Path) -> Path:
    node_dir = tmp_path / "node-bin"
    node_dir.mkdir()
    node = node_dir / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    return node_dir


def _approval(
    *,
    order_id: str = _ORDER_ID,
    brief_hash: str = _BRIEF_HASH,
    base_sha: str = _BASE_SHA,
    approved_by: str = _SECRET_APPROVAL,
) -> dict[str, object]:
    return {
        "schema_id": "omnigent.factory.motion-order-start-approval.v1",
        "approved": True,
        "order_id": order_id,
        "brief_hash": brief_hash,
        "base_sha": base_sha,
        "approved_by": approved_by,
        "approval_ref": "local-operator-approval-001",
    }


def _write_approval(approvals_dir: Path, order_id: str, data: dict[str, object]) -> Path:
    approvals_dir.mkdir(parents=True, exist_ok=True)
    path = approvals_dir / f"{order_id}.json"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return path


def _precheck_payload(
    *,
    order_id: str = _ORDER_ID,
    state: str = "new",
    brief_hash: str = _BRIEF_HASH,
    base_sha: str = _BASE_SHA,
    cancellation_phase: str | None = "active",
    cancel_request: object | None = None,
    cancellation_marker: object | None = None,
    structured_brief_hash: str | None = None,
) -> dict[str, object]:
    submit_brief = structured_brief_hash if structured_brief_hash is not None else brief_hash
    payload: dict[str, object] = {
        "ok": True,
        "order": {
            "order_id": order_id,
            "state": state,
            "brief_hash": brief_hash,
            "structured_submit": {
                "schema_id": "motion.order.structured-submit.v2",
                "brief_hash": submit_brief,
                "policy": {"base_sha": base_sha},
            },
        },
        "cancel_request": cancel_request,
    }
    if cancellation_phase is not None:
        payload["cancellation_phase"] = cancellation_phase
    if cancellation_marker is not None:
        payload["cancellation_marker"] = cancellation_marker
    return payload


def _start_ok_payload(order_id: str = _ORDER_ID) -> dict[str, object]:
    return {
        "ok": True,
        "order_id": order_id,
        "state": "started",
        "direct_path": {
            "mocked": False,
            "detached": {"ok": True, "mechanism": "launchd"},
        },
        "secret": "must not leak",
    }


def _start_env(
    *,
    core_root: Path,
    orders_root: Path,
    approvals_dir: Path,
    path: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {
        MOTION_ORDER_START_ENABLE_ENV: "1",
        MOTION_ORDER_APPROVALS_DIR_ENV: str(approvals_dir),
        MOTION_CORE_ROOT_ENV: str(core_root),
        MOTION_ORDERS_ROOT_ENV: str(orders_root),
        "PATH": path,
        "HOME": "/tmp/omnigent-home",
        "TMPDIR": "/tmp",
    }
    if extra:
        env.update(extra)
    return env


def _dual_runner(
    *,
    status_payload: dict[str, object],
    start_payload: dict[str, object] | None = None,
    start_rc: int = 0,
) -> tuple[list[list[str]], list[dict[str, str]], list[str]]:
    captured_argv: list[list[str]] = []
    captured_env: list[dict[str, str]] = []
    phases: list[str] = []

    def _runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        captured_argv.append(list(argv))
        captured_env.append(dict(env))
        cmd = argv[2] if len(argv) > 2 else ""
        phases.append(cmd)
        if cmd == "status":
            return subprocess.CompletedProcess(argv, 0, json.dumps(status_payload) + "\n", "")
        if cmd == "start":
            body = json.dumps(start_payload or _start_ok_payload())
            return subprocess.CompletedProcess(argv, start_rc, body + "\n", "")
        raise AssertionError(f"unexpected argv command: {cmd}")

    return captured_argv, captured_env, phases, _runner


def _bind_real_chat_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")


async def _collect_events(executor: FactoryGateARealExecutor, text: str) -> list[object]:
    events: list[object] = []
    async for event in executor.run_turn([Message(role="user", content=text)], [], ""):
        events.append(event)
    return events


def test_parse_operator_command_order_start_exact() -> None:
    assert parse_operator_command(f"order start {_ORDER_ID}") == OperatorCommand(
        kind="order_start", task_id=_ORDER_ID
    )
    assert parse_operator_command("order start") is None
    assert parse_operator_command(f"order start {_ORDER_ID} extra") is None
    assert parse_operator_command(f"ORDER start {_ORDER_ID}") is None


def test_motion_order_start_disabled_by_default() -> None:
    assert not motion_order_start_enabled({})


def test_start_rejects_missing_approval(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    _, _, _, runner = _dual_runner(status_payload=_precheck_payload())
    with pytest.raises(ValueError, match="approval is not available"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )


def test_start_rejects_malformed_approval(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _install_fake_cli(core_root)
    bad = _approval()
    bad["approved"] = False
    _write_approval(approvals, _ORDER_ID, bad)
    node_dir = _fake_node_dir(tmp_path)
    _, _, _, runner = _dual_runner(status_payload=_precheck_payload())
    with pytest.raises(ValueError, match="approval is invalid") as exc:
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )
    assert _SECRET_APPROVAL not in str(exc.value)


def test_start_rejects_symlink_approval(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    real = approvals / "real.json"
    _write_approval(approvals, "real", _approval(order_id=_ORDER_ID))
    link = approvals / f"{_ORDER_ID}.json"
    link.symlink_to(real)
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=lambda a, e: subprocess.CompletedProcess(a, 0, "{}", ""),
        )


def test_start_precheck_hash_mismatch_skips_start(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    captured_argv, _, phases, runner = _dual_runner(
        status_payload=_precheck_payload(brief_hash=_OTHER_HASH),
    )
    with pytest.raises(ValueError, match="brief_hash mismatch"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )
    assert phases == ["status"]
    assert not any(a[2] == "start" for a in captured_argv)


def test_start_precheck_base_sha_mismatch(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    _, _, phases, runner = _dual_runner(
        status_payload=_precheck_payload(base_sha="d" * 40),
    )
    with pytest.raises(ValueError, match="base_sha mismatch"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )
    assert phases == ["status"]


def test_start_precheck_rejects_non_new_state(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    _, _, phases, runner = _dual_runner(status_payload=_precheck_payload(state="running"))
    with pytest.raises(ValueError, match="not in new state"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )
    assert phases == ["status"]


def test_start_precheck_rejects_cancellation(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    _, _, phases, runner = _dual_runner(
        status_payload=_precheck_payload(
            cancellation_phase="requested",
            cancel_request={"id": "x"},
        ),
    )
    with pytest.raises(ValueError, match="cancellation is active"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )
    assert phases == ["status"]


@pytest.mark.parametrize(
    "payload",
    [
        {"cancel_request": None},
        {"cancellation_phase": "unknown", "cancel_request": None},
        {"cancellation_phase": "active", "cancel_request": None, "cancellation_marker": False},
    ],
)
def test_start_precheck_rejects_unknown_cancel_state(payload: dict[str, object]) -> None:
    assert _cancellation_active(payload)


def test_start_argv_and_forced_env(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    captured_argv, captured_env, phases, runner = _dual_runner(
        status_payload=_precheck_payload(),
        start_payload=_start_ok_payload(),
    )
    summary = start_motion_order(
        _ORDER_ID,
        _start_env(
            core_root=core_root,
            orders_root=orders_root,
            approvals_dir=approvals,
            path=str(node_dir),
            extra={
                "MOTION_ORDER_AUTO_MERGE": "1",
                "MOTION_ORDER_FOO": "bar",
                "MOTION_RUN_RECORD_DIR": "/tmp/leak",
                "CLOUDSDK_CONFIG": "/tmp/gcloud",
                "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/adc.json",
            },
        ),
        subprocess_runner=runner,
    )
    assert "order_start_ok: true" in summary
    assert "launch_submitted: true" in summary
    assert phases == ["status", "start"]
    start_argv = captured_argv[1]
    assert start_argv[2] == "start"
    assert start_argv[3] == _ORDER_ID
    assert start_argv[4:6] == ["--execute-direct-path", "--orders-root"]
    assert start_argv[7] == "--json"
    env = captured_env[1]
    assert env["MOTION_ORDER_AUTO_MERGE"] == "0"
    assert env["MOTION_ORDER_DUPLICATE_CHECK"] == "enforce"
    assert env["MOTION_ORDER_DETACH"] == "launchd"
    assert env["MOTION_ORDER_CLIENT_SECRET_SCOPING"] == "1"
    assert "MOTION_ORDER_FOO" not in env
    assert "MOTION_RUN_RECORD_DIR" not in env
    assert env["CLOUDSDK_CONFIG"] == "/tmp/gcloud"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert env["HOME"] == "/tmp/omnigent-home"


def test_start_nonzero_exit_truthful(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    _, _, _, runner = _dual_runner(
        status_payload=_precheck_payload(),
        start_payload=_start_ok_payload(),
        start_rc=2,
    )
    with pytest.raises(ValueError, match="did not complete successfully"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )


def test_subprocess_timeout_bounds_differ_by_command() -> None:
    status_argv = ["node", "/core/bin/motion-order.mjs", "status", "ord"]
    start_argv = ["node", "/core/bin/motion-order.mjs", "start", "ord"]
    assert _subprocess_timeout_for_argv(status_argv) == _STATUS_SUBPROCESS_TIMEOUT_S
    assert _subprocess_timeout_for_argv(start_argv) == _START_SUBPROCESS_TIMEOUT_S
    assert _STATUS_SUBPROCESS_TIMEOUT_S < _START_SUBPROCESS_TIMEOUT_S


def test_default_runner_passes_distinct_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def _fake_run(
        *_args: object, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(timeout)
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _default_subprocess_runner(["node", "cli", "status", "x"], {"PATH": "/bin"})
    _default_subprocess_runner(["node", "cli", "start", "x"], {"PATH": "/bin"})
    assert seen == [_STATUS_SUBPROCESS_TIMEOUT_S, _START_SUBPROCESS_TIMEOUT_S]


def test_start_precheck_status_timeout_message(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    def _runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        if argv[2] == "status":
            raise subprocess.TimeoutExpired(argv, _STATUS_SUBPROCESS_TIMEOUT_S)
        raise AssertionError("start must not run")

    with pytest.raises(ValueError, match="status timed out"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=_runner,
        )


_CORE_STRUCT_V2_ORDER_ID = "struct-8fefba478c8c4dacf0518de7cb94c6f8"
_CORE_STRUCT_V2_BRIEF_HASH = "325057d7a01aae1c5210f5689701912c0a5c5f0c64305efc0174a63abc71a9de"
_CORE_STRUCT_V2_BASE_SHA = "ad149aec2349c8d8680ff38a8500b0d85364acd4"


def _core_struct_v2_status_precheck_payload() -> dict[str, object]:
    """Status JSON shape from a healthy Core `new` structured v2 order (smoke fixture)."""
    return {
        "ok": True,
        "order": {
            "order_id": _CORE_STRUCT_V2_ORDER_ID,
            "state": "new",
            "brief_hash": _CORE_STRUCT_V2_BRIEF_HASH,
            "structured_submit": {
                "schema_id": "motion.order.structured-submit.v2",
                "client_id": "internal",
                "idempotency_key": "factory-omnigent-policy-v2-smoke-20261009",
                "brief_hash": _CORE_STRUCT_V2_BRIEF_HASH,
                "policy": {
                    "gates": ["node --test test/motion-order-structured-submit.test.mjs"],
                    "base_sha": _CORE_STRUCT_V2_BASE_SHA,
                    "human_gates": ["Human approval required before any start or merge"],
                    "review": "Independent Grok 4.7 High review after Composer 2.5 build",
                    "non_goals": ["No live order start", "No deployment"],
                },
            },
        },
        "cancellation_phase": "active",
        "cancel_request": None,
    }


def test_start_precheck_accepts_core_active_cancellation_phase(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(
        approvals,
        _CORE_STRUCT_V2_ORDER_ID,
        _approval(
            order_id=_CORE_STRUCT_V2_ORDER_ID,
            brief_hash=_CORE_STRUCT_V2_BRIEF_HASH,
            base_sha=_CORE_STRUCT_V2_BASE_SHA,
        ),
    )
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    captured_argv, _, phases, runner = _dual_runner(
        status_payload=_core_struct_v2_status_precheck_payload(),
        start_payload=_start_ok_payload(order_id=_CORE_STRUCT_V2_ORDER_ID),
    )
    summary = start_motion_order(
        _CORE_STRUCT_V2_ORDER_ID,
        _start_env(
            core_root=core_root,
            orders_root=orders_root,
            approvals_dir=approvals,
            path=str(node_dir),
        ),
        subprocess_runner=runner,
    )
    assert "order_start_ok: true" in summary
    assert phases == ["status", "start"]
    assert captured_argv[1][2] == "start"


def test_start_precheck_rejects_structured_brief_hash_mismatch(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    _, _, phases, runner = _dual_runner(
        status_payload=_precheck_payload(structured_brief_hash=_OTHER_HASH),
    )
    with pytest.raises(ValueError, match="structured brief_hash mismatch"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )
    assert phases == ["status"]


def test_start_rejects_non_launchd_detach_mechanism(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    spawn_payload = _start_ok_payload()
    spawn_payload["direct_path"] = {
        "mocked": False,
        "detached": {"ok": True, "mechanism": "spawn"},
    }
    _, _, _, runner = _dual_runner(status_payload=_precheck_payload(), start_payload=spawn_payload)
    with pytest.raises(ValueError, match="ambiguous outcome"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )


def test_start_timeout_truthful(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    calls = 0

    def _runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if argv[2] == "status":
            return subprocess.CompletedProcess(argv, 0, json.dumps(_precheck_payload()) + "\n", "")
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(ValueError, match="did not complete successfully"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=_runner,
        )
    assert calls == 2


def test_start_rejects_mocked_direct_path(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    mocked = _start_ok_payload()
    mocked["direct_path"] = {
        "mocked": True,
        "detached": {"ok": True, "mechanism": "launchd"},
    }
    _, _, _, runner = _dual_runner(status_payload=_precheck_payload(), start_payload=mocked)
    with pytest.raises(ValueError, match="ambiguous outcome"):
        start_motion_order(
            _ORDER_ID,
            _start_env(
                core_root=core_root,
                orders_root=orders_root,
                approvals_dir=approvals,
                path=str(node_dir),
            ),
            subprocess_runner=runner,
        )


def test_start_success_fixture_summary(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    approvals = tmp_path / "approvals"
    _write_approval(approvals, _ORDER_ID, _approval())
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    _, _, _, runner = _dual_runner(
        status_payload=_precheck_payload(),
        start_payload=_start_ok_payload(),
    )
    summary = start_motion_order(
        _ORDER_ID,
        _start_env(
            core_root=core_root,
            orders_root=orders_root,
            approvals_dir=approvals,
            path=str(node_dir),
        ),
        subprocess_runner=runner,
    )
    assert summary == format_motion_order_start_safe_summary(order_id=_ORDER_ID, state="started")
    assert "must not leak" not in summary


@pytest.mark.asyncio
async def test_executor_order_start_disabled_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_real_chat_minimal(monkeypatch)
    events = await _collect_events(FactoryGateARealExecutor(), f"order start {_ORDER_ID}")
    assert any(
        isinstance(e, ExecutorError) and MOTION_ORDER_START_ENABLE_ENV in e.message for e in events
    )


@pytest.mark.asyncio
async def test_executor_order_start_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_real_chat_minimal(monkeypatch)

    def _stub_start(order_id: str, **kwargs: object) -> str:
        return format_motion_order_start_safe_summary(order_id=order_id, state="started")

    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.start_motion_order",
        _stub_start,
    )
    events = await _collect_events(FactoryGateARealExecutor(), f"order start {_ORDER_ID}")
    assert any(isinstance(e, TextChunk) and "order_start_ok: true" in e.text for e in events)


@pytest.mark.asyncio
async def test_http_order_start_safe_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_real_chat_minimal(monkeypatch)

    def _stub_start(order_id: str, **kwargs: object) -> str:
        return format_motion_order_start_safe_summary(order_id=order_id, state="started")

    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.start_motion_order",
        _stub_start,
    )

    from omnigent.inner import factory_gate_a_real_harness

    app = factory_gate_a_real_harness.create_app()
    conversation_id = "conv_motion_order_start"
    app.state.conversation_id = conversation_id
    body = {
        "type": "message",
        "role": "user",
        "model": "factory-gate-a-real-beta",
        "content": [{"type": "input_text", "text": f"order start {_ORDER_ID}"}],
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness.test") as client:
        events: list[str] = []
        async with client.stream(
            "POST",
            f"/v1/sessions/{conversation_id}/events",
            json=body,
        ) as response:
            response.raise_for_status()
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, _, buffer = buffer.partition("\n\n")
                    if "response.output_text.delta" not in frame:
                        continue
                    for line in frame.splitlines():
                        if line.startswith("data:"):
                            data = json.loads(line[len("data:") :].strip())
                            events.append(data.get("delta", ""))

    text = "".join(events)
    assert "order_start_ok: true" in text
    assert _SECRET_APPROVAL not in text
