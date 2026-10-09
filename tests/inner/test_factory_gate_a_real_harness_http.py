"""HTTP integration tests for factory-gate-a-real via ExecutorAdapter FastAPI app."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from dev.factory.gate_a_real.constants import REAL_TASK_ENV
from dev.factory.gate_a_real.orchestration import RealTaskRunResult
from dev.factory.gate_a_real.profile import (
    materialize_real_task_cursor_config_dir,
    materialize_real_task_review_config_dir,
)
from dev.factory.gate_a_real.receipt import RealTaskReceipt, utc_now_iso
from dev.factory.gate_a_real.spec import canonical_spec_sha256, load_real_task_spec
from omnigent.factory.gate_a.real_chat import (
    REAL_TASK_ARTIFACTS_ENV,
    REAL_TASK_CHAT_ENV,
    REAL_TASK_SPEC_ENV,
)

TASK_ID = "beta-chat-smoke-20261008"
_CONVERSATION_ID = "conv_factory_gate_a_real_http"
_STATUS_PROMPT = f"status {TASK_ID}"
_RAW_PROBLEM_SNIPPET = "builder attempt already recorded"


@dataclass
class _ParsedSSEEvent:
    event: str
    data: dict[str, Any]


def _write_spec(tmp_path: Path, workspace: Path, task_id: str = TASK_ID) -> Path:
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
    path = tmp_path / "task.spec.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _sample_receipt(
    task_id: str = TASK_ID,
    *,
    ok: bool = True,
    spec_sha256: str = "a" * 64,
    workspace: str = "/tmp/ws",
    problems: list[str] | None = None,
) -> RealTaskReceipt:
    if problems is None:
        problems = [] if ok else [_RAW_PROBLEM_SNIPPET]
    return RealTaskReceipt(
        ok=ok,
        task_id=task_id,
        spec_sha256=spec_sha256,
        workspace=workspace,
        problems=problems,
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


def _bind_real_chat_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec_path: Path,
    artifacts_dir: Path,
) -> None:
    monkeypatch.setenv(REAL_TASK_ENV, "1")
    monkeypatch.setenv(REAL_TASK_CHAT_ENV, "1")
    monkeypatch.setenv(REAL_TASK_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ENV, str(artifacts_dir))


def _status_message_body() -> dict[str, object]:
    return {
        "type": "message",
        "role": "user",
        "model": "factory-gate-a-real-beta",
        "content": [{"type": "input_text", "text": _STATUS_PROMPT}],
    }


async def _stream_iter(response: httpx.Response) -> AsyncIterator[_ParsedSSEEvent]:
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            event_line = next(
                (line for line in frame.splitlines() if line.startswith("event:")),
                None,
            )
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")),
                None,
            )
            if event_line is None or data_line is None:
                continue
            event_name = event_line[len("event:") :].strip()
            data_payload = json.loads(data_line[len("data:") :].strip())
            yield _ParsedSSEEvent(event=event_name, data=data_payload)


async def _post_status_turn(app: object) -> list[_ParsedSSEEvent]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness.test") as client:
        events: list[_ParsedSSEEvent] = []
        async with client.stream(
            "POST",
            f"/v1/sessions/{_CONVERSATION_ID}/events",
            json=_status_message_body(),
        ) as response:
            response.raise_for_status()
            async for event in _stream_iter(response):
                events.append(event)
        return events


def _combined_text_deltas(events: list[_ParsedSSEEvent]) -> str:
    return "".join(
        event.data.get("delta", "")
        for event in events
        if event.event == "response.output_text.delta"
    )


@pytest.mark.asyncio
async def test_http_status_missing_receipt_fails_without_run_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _bind_real_chat_env(monkeypatch, spec_path=spec_path, artifacts_dir=artifacts)

    run_calls = 0

    def _forbid_run(*_a: object, **_k: object) -> RealTaskRunResult:
        nonlocal run_calls
        run_calls += 1
        raise AssertionError("run_real_task_gate must not be invoked for status")

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _forbid_run)

    from omnigent.inner import factory_gate_a_real_harness

    app = factory_gate_a_real_harness.create_app()
    app.state.conversation_id = _CONVERSATION_ID

    events = await _post_status_turn(app)

    assert run_calls == 0
    assert _combined_text_deltas(events) == ""
    failed = next((e for e in events if e.event == "response.failed"), None)
    assert failed is not None
    error = failed.data["response"]["error"]
    assert "receipt not found" in error["message"]
    assert not any(e.event == "response.completed" for e in events)


@pytest.mark.asyncio
async def test_http_status_with_bound_receipt_emits_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_path = _write_spec(tmp_path, workspace)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _receipt_for_spec(spec_path, ok=False).write(artifacts / "receipt.json")
    _bind_real_chat_env(monkeypatch, spec_path=spec_path, artifacts_dir=artifacts)

    run_calls = 0

    def _forbid_run(*_a: object, **_k: object) -> RealTaskRunResult:
        nonlocal run_calls
        run_calls += 1
        raise AssertionError("run_real_task_gate must not be invoked for status")

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _forbid_run)

    from omnigent.inner import factory_gate_a_real_harness

    app = factory_gate_a_real_harness.create_app()
    app.state.conversation_id = _CONVERSATION_ID

    events = await _post_status_turn(app)
    text = _combined_text_deltas(events)

    assert run_calls == 0
    assert events[-1].event == "response.completed"
    assert "receipt_ok: False" in text
    assert "problem_count:" in text
    assert "verify_exit_code:" in text
    assert _RAW_PROBLEM_SNIPPET not in text
    assert "do the thing" not in text
