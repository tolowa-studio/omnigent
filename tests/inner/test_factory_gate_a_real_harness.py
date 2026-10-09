"""Offline tests for factory-gate-a-real chat harness (no live Cursor)."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from dev.factory.gate_a_real.orchestration import RealTaskRunOptions, RealTaskRunResult
from dev.factory.gate_a_real.profile import (
    materialize_real_task_cursor_config_dir,
    materialize_real_task_review_config_dir,
)
from dev.factory.gate_a_real.receipt import RealTaskReceipt, utc_now_iso
from dev.factory.gate_a_real.spec import canonical_spec_sha256, load_real_task_spec
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_ARTIFACTS_ENV,
    REAL_TASK_ARTIFACTS_ROOT_ENV,
    REAL_TASK_CHAT_ENV,
    REAL_TASK_SPEC_DIR_ENV,
    REAL_TASK_SPEC_ENV,
    OperatorCommand,
    factory_gate_a_real_enabled,
    format_safe_summary,
    latest_user_message_text,
    parse_operator_command,
    read_status_summary,
    resolve_bound_paths,
    resolve_task_paths,
    validate_operator_task_id,
)
from omnigent.harness_plugins import valid_harnesses
from omnigent.inner.datamodel import Message
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.factory_gate_a_real_harness import FactoryGateARealExecutor
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE
from omnigent.spec.types import ExecutorSpec, LLMConfig
from omnigent.spec.validator import validate


def _write_spec(
    tmp_path: Path,
    workspace: Path,
    task_id: str = "unit-task",
    *,
    spec_dir: Path | None = None,
) -> Path:
    profile_dir = tmp_path / "profile"
    profile = materialize_real_task_cursor_config_dir(profile_dir)
    review_profile = materialize_real_task_review_config_dir(tmp_path / "review-profile")
    spec: dict[str, object] = {
        "task_id": task_id,
        "workspace": str(workspace.resolve()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "prompt": "do the thing",
        "deliverable_paths": ["out.txt"],
        "verify_command": ["test", "-f", "out.txt"],
        "config_hashes": profile["effective_config_hashes"],
        "review_config_hashes": review_profile["effective_config_hashes"],
    }
    spec["spec_sha256"] = canonical_spec_sha256(spec)
    if spec_dir is not None:
        spec_dir.mkdir(parents=True, exist_ok=True)
        path = spec_dir / f"{task_id}.json"
    else:
        path = tmp_path / "task.spec.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _bind_registry_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec_root: Path,
    artifacts_root: Path,
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.delenv(REAL_TASK_SPEC_ENV, raising=False)
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ENV, raising=False)
    monkeypatch.setenv(REAL_TASK_SPEC_DIR_ENV, str(spec_root))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ROOT_ENV, str(artifacts_root))


def _sample_receipt(
    task_id: str = "unit-task",
    *,
    ok: bool = True,
    spec_sha256: str = "a" * 64,
    workspace: str = "/tmp/ws",
) -> RealTaskReceipt:
    return RealTaskReceipt(
        ok=ok,
        task_id=task_id,
        spec_sha256=spec_sha256,
        workspace=workspace,
        problems=[] if ok else ["builder attempt already recorded"],
        builder_session_ids=["build-1"],
        review_session_ids=["review-1"],
        builder_exit_code=0 if ok else None,
        review_exit_code=0 if ok else None,
        verify_exit_code=0 if ok else None,
        deliverable_manifest_sha256="b" * 64,
        post_review_manifest_sha256="c" * 64,
        freeze_manifest_path="/tmp/artifacts/freeze/manifest.json",
        review_pass=ok,
        completed_at=utc_now_iso(),
    )


def _receipt_for_spec(spec_path: Path, *, ok: bool = True) -> RealTaskReceipt:
    spec = load_real_task_spec(spec_path)
    return _sample_receipt(
        task_id=spec.task_id,
        ok=ok,
        spec_sha256=spec.spec_sha256,
        workspace=str(spec.workspace),
    )


async def _collect_events(
    executor: FactoryGateARealExecutor,
    text: str,
    *,
    content: object | None = None,
) -> list[object]:
    events: list[object] = []
    async for event in executor.run_turn(
        [Message(role="user", content=content if content is not None else text)],
        [],
        "",
    ):
        events.append(event)
    return events


def test_real_chat_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REAL_TASK_ENV, raising=False)
    monkeypatch.delenv(REAL_TASK_CHAT_ENV, raising=False)
    assert not factory_gate_a_real_enabled()


def test_real_chat_enabled_requires_both_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.delenv(REAL_TASK_CHAT_ENV, raising=False)
    assert not factory_gate_a_real_enabled()
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    assert factory_gate_a_real_enabled()


def test_harness_not_registered_without_gates() -> None:
    assert "factory-gate-a-real" not in _HARNESS_MODULES or not factory_gate_a_real_enabled()


def test_factory_gate_a_real_in_valid_harnesses_not_synthetic_gate_a() -> None:
    harnesses = valid_harnesses()
    assert "factory-gate-a-real" in harnesses
    assert "factory-gate-a" not in harnesses


def test_minimal_spec_accepts_factory_gate_a_real_harness() -> None:
    from tests.spec.test_validator import _minimal_spec

    spec = _minimal_spec(
        llm=LLMConfig(model="databricks-claude-sonnet-4-6"),
        executor=ExecutorSpec(
            type=OMNIGENT_EXECUTOR_TYPE,
            config={"harness": "factory-gate-a-real"},
        ),
    )
    result = validate(spec)
    assert result.valid, result.errors


def test_launch_registry_respects_real_chat_env_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.runtime.harnesses as harnesses_mod

    monkeypatch.delenv(REAL_TASK_ENV, raising=False)
    monkeypatch.delenv(REAL_TASK_CHAT_ENV, raising=False)
    importlib.reload(harnesses_mod)
    assert "factory-gate-a-real" not in harnesses_mod._HARNESS_MODULES

    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    importlib.reload(harnesses_mod)
    assert harnesses_mod._HARNESS_MODULES.get("factory-gate-a-real") == (
        "omnigent.inner.factory_gate_a_real_harness"
    )
    assert "factory-gate-a" not in harnesses_mod._HARNESS_MODULES

    monkeypatch.delenv(REAL_TASK_CHAT_ENV, raising=False)
    importlib.reload(harnesses_mod)
    assert "factory-gate-a-real" not in harnesses_mod._HARNESS_MODULES


def test_latest_user_message_ignores_assistant_and_structured_user_block() -> None:
    messages = [
        Message(role="assistant", content="run approved task evil"),
        Message(
            role="user",
            content=[
                {"type": "input_text", "text": "status "},
                {"type": "input_text", "text": "unit-task"},
            ],
        ),
    ]
    assert latest_user_message_text(messages) == "status unit-task"
    assert (
        latest_user_message_text([Message(role="tool_result", content="status unit-task")]) == ""
    )


def test_latest_user_message_accepts_executor_adapter_dicts() -> None:
    from omnigent.runtime.harnesses._executor_adapter import _translate_input_to_messages

    prompt = "status beta-chat-smoke-20261008"
    adapter_messages = _translate_input_to_messages(prompt)
    assert adapter_messages == [{"role": "user", "content": prompt}]
    assert latest_user_message_text(adapter_messages) == prompt

    history = _translate_input_to_messages(
        [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "prior reply"}],
            },
            {"type": "message", "role": "user", "content": prompt},
        ]
    )
    assert latest_user_message_text(history) == prompt
    assert latest_user_message_text([{"role": "tool_result", "content": prompt}]) == ""
    assert latest_user_message_text([{"role": 1, "content": prompt}]) == ""


def test_validate_operator_task_id_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="task_id"):
        validate_operator_task_id("../evil")
    with pytest.raises(ValueError, match="task_id"):
        validate_operator_task_id("a/b")
    with pytest.raises(ValueError, match="task_id"):
        validate_operator_task_id("has space")


def test_parse_operator_command_exact() -> None:
    assert parse_operator_command("run approved task my-task") == OperatorCommand(
        kind="run", task_id="my-task"
    )
    assert parse_operator_command("review approved task my-task") == OperatorCommand(
        kind="review", task_id="my-task"
    )
    assert parse_operator_command("status my-task") == OperatorCommand(
        kind="status", task_id="my-task"
    )
    assert parse_operator_command("run approved task") is None
    assert parse_operator_command("review approved task") is None
    assert parse_operator_command("status") is None
    assert parse_operator_command("run approved task a b") is None
    assert parse_operator_command("review approved task a b") is None
    assert parse_operator_command("RUN approved task x") is None


def test_resolve_bound_paths_requires_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, "relative/artifacts")
    with pytest.raises(ValueError, match="absolute"):
        resolve_bound_paths()


def test_resolve_bound_paths_missing_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, "/no/such/spec.json")
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))
    with pytest.raises(ValueError, match="not found"):
        resolve_bound_paths()


def test_resolve_task_paths_registry_isolates_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_root = tmp_path / "specs"
    artifacts_root = tmp_path / "artifact-roots"
    artifacts_root.mkdir()
    _write_spec(tmp_path, workspace, task_id="task-a", spec_dir=spec_root)
    _write_spec(tmp_path, workspace, task_id="task-b", spec_dir=spec_root)
    _bind_registry_env(monkeypatch, spec_root=spec_root, artifacts_root=artifacts_root)

    spec_a, art_a = resolve_task_paths("task-a")
    spec_b, art_b = resolve_task_paths("task-b")
    assert spec_a.name == "task-a.json"
    assert spec_b.name == "task-b.json"
    assert art_a == artifacts_root / "task-a"
    assert art_b == artifacts_root / "task-b"
    assert art_a != art_b


def test_resolve_task_paths_registry_rejects_in_root_spec_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_root = tmp_path / "specs"
    artifacts_root = tmp_path / "artifact-roots"
    artifacts_root.mkdir()
    canonical = _write_spec(tmp_path, workspace, task_id="canonical", spec_dir=spec_root)
    (spec_root / "task-a.json").symlink_to(canonical.name)
    _bind_registry_env(monkeypatch, spec_root=spec_root, artifacts_root=artifacts_root)

    with pytest.raises(ValueError, match="symlink"):
        resolve_task_paths("task-a")


def test_resolve_task_paths_registry_rejects_artifacts_symlink_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_root = tmp_path / "specs"
    artifacts_root = tmp_path / "artifact-roots"
    artifacts_root.mkdir()
    _write_spec(tmp_path, workspace, task_id="task-a", spec_dir=spec_root)
    (artifacts_root / "real-dir").mkdir()
    (artifacts_root / "task-a").symlink_to("real-dir", target_is_directory=True)
    _bind_registry_env(monkeypatch, spec_root=spec_root, artifacts_root=artifacts_root)

    with pytest.raises(ValueError, match="symlink"):
        resolve_task_paths("task-a")


def test_resolve_bound_paths_rejects_spec_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real_spec = _write_spec(tmp_path, workspace)
    link = tmp_path / "task.spec.link.json"
    link.symlink_to(real_spec)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(link))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))

    with pytest.raises(ValueError, match="symlink"):
        resolve_bound_paths()


def test_resolve_bound_paths_accepts_non_symlink_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))

    resolved_spec, resolved_art = resolve_bound_paths()
    assert resolved_spec == spec_path.resolve()
    assert resolved_art == artifacts.resolve()


@pytest.mark.asyncio
async def test_registry_rejects_spec_symlink_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_root = tmp_path / "specs"
    artifacts_root = tmp_path / "artifact-roots"
    artifacts_root.mkdir()
    canonical = _write_spec(tmp_path, workspace, task_id="canonical", spec_dir=spec_root)
    (spec_root / "task-a.json").symlink_to(canonical.name)
    _bind_registry_env(monkeypatch, spec_root=spec_root, artifacts_root=artifacts_root)

    events = await _collect_events(FactoryGateARealExecutor(), "status task-a")
    assert any(isinstance(e, ExecutorError) and "symlink" in e.message for e in events)


@pytest.mark.asyncio
async def test_registry_status_per_task_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_root = tmp_path / "specs"
    artifacts_root = tmp_path / "artifact-roots"
    artifacts_root.mkdir()
    spec_a = _write_spec(tmp_path, workspace, task_id="task-a", spec_dir=spec_root)
    spec_b = _write_spec(tmp_path, workspace, task_id="task-b", spec_dir=spec_root)
    art_a = artifacts_root / "task-a"
    art_b = artifacts_root / "task-b"
    art_a.mkdir()
    art_b.mkdir()
    _receipt_for_spec(spec_a, ok=True).write(art_a / "receipt.json")
    _receipt_for_spec(spec_b, ok=False).write(art_b / "receipt.json")
    _bind_registry_env(monkeypatch, spec_root=spec_root, artifacts_root=artifacts_root)

    events_a = await _collect_events(FactoryGateARealExecutor(), "status task-a")
    events_b = await _collect_events(FactoryGateARealExecutor(), "status task-b")
    text_a = "".join(e.text for e in events_a if isinstance(e, TextChunk))
    text_b = "".join(e.text for e in events_b if isinstance(e, TextChunk))
    assert "receipt_ok: True" in text_a
    assert "receipt_ok: False" in text_b


@pytest.mark.asyncio
async def test_registry_rejects_invalid_task_id_before_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_root = tmp_path / "specs"
    spec_root.mkdir()
    artifacts_root = tmp_path / "artifact-roots"
    artifacts_root.mkdir()
    _bind_registry_env(monkeypatch, spec_root=spec_root, artifacts_root=artifacts_root)
    events = await _collect_events(FactoryGateARealExecutor(), "status ../evil")
    assert any(isinstance(e, ExecutorError) and "task_id" in e.message for e in events)


@pytest.mark.asyncio
async def test_executor_rejects_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.delenv(REAL_TASK_SPEC_ENV, raising=False)
    monkeypatch.delenv(REAL_TASK_ARTIFACTS_ENV, raising=False)
    events = await _collect_events(FactoryGateARealExecutor(), "status unit-task")
    assert any(isinstance(e, ExecutorError) for e in events)


@pytest.mark.asyncio
async def test_executor_status_via_adapter_translated_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: CLI passes ExecutorAdapter dict messages, not Message dataclasses."""
    from omnigent.runtime.harnesses._executor_adapter import _translate_input_to_messages

    task_id = "beta-chat-smoke-20261008"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace, task_id=task_id)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _receipt_for_spec(spec_path).write(artifacts / "receipt.json")
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))

    run_calls = 0
    status_calls: list[str] = []

    def _forbid_run(*_a: object, **_k: object) -> RealTaskRunResult:
        nonlocal run_calls
        run_calls += 1
        raise AssertionError("run must not be invoked for status")

    def _stub_read_status(
        *,
        spec_path: Path,
        artifacts_dir: Path,
        task_id: str,
    ) -> str:
        del spec_path, artifacts_dir
        status_calls.append(task_id)
        return "receipt_ok: True\nstubbed status\n"

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _forbid_run)
    monkeypatch.setattr(
        "omnigent.inner.factory_gate_a_real_harness.read_status_summary",
        _stub_read_status,
    )

    messages = _translate_input_to_messages(f"status {task_id}")
    events: list[object] = []
    async for event in FactoryGateARealExecutor().run_turn(messages, [], ""):
        events.append(event)

    assert run_calls == 0
    assert status_calls == [task_id]
    assert any(isinstance(e, TextChunk) and "stubbed status" in e.text for e in events)
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_status_read_only_no_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    receipt = _receipt_for_spec(spec_path)
    receipt.write(artifacts / "receipt.json")
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))

    run_calls = 0

    def _forbid_run(*_a: object, **_k: object) -> RealTaskRunResult:
        nonlocal run_calls
        run_calls += 1
        raise AssertionError("run must not be invoked for status")

    monkeypatch.setattr(
        "omnigent.factory.gate_a.real_chat.run_real_task_gate",
        _forbid_run,
    )
    events = await _collect_events(FactoryGateARealExecutor(), "status unit-task")
    assert run_calls == 0
    assert any(isinstance(e, TextChunk) and "review_pass" in e.text for e in events)
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_executor_error_is_terminal_without_turn_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))
    events = await _collect_events(FactoryGateARealExecutor(), "run approved task wrong-id")
    assert any(isinstance(e, ExecutorError) and "does not match" in e.message for e in events)
    assert not any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_review_success_passes_review_only_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))
    receipt = _sample_receipt(ok=True)
    seen_options: list[RealTaskRunOptions] = []

    def _fake_run(spec: object, options: RealTaskRunOptions) -> RealTaskRunResult:
        del spec
        seen_options.append(options)
        art = options.artifacts_dir
        art.mkdir(parents=True, exist_ok=True)
        receipt.write(art / "receipt.json")
        return RealTaskRunResult(receipt=receipt, problems=[])

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _fake_run)
    events = await _collect_events(FactoryGateARealExecutor(), "review approved task unit-task")
    assert len(seen_options) == 1
    assert seen_options[0].review_only is True
    assert seen_options[0].artifacts_dir == artifacts.resolve()
    chunks = [e for e in events if isinstance(e, TextChunk)]
    assert any("review-only run" in c.text for c in chunks)
    assert any("receipt_ok: True" in c.text for c in chunks)
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_run_success_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))
    receipt = _sample_receipt(ok=True)

    def _fake_run(spec: object, options: RealTaskRunOptions) -> RealTaskRunResult:
        del spec
        art = options.artifacts_dir
        art.mkdir(parents=True, exist_ok=True)
        receipt.write(art / "receipt.json")
        return RealTaskRunResult(receipt=receipt, problems=[])

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _fake_run)
    events = await _collect_events(FactoryGateARealExecutor(), "run approved task unit-task")
    chunks = [e for e in events if isinstance(e, TextChunk)]
    assert any("receipt_ok: True" in c.text and "build-1" in c.text for c in chunks)
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_repeated_run_reports_builder_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "builder.stdout.txt").write_text("prior\n", encoding="utf-8")
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))
    spec = load_real_task_spec(spec_path)
    from dev.factory.gate_a_real.orchestration import run_real_task_gate

    monkeypatch.setenv(REAL_TASK_ENV, "1")
    result = run_real_task_gate(spec, RealTaskRunOptions(artifacts_dir=artifacts))
    assert not result.receipt.ok
    assert any("already recorded" in p for p in result.problems)

    events = await _collect_events(FactoryGateARealExecutor(), "run approved task unit-task")
    chunks = [e for e in events if isinstance(e, TextChunk)]
    assert any("receipt_ok: False" in c.text and "problem_count:" in c.text for c in chunks)
    assert not any("already recorded" in c.text for c in chunks)
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert "receipt_ok=False" in errors[0].message
    assert "problem_count=" in errors[0].message
    assert not any(isinstance(e, TurnComplete) for e in events)
    summary_idx = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, TextChunk) and "receipt_ok: False" in e.text
    )
    error_idx = next(i for i, e in enumerate(events) if isinstance(e, ExecutorError))
    assert summary_idx < error_idx


@pytest.mark.asyncio
async def test_run_failed_receipt_event_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts))
    receipt = _receipt_for_spec(spec_path, ok=False)

    def _fake_run(spec: object, options: RealTaskRunOptions) -> RealTaskRunResult:
        del spec
        art = options.artifacts_dir
        art.mkdir(parents=True, exist_ok=True)
        receipt.write(art / "receipt.json")
        return RealTaskRunResult(receipt=receipt, problems=list(receipt.problems))

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _fake_run)
    events = await _collect_events(FactoryGateARealExecutor(), "run approved task unit-task")
    assert any(isinstance(e, TextChunk) and "Starting Gate A" in e.text for e in events)
    assert any(isinstance(e, TextChunk) and "receipt_ok: False" in e.text for e in events)
    assert isinstance(events[-1], ExecutorError)
    assert not any(isinstance(e, TurnComplete) for e in events)


def test_format_safe_summary_omits_logs() -> None:
    receipt = _sample_receipt()
    receipt.builder_log_path = "/secret/builder.log"
    text = format_safe_summary(receipt, receipt_path=Path("/tmp/receipt.json"))
    assert "builder.log" not in text
    assert "do the thing" not in text


def test_read_status_task_mismatch(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _receipt_for_spec(spec_path).write(artifacts / "receipt.json")
    with pytest.raises(ValueError, match="does not match"):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="other-task",
        )


def test_failed_receipt_status(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _receipt_for_spec(spec_path, ok=False).write(artifacts / "receipt.json")
    summary = read_status_summary(
        spec_path=spec_path,
        artifacts_dir=artifacts,
        task_id="unit-task",
    )
    assert "receipt_ok: False" in summary
    assert "verify_exit_code:" in summary
    assert "already recorded" not in summary


def test_read_status_rejects_stale_receipt_spec_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    receipt = _receipt_for_spec(spec_path)
    receipt.spec_sha256 = "f" * 64
    receipt.write(artifacts / "receipt.json")
    with pytest.raises(ValueError, match="spec_sha256"):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="unit-task",
        )


def test_read_status_rejects_malformed_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    from omnigent.factory.gate_a.real_chat import RECEIPT_SCHEMA

    (artifacts / "receipt.json").write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "ok": "yes",
                "task_id": "unit-task",
                "spec_sha256": "a" * 64,
                "workspace": str(workspace.resolve()),
                "problems": [],
                "builder_session_ids": [],
                "review_session_ids": [],
                "builder_exit_code": None,
                "review_exit_code": None,
                "verify_exit_code": None,
                "deliverable_manifest_sha256": None,
                "post_review_manifest_sha256": None,
                "freeze_manifest_path": None,
                "review_pass": False,
                "completed_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="boolean"):
        read_status_summary(
            spec_path=spec_path,
            artifacts_dir=artifacts,
            task_id="unit-task",
        )
