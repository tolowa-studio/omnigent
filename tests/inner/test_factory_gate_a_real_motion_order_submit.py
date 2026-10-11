"""Motion Core draft order submit bridge for factory-gate-a-real chat harness."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from dev.factory.gate_a_real.orchestration import RealTaskRunResult
from omnigent.factory.gate_a.motion_order_status import (
    MOTION_CORE_ROOT_ENV,
    MOTION_ORDERS_ROOT_ENV,
)
from omnigent.factory.gate_a.motion_order_submit import (
    MOTION_ORDER_SUBMIT_ENABLE_ENV,
    MOTION_TASK_CONTRACTS_DIR_ENV,
    expected_structured_order_id,
    load_task_contract_bytes,
    motion_order_submit_enabled,
    submit_motion_order_draft,
)
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_CHAT_ENV,
    OperatorCommand,
    parse_operator_command,
)
from omnigent.inner.datamodel import Message
from omnigent.inner.executor import ExecutorError, TextChunk
from omnigent.inner.factory_gate_a_real_harness import FactoryGateARealExecutor

_TASK_ID = "unit-order-task"
_SECRET_OBJECTIVE = "do not leak objective text"
_FIXTURE_CLI = Path(__file__).resolve().parents[1] / "fixtures" / "motion_order_new_fixture.mjs"


def _sample_contract(task_id: str = _TASK_ID, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "idempotency_key": task_id,
        "client": "fixture-client",
        "repo": "fixture-org/fixture-repo",
        "worktree": "/tmp/omnigent-fixture-worktree",
        "branch": "main",
        "authority_ref": "fixture-authority",
        "objective": _SECRET_OBJECTIVE,
        "scope": "fixture scope",
        "acceptance": "fixture acceptance",
        "gates": ["npm test"],
        "base_sha": "a" * 40,
        "human_gates": ["human sign-off"],
        "review": "fixture review lane",
        "non_goals": [],
    }
    base.update(overrides)
    return base


def _write_contract(contracts_dir: Path, task_id: str, contract: dict[str, object]) -> Path:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_dir / f"{task_id}.json"
    path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    return path


def _install_fixture_cli(core_root: Path) -> Path:
    bin_dir = core_root / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "motion-order.mjs"
    shutil.copy(_FIXTURE_CLI, cli)
    cli.chmod(cli.stat().st_mode | 0o111)
    return cli


def _submit_env(
    *,
    contracts_dir: Path,
    core_root: Path,
    orders_root: Path,
) -> dict[str, str]:
    import os

    if shutil.which("node", path=os.environ.get("PATH")) is None:
        pytest.skip("node is required for motion order submit fixture tests")
    return {
        MOTION_ORDER_SUBMIT_ENABLE_ENV: "1",
        MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts_dir),
        MOTION_CORE_ROOT_ENV: str(core_root),
        MOTION_ORDERS_ROOT_ENV: str(orders_root),
        "PATH": os.environ.get("PATH", ""),
    }


def _bind_real_chat_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")


async def _collect_events(executor: FactoryGateARealExecutor, text: str) -> list[object]:
    events: list[object] = []
    async for event in executor.run_turn([Message(role="user", content=text)], [], ""):
        events.append(event)
    return events


def test_parse_operator_command_order_submit_exact() -> None:
    assert parse_operator_command(f"order submit {_TASK_ID}") == OperatorCommand(
        kind="order_submit", task_id=_TASK_ID
    )
    assert parse_operator_command("order submit") is None
    assert parse_operator_command(f"order submit {_TASK_ID} extra") is None
    assert parse_operator_command(f"ORDER submit {_TASK_ID}") is None
    assert parse_operator_command("order status ord-abc") == OperatorCommand(
        kind="order_status", task_id="ord-abc"
    )


def test_motion_order_submit_disabled_by_default() -> None:
    assert not motion_order_submit_enabled({})


def test_load_task_contract_rejects_unknown_field(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    contract = _sample_contract()
    malicious_key = "sk-live-\x00leak-me"
    contract[malicious_key] = "nope"
    _write_contract(contracts, _TASK_ID, contract)
    with pytest.raises(ValueError, match="unsupported fields") as exc:
        load_task_contract_bytes(_TASK_ID, {MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts)})
    assert malicious_key not in str(exc.value)
    assert "sk-live" not in str(exc.value)


def test_load_task_contract_idempotency_mismatch(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    _write_contract(contracts, _TASK_ID, _sample_contract(idempotency_key="other"))
    with pytest.raises(ValueError, match="idempotency_key"):
        load_task_contract_bytes(_TASK_ID, {MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts)})


def test_submit_motion_order_draft_first_create_and_idempotent_replay(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract())
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)

    first = submit_motion_order_draft(_TASK_ID, env)
    assert "order_submit_ok: true" in first
    assert "submit_status: created" in first
    assert "state: new" in first
    assert _SECRET_OBJECTIVE not in first
    assert "fixture-authority" not in first
    order_id_line = next(line for line in first.splitlines() if line.startswith("order_id:"))
    order_id = order_id_line.split(":", 1)[1].strip()
    assert order_id == expected_structured_order_id("fixture-client", _TASK_ID)
    assert order_id.startswith("struct-")

    second = submit_motion_order_draft(_TASK_ID, env)
    assert "submit_status: idempotent_replay" in second
    assert f"order_id: {order_id}" in second
    assert len(list(orders_root.glob("**/*"))) >= 1


def test_submit_motion_order_draft_changed_policy_fails_closed(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    path = _write_contract(contracts, _TASK_ID, _sample_contract())
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)
    submit_motion_order_draft(_TASK_ID, env)
    contract = _sample_contract(gates=["npm test", "extra gate"])
    path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="idempotency conflict"):
        submit_motion_order_draft(_TASK_ID, env)


def test_load_task_contract_rejects_missing_policy_before_cli(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    contract = _sample_contract()
    del contract["gates"]
    _write_contract(contracts, _TASK_ID, contract)
    with pytest.raises(ValueError, match="policy is invalid"):
        load_task_contract_bytes(_TASK_ID, {MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts)})


def test_load_task_contract_rejects_invalid_base_sha(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    _write_contract(contracts, _TASK_ID, _sample_contract(base_sha="not-a-sha"))
    with pytest.raises(ValueError, match="policy is invalid"):
        load_task_contract_bytes(_TASK_ID, {MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts)})


def test_submit_invalid_policy_never_invokes_cli(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract(review=""))
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)

    def _forbid_cli(
        argv: list[str], _env: dict[str, str], _stdin: str
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("CLI must not run when policy is invalid")

    with pytest.raises(ValueError, match="policy is invalid"):
        submit_motion_order_draft(_TASK_ID, env, subprocess_runner=_forbid_cli)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gates", ["- injected list marker"]),
        ("gates", ["* injected list marker"]),
        ("gates", ["# injected heading"]),
        ("gates", ["> injected quote"]),
        ("gates", ["`injected code`"]),
        ("gates", ["1. numbered list"]),
        ("gates", ["2) numbered list"]),
        ("gates", ["safe gate with # WORK ORDER embedded"]),
        ("human_gates", ["- human gate marker"]),
        ("non_goals", ["# WORK ORDER in non goal"]),
        (
            "review",
            "**CLIENT:** injected work-order field",
        ),
        ("review", "# heading lane"),
        ("review", "--- horizontal rule lane"),
        ("review", "lane mentions # work order marker"),
    ],
)
def test_load_task_contract_rejects_policy_injection_markers(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    contracts = tmp_path / "contracts"
    _write_contract(contracts, _TASK_ID, _sample_contract(**{field: value}))
    with pytest.raises(ValueError, match="policy is invalid") as exc:
        load_task_contract_bytes(_TASK_ID, {MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts)})
    assert str(value) not in str(exc.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gates", ["- injected list marker"]),
        ("review", "**CLIENT:** injected work-order field"),
    ],
)
def test_submit_rejects_policy_injection_before_cli(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract(**{field: value}))
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)

    def _forbid_cli(
        argv: list[str], _env: dict[str, str], _stdin: str
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("CLI must not run when policy injection is present")

    with pytest.raises(ValueError, match="policy is invalid"):
        submit_motion_order_draft(_TASK_ID, env, subprocess_runner=_forbid_cli)


def test_load_task_contract_rejects_non_canonical_client(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    _write_contract(contracts, _TASK_ID, _sample_contract(client="Fixture-Client"))
    with pytest.raises(ValueError, match="canonical client"):
        load_task_contract_bytes(_TASK_ID, {MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts)})


def test_submit_rejects_before_cli(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    with pytest.raises(ValueError, match="task_id"):
        load_task_contract_bytes("../evil", {MOTION_TASK_CONTRACTS_DIR_ENV: str(contracts)})


def test_submit_cli_error_fail_closed(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(
        contracts,
        _TASK_ID,
        _sample_contract(client="disallowed-client"),
    )
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)
    with pytest.raises(ValueError, match="exited with code"):
        submit_motion_order_draft(_TASK_ID, env)


def test_submit_malformed_cli_json(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract())
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)

    def _runner(
        argv: list[str], _env: dict[str, str], _stdin: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "not-json", "")

    with pytest.raises(ValueError, match="not valid JSON"):
        submit_motion_order_draft(_TASK_ID, env, subprocess_runner=_runner)


def test_submit_mismatched_stable_order_id_in_response(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract())
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)
    payload = {
        "ok": True,
        "idempotent": False,
        "structured": True,
        "order_id": "struct-" + ("a" * 32),
        "work_id": "struct-" + ("a" * 32),
        "order_path": "/secret/path",
        "brief_hash": "a" * 64,
        "state": "new",
        "linear": None,
    }

    def _runner(
        argv: list[str], _env: dict[str, str], _stdin: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="expected stable id"):
        submit_motion_order_draft(_TASK_ID, env, subprocess_runner=_runner)


def test_submit_core_exit_five_ambiguous_index(tmp_path: Path) -> None:
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract())
    env = _submit_env(contracts_dir=contracts, core_root=core_root, orders_root=orders_root)

    def _runner(
        argv: list[str], _env: dict[str, str], _stdin: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 5, "", "ambiguous index detail")

    with pytest.raises(ValueError, match="idempotency index ambiguous"):
        submit_motion_order_draft(_TASK_ID, env, subprocess_runner=_runner)


@pytest.mark.asyncio
async def test_executor_order_submit_does_not_resolve_gate_a_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_real_chat_minimal(monkeypatch)
    monkeypatch.delenv("OMNIGENT_FACTORY_GATE_A_REAL_SPEC", raising=False)
    monkeypatch.delenv("OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS", raising=False)
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract())
    import os

    if shutil.which("node", path=os.environ.get("PATH")) is None:
        pytest.skip("node is required for motion order submit fixture tests")
    monkeypatch.setenv(MOTION_ORDER_SUBMIT_ENABLE_ENV, "1")
    monkeypatch.setenv(MOTION_TASK_CONTRACTS_DIR_ENV, str(contracts))
    monkeypatch.setenv(MOTION_CORE_ROOT_ENV, str(core_root))
    monkeypatch.setenv(MOTION_ORDERS_ROOT_ENV, str(orders_root))

    run_calls = 0

    def _forbid_run(*_a: object, **_k: object) -> RealTaskRunResult:
        nonlocal run_calls
        run_calls += 1
        raise AssertionError("run must not be invoked")

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _forbid_run)

    events = await _collect_events(FactoryGateARealExecutor(), f"order submit {_TASK_ID}")
    assert run_calls == 0
    assert any(isinstance(e, TextChunk) and "order_submit_ok: true" in e.text for e in events)


@pytest.mark.asyncio
async def test_executor_order_submit_disabled_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_real_chat_minimal(monkeypatch)
    events = await _collect_events(FactoryGateARealExecutor(), f"order submit {_TASK_ID}")
    assert any(
        isinstance(e, ExecutorError) and MOTION_ORDER_SUBMIT_ENABLE_ENV in e.message
        for e in events
    )


@pytest.mark.asyncio
async def test_executor_order_status_unchanged_when_submit_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.factory.gate_a.motion_order_status import format_motion_order_safe_summary

    _bind_real_chat_minimal(monkeypatch)
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract())
    monkeypatch.setenv(MOTION_ORDER_SUBMIT_ENABLE_ENV, "1")
    monkeypatch.setenv(MOTION_TASK_CONTRACTS_DIR_ENV, str(contracts))
    monkeypatch.setenv(MOTION_CORE_ROOT_ENV, str(core_root))
    monkeypatch.setenv(MOTION_ORDERS_ROOT_ENV, str(orders_root))

    submit_calls = 0

    def _forbid_submit(*_a: object, **_k: object) -> str:
        nonlocal submit_calls
        submit_calls += 1
        raise AssertionError("submit must not run for order status")

    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.submit_motion_order_draft",
        _forbid_submit,
    )
    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.read_motion_order_status_summary",
        lambda order_id, **kwargs: format_motion_order_safe_summary(
            order_id=order_id,
            state="new",
            cancellation_phase=None,
            receipt_category_count=0,
            report_ok=True,
            report_unavailable_count=0,
            report_mismatch_count=0,
        ),
    )

    events = await _collect_events(FactoryGateARealExecutor(), "order status ord-abc-123")
    assert submit_calls == 0
    assert any(isinstance(e, TextChunk) and "order_status_ok: true" in e.text for e in events)


@pytest.mark.asyncio
async def test_http_order_submit_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_real_chat_minimal(monkeypatch)
    core_root = tmp_path / "motion-core"
    orders_root = tmp_path / "orders"
    orders_root.mkdir()
    contracts = tmp_path / "contracts"
    _install_fixture_cli(core_root)
    _write_contract(contracts, _TASK_ID, _sample_contract())
    import os

    if shutil.which("node", path=os.environ.get("PATH")) is None:
        pytest.skip("node is required for motion order submit fixture tests")
    monkeypatch.setenv(MOTION_ORDER_SUBMIT_ENABLE_ENV, "1")
    monkeypatch.setenv(MOTION_TASK_CONTRACTS_DIR_ENV, str(contracts))
    monkeypatch.setenv(MOTION_CORE_ROOT_ENV, str(core_root))
    monkeypatch.setenv(MOTION_ORDERS_ROOT_ENV, str(orders_root))

    from omnigent.inner import factory_gate_a_real_harness

    app = factory_gate_a_real_harness.create_app()
    conversation_id = "conv_motion_order_submit"
    app.state.conversation_id = conversation_id
    body = {
        "type": "message",
        "role": "user",
        "model": "factory-gate-a-real-beta",
        "content": [{"type": "input_text", "text": f"order submit {_TASK_ID}"}],
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
    assert "order_submit_ok: true" in text
    assert _SECRET_OBJECTIVE not in text
    assert "must not appear" not in text
