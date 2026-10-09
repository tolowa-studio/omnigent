"""HTTP integration test for operator-pinned Gate A multi-task registry binding."""

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
    REAL_TASK_ARTIFACTS_ROOT_ENV,
    REAL_TASK_CHAT_ENV,
    REAL_TASK_SPEC_DIR_ENV,
    REAL_TASK_SPEC_ENV,
)

_APPROVED_TASK_ID = "beta-chat-smoke-20261008"
_FOREIGN_TASK_ID = "beta-chat-smoke-20261009"
_UNREGISTERED_TASK_ID = "beta-chat-smoke-unregistered"
_CONVERSATION_ID = "conv_factory_gate_a_real_registry_http"


@dataclass
class _ParsedSSEEvent:
    event: str
    data: dict[str, Any]


def _write_spec(
    tmp_path: Path,
    workspace: Path,
    task_id: str,
    *,
    spec_dir: Path,
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
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / f"{task_id}.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _sample_receipt(
    task_id: str,
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
    monkeypatch.setenv(REAL_TASK_SPEC_DIR_ENV, str(spec_root.resolve()))
    monkeypatch.setenv(REAL_TASK_ARTIFACTS_ROOT_ENV, str(artifacts_root.resolve()))


def _message_body(prompt: str) -> dict[str, object]:
    return {
        "type": "message",
        "role": "user",
        "model": "factory-gate-a-real-beta",
        "content": [{"type": "input_text", "text": prompt}],
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


async def _post_operator_turn(app: object, prompt: str) -> list[_ParsedSSEEvent]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness.test") as client:
        events: list[_ParsedSSEEvent] = []
        async with client.stream(
            "POST",
            f"/v1/sessions/{_CONVERSATION_ID}/events",
            json=_message_body(prompt),
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


def _failed_message(events: list[_ParsedSSEEvent]) -> str:
    failed = next((e for e in events if e.event == "response.failed"), None)
    assert failed is not None
    return failed.data["response"]["error"]["message"]


@pytest.mark.asyncio
async def test_http_registry_approved_status_and_foreign_task_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec_root = tmp_path / "specs"
    artifacts_root = tmp_path / "artifact-roots"
    artifacts_root.mkdir()
    approved_spec = _write_spec(
        tmp_path,
        workspace,
        _APPROVED_TASK_ID,
        spec_dir=spec_root,
    )
    approved_artifacts = artifacts_root / _APPROVED_TASK_ID
    approved_artifacts.mkdir()
    _receipt_for_spec(approved_spec, ok=True).write(approved_artifacts / "receipt.json")
    _write_spec(tmp_path, workspace, _FOREIGN_TASK_ID, spec_dir=spec_root)
    foreign_artifacts = artifacts_root / _FOREIGN_TASK_ID
    foreign_artifacts.mkdir()
    _bind_registry_env(monkeypatch, spec_root=spec_root, artifacts_root=artifacts_root)

    run_calls = 0

    def _forbid_run(*_a: object, **_k: object) -> RealTaskRunResult:
        nonlocal run_calls
        run_calls += 1
        raise AssertionError("run_real_task_gate must not be invoked")

    monkeypatch.setattr("omnigent.factory.gate_a.real_chat.run_real_task_gate", _forbid_run)

    from omnigent.inner import factory_gate_a_real_harness

    app = factory_gate_a_real_harness.create_app()
    app.state.conversation_id = _CONVERSATION_ID

    status_events = await _post_operator_turn(app, f"status {_APPROVED_TASK_ID}")
    status_text = _combined_text_deltas(status_events)

    assert run_calls == 0
    assert status_events[-1].event == "response.completed"
    assert f"task_id: {_APPROVED_TASK_ID}" in status_text
    assert "receipt_ok: True" in status_text
    assert "do the thing" not in status_text

    foreign_status_events = await _post_operator_turn(app, f"status {_FOREIGN_TASK_ID}")
    foreign_status_text = _combined_text_deltas(foreign_status_events)

    assert run_calls == 0
    assert foreign_status_text == ""
    foreign_status_error = _failed_message(foreign_status_events)
    assert "receipt not found" in foreign_status_error
    assert "receipt_ok: True" not in foreign_status_text
    assert _APPROVED_TASK_ID not in foreign_status_error
    assert not any(e.event == "response.completed" for e in foreign_status_events)

    foreign_run_events = await _post_operator_turn(
        app,
        f"run approved task {_UNREGISTERED_TASK_ID}",
    )
    foreign_run_text = _combined_text_deltas(foreign_run_events)

    assert run_calls == 0
    assert foreign_run_text == ""
    foreign_run_error = _failed_message(foreign_run_events)
    assert "spec file not found" in foreign_run_error
    assert not any(e.event == "response.completed" for e in foreign_run_events)
