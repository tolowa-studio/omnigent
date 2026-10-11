"""Motion Core order status bridge for factory-gate-a-real chat harness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from dev.factory.gate_a_real.orchestration import RealTaskRunResult
from omnigent.factory.gate_a.motion_order_status import (
    MOTION_CORE_ROOT_ENV,
    MOTION_ORDERS_ROOT_ENV,
    format_motion_order_safe_summary,
    read_motion_order_status_summary,
    validate_motion_order_id,
)
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_ARTIFACTS_ENV,
    REAL_TASK_CHAT_ENV,
    REAL_TASK_SPEC_ENV,
    OperatorCommand,
    parse_operator_command,
)
from omnigent.inner.datamodel import Message
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.factory_gate_a_real_harness import FactoryGateARealExecutor

_ORDER_ID = "ord-abc-123"
# Deterministic shape from Motion Core makeOrderId("TOL-701") (slug + compact UTC + suffix).
_CORE_FACTORY_ORDER_ID = "tol-701-20260928T193739-9afc6322"
_SECRET_TOKEN = "super-secret-motion-token"
_RAW_RECEIPT = {"prompt": "do not leak", "token": _SECRET_TOKEN}


def _motion_pins(
    monkeypatch: pytest.MonkeyPatch,
    *,
    core_root: Path,
    orders_root: Path,
) -> None:
    monkeypatch.setenv(MOTION_CORE_ROOT_ENV, str(core_root))
    monkeypatch.setenv(MOTION_ORDERS_ROOT_ENV, str(orders_root))


def _fake_node_dir(tmp_path: Path) -> Path:
    node_dir = tmp_path / "node-bin"
    node_dir.mkdir()
    node = node_dir / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    return node_dir


def _fake_node_shim_dir(tmp_path: Path) -> tuple[Path, Path]:
    """PATH dir with symlinked node shim (mise-style) and resolved real executable."""
    real_dir = tmp_path / "real-node-bin"
    real_dir.mkdir()
    real_node = real_dir / "node"
    real_node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_node.chmod(0o755)
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    shim = shim_dir / "node"
    shim.symlink_to(real_node)
    return shim_dir, real_node.resolve()


def _install_fake_cli(core_root: Path) -> Path:
    bin_dir = core_root / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "motion-order.mjs"
    cli.write_text("// placeholder for pinned CLI path checks\n", encoding="utf-8")
    return cli


def _ok_payload(order_id: str = _ORDER_ID) -> dict[str, object]:
    return {
        "ok": True,
        "order": {"order_id": order_id, "state": "running"},
        "receipts": {"builder": {}, "review": {}},
        "report": {
            "ok": True,
            "order_id": order_id,
            "sections": {
                "run_rows": {"status": "ok"},
            },
        },
        "cancellation_phase": "none",
        "cancel_request": None,
        "leak_probe": "must not appear in chat",
    }


def _bind_real_chat_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")


async def _collect_events(executor: FactoryGateARealExecutor, text: str) -> list[object]:
    events: list[object] = []
    async for event in executor.run_turn([Message(role="user", content=text)], [], ""):
        events.append(event)
    return events


def test_parse_operator_command_order_status_exact() -> None:
    assert parse_operator_command(f"order status {_ORDER_ID}") == OperatorCommand(
        kind="order_status", task_id=_ORDER_ID
    )
    assert parse_operator_command("order status") is None
    assert parse_operator_command(f"order status {_ORDER_ID} extra") is None
    assert parse_operator_command(f"ORDER status {_ORDER_ID}") is None
    assert parse_operator_command(f"status {_ORDER_ID}") == OperatorCommand(
        kind="status", task_id=_ORDER_ID
    )


def test_validate_motion_order_id_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="order_id"):
        validate_motion_order_id("../evil")
    with pytest.raises(ValueError, match="order_id"):
        validate_motion_order_id("has space")
    with pytest.raises(ValueError, match="order_id"):
        validate_motion_order_id("UPPER")


def test_validate_motion_order_id_accepts_motion_core_factory_shape() -> None:
    validate_motion_order_id(_CORE_FACTORY_ORDER_ID)


def test_validate_motion_order_id_rejects_arbitrary_uppercase_outside_factory_timestamp() -> None:
    with pytest.raises(ValueError, match="order_id"):
        validate_motion_order_id("TOL-701-20260928T193739-9afc6322")
    with pytest.raises(ValueError, match="order_id"):
        validate_motion_order_id("tol-701-20260928T193739-9AFC6322")


def test_read_motion_order_status_summary_success(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    captured_argv: list[str] = []

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        captured_argv[:] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(_ok_payload()) + "\n",
            "",
        )

    summary = read_motion_order_status_summary(
        _ORDER_ID,
        {
            MOTION_CORE_ROOT_ENV: str(core_root),
            MOTION_ORDERS_ROOT_ENV: str(orders_root),
            "PATH": str(node_dir),
        },
        subprocess_runner=_runner,
    )
    assert "order_status_ok: true" in summary
    assert "report_ok: true" in summary
    assert "report_unavailable_count: 0" in summary
    assert "report_mismatch_count: 0" in summary
    assert f"order_id: {_ORDER_ID}" in summary
    assert "state: running" in summary
    assert "receipt_category_count: 2" in summary
    assert _SECRET_TOKEN not in summary
    assert "must not appear" not in summary
    assert captured_argv[2] == "status"
    assert captured_argv[3] == _ORDER_ID
    assert captured_argv[4:6] == ["--orders-root", str(orders_root.resolve())]
    assert captured_argv[6] == "--json"


def test_read_motion_order_status_missing_pins() -> None:
    with pytest.raises(ValueError, match=MOTION_CORE_ROOT_ENV):
        read_motion_order_status_summary(_ORDER_ID, {})


def test_read_motion_order_status_report_ok_false_with_unavailable_sections(
    tmp_path: Path,
) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    payload = _ok_payload()
    payload["order"] = {"order_id": _ORDER_ID, "state": "new"}
    payload["report"] = {
        "ok": False,
        "order_id": _ORDER_ID,
        "sections": {
            "run_rows": {"status": "unavailable"},
            "pull_requests": {"status": "unavailable"},
            "execution_identity": {"status": "unavailable"},
        },
    }

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    summary = read_motion_order_status_summary(
        _ORDER_ID,
        {
            MOTION_CORE_ROOT_ENV: str(core_root),
            MOTION_ORDERS_ROOT_ENV: str(orders_root),
            "PATH": str(node_dir),
        },
        subprocess_runner=_runner,
    )
    assert "order_status_ok: true" in summary
    assert "report_ok: false" in summary
    assert "report_unavailable_count: 3" in summary
    assert "report_mismatch_count: 0" in summary
    assert "state: new" in summary


_SUSPICIOUS_REPORTED_ID = "ord-evil-do-not-echo-in-error-msg"


def test_read_motion_order_status_mismatch_errors_never_echo_reported_ids(
    tmp_path: Path,
) -> None:
    """Untrusted payload order_id values must not appear in mismatch ValueError text."""
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    order_payload = _ok_payload()
    order_payload["order"] = {"order_id": _SUSPICIOUS_REPORTED_ID, "state": "running"}

    def _runner_order(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(order_payload), "")

    with pytest.raises(ValueError) as order_exc:
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner_order,
        )
    assert str(order_exc.value) == "Motion Core order_id does not match requested order_id"
    assert _SUSPICIOUS_REPORTED_ID not in str(order_exc.value)
    assert _ORDER_ID not in str(order_exc.value)

    report_payload = _ok_payload()
    report_payload["report"] = {
        "ok": True,
        "order_id": _SUSPICIOUS_REPORTED_ID,
        "sections": {},
    }

    def _runner_report(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(report_payload), "")

    with pytest.raises(ValueError) as report_exc:
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner_report,
        )
    assert str(report_exc.value) == "Motion Core report order_id does not match requested order_id"
    assert _SUSPICIOUS_REPORTED_ID not in str(report_exc.value)
    assert _ORDER_ID not in str(report_exc.value)


def test_read_motion_order_status_mismatched_report_order_id(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    payload = _ok_payload()
    payload["report"] = {
        "ok": True,
        "order_id": "other-id",
        "sections": {},
    }

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(
        ValueError,
        match="Motion Core report order_id does not match requested order_id",
    ):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_rejects_malformed_report_sections(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    payload = _ok_payload()
    payload["report"] = {
        "ok": True,
        "order_id": _ORDER_ID,
        "sections": {"run_rows": "bad"},
    }

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="section must be an object"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_mismatched_id(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(_ok_payload(order_id="other-id")) + "\n",
            "",
        )

    with pytest.raises(
        ValueError,
        match="Motion Core order_id does not match requested order_id",
    ):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_nonzero_exit(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, "", "failed")

    with pytest.raises(ValueError, match="exited with code"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_malformed_json(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "not-json", "")

    with pytest.raises(ValueError, match="not valid JSON"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_ok_false(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    payload = _ok_payload()
    payload["ok"] = False

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="ok must be true"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_timeout(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    def _runner(_argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="node", timeout=1)

    with pytest.raises(ValueError, match="timed out"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_executes_node_shim_not_resolved_target(
    tmp_path: Path,
) -> None:
    """Regression: mise dispatches by argv[0]; subprocess must use the PATH shim path."""
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    shim_dir, real_node = _fake_node_shim_dir(tmp_path)
    node_shim = (shim_dir / "node").absolute()
    captured_argv: list[str] = []

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        captured_argv[:] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(_ok_payload()) + "\n",
            "",
        )

    read_motion_order_status_summary(
        _ORDER_ID,
        {
            MOTION_CORE_ROOT_ENV: str(core_root),
            MOTION_ORDERS_ROOT_ENV: str(orders_root),
            "PATH": str(shim_dir),
        },
        subprocess_runner=_runner,
    )
    assert captured_argv[0] == str(node_shim)
    assert captured_argv[0] != str(real_node)


def test_read_motion_order_status_rejects_unsafe_state(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    payload = _ok_payload()
    payload["order"] = {"order_id": _ORDER_ID, "state": "run\nning"}

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="single-line"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_rejects_unsafe_cancellation_phase(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    payload = _ok_payload()
    payload["cancellation_phase"] = "none\x00hidden"

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="cancellation_phase"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_rejects_non_object_receipts(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)
    payload = _ok_payload()
    payload["receipts"] = ["builder"]

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="receipts must be an object"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=_runner,
        )


def test_read_motion_order_status_rejects_cli_symlink(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    cli = core_root / "bin" / "motion-order.mjs"
    real = core_root / "real-motion-order.mjs"
    real.write_text(cli.read_text(encoding="utf-8"), encoding="utf-8")
    cli.unlink()
    cli.symlink_to(real)

    node_dir = _fake_node_dir(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        read_motion_order_status_summary(
            _ORDER_ID,
            {
                MOTION_CORE_ROOT_ENV: str(core_root),
                MOTION_ORDERS_ROOT_ENV: str(orders_root),
                "PATH": str(node_dir),
            },
            subprocess_runner=lambda a, e: subprocess.CompletedProcess(a, 0, "{}", ""),
        )


@pytest.mark.asyncio
async def test_executor_order_status_does_not_resolve_task_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_real_chat_minimal(monkeypatch)
    monkeypatch.delenv(REAL_TASK_SPEC_ENV, raising=False)
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ENV, raising=False)
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    _motion_pins(monkeypatch, core_root=core_root, orders_root=orders_root)

    run_calls = 0

    def _forbid_run(*_a: object, **_k: object) -> RealTaskRunResult:
        nonlocal run_calls
        run_calls += 1
        raise AssertionError("run must not be invoked")

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _forbid_run)

    def _stub_summary(
        order_id: str, environ: dict[str, str] | None = None, **kwargs: object
    ) -> str:
        del environ, kwargs
        return format_motion_order_safe_summary(
            order_id=order_id,
            state="queued",
            cancellation_phase=None,
            receipt_category_count=0,
            report_ok=True,
            report_unavailable_count=0,
            report_mismatch_count=0,
        )

    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.read_motion_order_status_summary",
        _stub_summary,
    )

    events = await _collect_events(FactoryGateARealExecutor(), f"order status {_ORDER_ID}")
    assert run_calls == 0
    assert any(isinstance(e, TextChunk) and "order_status_ok: true" in e.text for e in events)
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_executor_order_status_invalid_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_real_chat_minimal(monkeypatch)
    events = await _collect_events(FactoryGateARealExecutor(), "order status ../evil")
    assert any(isinstance(e, ExecutorError) and "order_id" in e.message for e in events)


@pytest.mark.asyncio
async def test_executor_task_status_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timedelta, timezone

    from dev.factory.gate_a_real.profile import (
        materialize_real_task_cursor_config_dir,
        materialize_real_task_review_config_dir,
    )
    from dev.factory.gate_a_real.spec import canonical_spec_sha256
    from tests.inner.test_factory_gate_a_real_harness import _receipt_for_spec

    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = materialize_real_task_cursor_config_dir(tmp_path / "profile")
    review_profile = materialize_real_task_review_config_dir(tmp_path / "review-profile")
    spec_dict: dict[str, object] = {
        "task_id": "unit-task",
        "workspace": str(workspace.resolve()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "prompt": "do the thing",
        "deliverable_paths": ["out.txt"],
        "verify_command": ["test", "-f", "out.txt"],
        "config_hashes": profile["effective_config_hashes"],
        "review_config_hashes": review_profile["effective_config_hashes"],
    }
    spec_dict["spec_sha256"] = canonical_spec_sha256(spec_dict)
    spec_path = tmp_path / "task.spec.json"
    spec_path.write_text(json.dumps(spec_dict, indent=2) + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    receipt = _receipt_for_spec(spec_path)
    receipt.post_review_manifest_sha256 = receipt.deliverable_manifest_sha256
    receipt.write(artifacts / "receipt.json")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))
    _bind_real_chat_minimal(monkeypatch)

    motion_calls = 0

    def _forbid_motion(*_a: object, **_k: object) -> str:
        nonlocal motion_calls
        motion_calls += 1
        raise AssertionError("motion order status must not run for task status")

    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.read_motion_order_status_summary",
        _forbid_motion,
    )

    events = await _collect_events(FactoryGateARealExecutor(), "status unit-task")
    assert motion_calls == 0
    assert any(isinstance(e, TextChunk) and "receipt_ok:" in e.text for e in events)


@pytest.mark.asyncio
async def test_http_order_status_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_real_chat_minimal(monkeypatch)
    monkeypatch.delenv(REAL_TASK_SPEC_ENV, raising=False)
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ENV, raising=False)
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    _motion_pins(monkeypatch, core_root=core_root, orders_root=orders_root)
    node_dir = _fake_node_dir(tmp_path)
    monkeypatch.setenv("PATH", str(node_dir))

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        payload = _ok_payload()
        payload["receipts"] = {"only": _RAW_RECEIPT}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(
        "omnigent.factory.gate_a.motion_order_status._default_subprocess_runner",
        _runner,
    )

    from omnigent.inner import factory_gate_a_real_harness

    app = factory_gate_a_real_harness.create_app()
    conversation_id = "conv_motion_order_status"
    app.state.conversation_id = conversation_id
    body = {
        "type": "message",
        "role": "user",
        "model": "factory-gate-a-real-beta",
        "content": [{"type": "input_text", "text": f"order status {_ORDER_ID}"}],
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
    assert "order_status_ok: true" in text
    assert _SECRET_TOKEN not in text
    assert "must not appear" not in text


def test_orders_root_not_mutated_by_status_read(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    marker = orders_root / "marker.txt"
    marker.write_text("stable\n", encoding="utf-8")
    before = marker.read_text(encoding="utf-8")
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(_ok_payload()), "")

    read_motion_order_status_summary(
        _ORDER_ID,
        {
            MOTION_CORE_ROOT_ENV: str(core_root),
            MOTION_ORDERS_ROOT_ENV: str(orders_root),
            "PATH": str(node_dir),
        },
        subprocess_runner=_runner,
    )
    assert marker.read_text(encoding="utf-8") == before


_BRIEF_HASH = "a" * 64
_BASE_SHA_40 = "b" * 40
_BASE_SHA_64 = "c" * 64
_LINEAR_ISSUE = "TOL-701"
_STRUCTURED_LEAK_PROBE = "structured-leak-probe"
_BINDING_LINE_PREFIXES = (
    "bound_linear_issue:",
    "requested_linear_issue:",
    "base_sha:",
    "brief_hash:",
)


def _structured_v2_payload(*, base_sha: str = _BASE_SHA_40) -> dict[str, object]:
    """Mirror the relevant shape of a real Core structured v2 draft with a bound issue."""
    payload = _ok_payload()
    payload["order"] = {
        "order_id": _ORDER_ID,
        "state": "new",
        "linear": {"identifier": _LINEAR_ISSUE, "url": None, "pending": False},
        "bound_linear_issue": {"identifier": _LINEAR_ISSUE},
        "repo": _STRUCTURED_LEAK_PROBE,
        "worktree": f"/tmp/{_STRUCTURED_LEAK_PROBE}",
        "branch": _STRUCTURED_LEAK_PROBE,
        "idempotency_key": _STRUCTURED_LEAK_PROBE,
        "brief_hash": _BRIEF_HASH,
        "structured_submit": {
            "schema_id": "motion.order.structured-submit.v2",
            "client_id": _STRUCTURED_LEAK_PROBE,
            "idempotency_key": _STRUCTURED_LEAK_PROBE,
            "brief_hash": _BRIEF_HASH,
            "policy": {
                "gates": [_STRUCTURED_LEAK_PROBE],
                "base_sha": base_sha,
                "human_gates": [_STRUCTURED_LEAK_PROBE] * 3,
                "review": _STRUCTURED_LEAK_PROBE,
                "non_goals": [_STRUCTURED_LEAK_PROBE] * 3,
            },
            "linear_issue": _LINEAR_ISSUE,
        },
        "created_at": "2026-10-11T00:00:00.000Z",
        "updated_at": "2026-10-11T00:00:00.000Z",
        "spec_path": f"/tmp/{_STRUCTURED_LEAK_PROBE}/spec.md",
    }
    return payload


def _structured_order(payload: dict[str, object]) -> dict[str, object]:
    order = payload["order"]
    assert isinstance(order, dict)
    return order


def _binding_lines(summary: str) -> list[str]:
    return [line for line in summary.splitlines() if line.startswith(_BINDING_LINE_PREFIXES)]


def _read_with_payload(tmp_path: Path, payload: dict[str, object]) -> str:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    _install_fake_cli(core_root)
    node_dir = _fake_node_dir(tmp_path)

    def _runner(argv: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    return read_motion_order_status_summary(
        _ORDER_ID,
        {
            MOTION_CORE_ROOT_ENV: str(core_root),
            MOTION_ORDERS_ROOT_ENV: str(orders_root),
            "PATH": str(node_dir),
        },
        subprocess_runner=_runner,
    )


@pytest.mark.parametrize("base_sha", [_BASE_SHA_40, _BASE_SHA_64])
def test_read_motion_order_status_structured_v2_shows_bindings(
    tmp_path: Path, base_sha: str
) -> None:
    summary = _read_with_payload(tmp_path, _structured_v2_payload(base_sha=base_sha))
    lines = summary.splitlines()
    assert "order_status_ok: true" in lines
    assert "state: new" in lines
    assert _binding_lines(summary) == [
        f"bound_linear_issue: {_LINEAR_ISSUE}",
        f"base_sha: {base_sha}",
        f"brief_hash: {_BRIEF_HASH}",
    ]
    assert lines[-1].startswith("note: snapshot from Motion Core order status")
    assert _STRUCTURED_LEAK_PROBE not in summary
    assert "must not appear" not in summary


@pytest.mark.parametrize("bound_state", ["absent", "null"])
def test_read_motion_order_status_structured_v2_requested_but_unbound(
    tmp_path: Path, bound_state: str
) -> None:
    payload = _structured_v2_payload()
    order = _structured_order(payload)
    order["linear"] = None
    if bound_state == "absent":
        del order["bound_linear_issue"]
    else:
        order["bound_linear_issue"] = None
    summary = _read_with_payload(tmp_path, payload)
    assert _binding_lines(summary) == [
        f"requested_linear_issue: {_LINEAR_ISSUE}",
        f"base_sha: {_BASE_SHA_40}",
        f"brief_hash: {_BRIEF_HASH}",
    ]
    assert "bound_linear_issue:" not in summary
    assert _STRUCTURED_LEAK_PROBE not in summary


@pytest.mark.parametrize("issue_state", ["absent", "null"])
@pytest.mark.parametrize("bound_state", ["absent", "null"])
def test_read_motion_order_status_structured_v2_without_optional_issue(
    tmp_path: Path, issue_state: str, bound_state: str
) -> None:
    payload = _structured_v2_payload()
    order = _structured_order(payload)
    structured = order["structured_submit"]
    assert isinstance(structured, dict)
    order["linear"] = None
    if issue_state == "absent":
        del structured["linear_issue"]
    else:
        structured["linear_issue"] = None
    if bound_state == "absent":
        del order["bound_linear_issue"]
    else:
        order["bound_linear_issue"] = None
    summary = _read_with_payload(tmp_path, payload)
    assert "order_status_ok: true" in summary.splitlines()
    assert _binding_lines(summary) == [
        f"base_sha: {_BASE_SHA_40}",
        f"brief_hash: {_BRIEF_HASH}",
    ]
    assert _STRUCTURED_LEAK_PROBE not in summary


@pytest.mark.parametrize("issue_state", ["absent", "null"])
def test_read_motion_order_status_structured_v2_bound_without_requested_fails_closed(
    tmp_path: Path, issue_state: str
) -> None:
    payload = _structured_v2_payload()
    structured = _structured_order(payload)["structured_submit"]
    assert isinstance(structured, dict)
    if issue_state == "absent":
        del structured["linear_issue"]
    else:
        structured["linear_issue"] = None
    with pytest.raises(ValueError) as exc:
        _read_with_payload(tmp_path, payload)
    message = str(exc.value)
    assert message == (
        "Motion Core status order.bound_linear_issue is present without "
        "structured_submit.linear_issue"
    )
    assert _LINEAR_ISSUE not in message


_UNSAFE_PROBE = "TOL-9; rm -rf ~ secret-probe"


def _set_path(payload: dict[str, object], path: tuple[str, ...], value: object) -> None:
    target: object = payload
    for key in path[:-1]:
        assert isinstance(target, dict)
        target = target[key]
    assert isinstance(target, dict)
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("order", "structured_submit", "linear_issue"), _UNSAFE_PROBE),
        (("order", "structured_submit", "linear_issue"), "TOL-0"),
        (("order", "structured_submit", "linear_issue"), "tol-701"),
        (("order", "structured_submit", "linear_issue"), 701),
        (("order", "structured_submit", "linear_issue"), "TOL-702"),
        (("order", "structured_submit", "linear_issue"), " TOL-701"),
        (("order", "bound_linear_issue", "identifier"), _UNSAFE_PROBE),
        (("order", "bound_linear_issue", "identifier"), "TOL-702"),
        (("order", "bound_linear_issue", "identifier"), "tol-701"),
        (("order", "bound_linear_issue", "identifier"), "TOL-701\n"),
        (("order", "bound_linear_issue", "identifier"), None),
        (("order", "bound_linear_issue"), {}),
        (("order", "bound_linear_issue"), [_LINEAR_ISSUE]),
        (("order", "bound_linear_issue"), _UNSAFE_PROBE),
        (("order", "brief_hash"), _UNSAFE_PROBE),
        (("order", "brief_hash"), "A" * 64),
        (("order", "brief_hash"), "d" * 64),
        (("order", "brief_hash"), None),
        (("order", "structured_submit", "brief_hash"), "e" * 63),
        (("order", "structured_submit", "brief_hash"), "f" * 64),
        (("order", "structured_submit", "policy", "base_sha"), _UNSAFE_PROBE),
        (("order", "structured_submit", "policy", "base_sha"), "B" * 40),
        (("order", "structured_submit", "policy", "base_sha"), "b" * 39),
        (("order", "structured_submit", "policy", "base_sha"), None),
        (("order", "structured_submit", "policy"), None),
        (("order", "structured_submit", "schema_id"), _UNSAFE_PROBE),
        (("order", "structured_submit"), _UNSAFE_PROBE),
    ],
)
def test_read_motion_order_status_structured_v2_rejects_bad_bindings_without_echo(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    payload = _structured_v2_payload()
    _set_path(payload, path, value)
    with pytest.raises(ValueError) as exc:
        _read_with_payload(tmp_path, payload)
    message = str(exc.value)
    assert message.startswith("Motion Core status order.")
    if isinstance(value, str):
        assert value not in message
    assert "TOL-" not in message
    assert "secret-probe" not in message
    assert _STRUCTURED_LEAK_PROBE not in message


def test_read_motion_order_status_legacy_order_omits_bindings(tmp_path: Path) -> None:
    payload = _ok_payload()
    payload["order"] = {
        "order_id": _ORDER_ID,
        "state": "running",
        "brief_hash": _BRIEF_HASH,
        "bound_linear_issue": {"identifier": _LINEAR_ISSUE},
    }
    summary = _read_with_payload(tmp_path, payload)
    assert not any(line.startswith(_BINDING_LINE_PREFIXES) for line in summary.splitlines())
    assert summary == format_motion_order_safe_summary(
        order_id=_ORDER_ID,
        state="running",
        cancellation_phase="none",
        receipt_category_count=2,
        report_ok=True,
        report_unavailable_count=0,
        report_mismatch_count=0,
    )
