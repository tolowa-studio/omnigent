"""Durable real-task Gate A receipt (truthful, local-only)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev.factory.gate_a_real.spec import RealTaskSpec


@dataclass
class RealTaskReceipt:
    ok: bool
    task_id: str
    spec_sha256: str
    workspace: str
    problems: list[str]
    builder_session_ids: list[str]
    review_session_ids: list[str]
    builder_exit_code: int | None
    review_exit_code: int | None
    verify_exit_code: int | None
    deliverable_manifest_sha256: str | None
    post_review_manifest_sha256: str | None
    freeze_manifest_path: str | None
    review_pass: bool
    completed_at: str
    builder_log_path: str | None = None
    review_log_path: str | None = None
    verify_log_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "omnigent.factory.gate_a_real.receipt/v1",
            "ok": self.ok,
            "task_id": self.task_id,
            "spec_sha256": self.spec_sha256,
            "workspace": self.workspace,
            "problems": self.problems,
            "builder_session_ids": self.builder_session_ids,
            "review_session_ids": self.review_session_ids,
            "builder_exit_code": self.builder_exit_code,
            "review_exit_code": self.review_exit_code,
            "verify_exit_code": self.verify_exit_code,
            "deliverable_manifest_sha256": self.deliverable_manifest_sha256,
            "post_review_manifest_sha256": self.post_review_manifest_sha256,
            "freeze_manifest_path": self.freeze_manifest_path,
            "review_pass": self.review_pass,
            "completed_at": self.completed_at,
            "builder_log_path": self.builder_log_path,
            "review_log_path": self.review_log_path,
            "verify_log_path": self.verify_log_path,
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def load_resume_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_resume_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_review_only_resume(
    spec: RealTaskSpec,
    artifacts: Path,
    prior: dict[str, Any],
) -> list[str]:
    """Bind review-only to the exact prior successful builder + freeze candidate."""
    problems: list[str] = []
    if not prior.get("builder_ok"):
        problems.append("review-only requires resume_state builder_ok=true")
    if prior.get("spec_sha256") != spec.spec_sha256:
        problems.append("resume_state spec_sha256 does not match bound spec")
    if prior.get("workspace") != str(spec.workspace):
        problems.append("resume_state workspace does not match bound spec")
    builder_exit = prior.get("builder_exit_code")
    if builder_exit != 0:
        problems.append(f"resume_state builder_exit_code must be 0, got {builder_exit!r}")
    if not prior.get("builder_session_ids"):
        problems.append("resume_state missing builder_session_ids")
    if not prior.get("deliverable_manifest_sha256"):
        problems.append("resume_state missing deliverable_manifest_sha256")
    freeze_manifest = artifacts / "freeze" / "manifest.json"
    if not freeze_manifest.is_file():
        problems.append("review-only requires existing freeze/manifest.json in artifacts dir")
    elif not prior.get("freeze_manifest_sha256"):
        problems.append("resume_state missing freeze_manifest_sha256")
    else:
        try:
            raw_manifest = freeze_manifest.read_bytes()
            digest = hashlib.sha256(raw_manifest).hexdigest()
            if digest != prior["freeze_manifest_sha256"]:
                problems.append("freeze manifest sha256 drift vs resume_state")
            entries = json.loads(raw_manifest)
            if not isinstance(entries, list):
                raise ValueError("freeze manifest must be a list")
            for entry in entries:
                rel = entry.get("relpath") if isinstance(entry, dict) else None
                expected = entry.get("sha256") if isinstance(entry, dict) else None
                if not isinstance(rel, str) or not isinstance(expected, str):
                    raise ValueError("freeze manifest contains an invalid record")
                frozen = artifacts / "freeze" / "files" / rel
                if (
                    not frozen.is_file()
                    or hashlib.sha256(frozen.read_bytes()).hexdigest() != expected
                ):
                    problems.append(f"frozen file hash drift: {rel}")
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f"freeze manifest unreadable: {exc}")
    review_profile_path = artifacts / "review.profile.json"
    if not review_profile_path.is_file():
        problems.append("review-only requires review.profile.json with pinned review hashes")
    else:
        try:
            recorded_review = json.loads(review_profile_path.read_text(encoding="utf-8"))
            if not isinstance(recorded_review, dict):
                raise ValueError("review.profile.json must be an object")
            for key, expected in spec.review_config_hashes.items():
                actual = recorded_review.get(key) if isinstance(recorded_review, dict) else None
                if not isinstance(actual, str) or actual.lower() != expected.lower():
                    problems.append(f"recorded review config hash drift for {key}")
            for key in recorded_review:
                if key not in spec.review_config_hashes:
                    problems.append(f"unexpected recorded review config fingerprint: {key}")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            problems.append(f"review.profile.json unreadable: {exc}")

    builder_meta = artifacts / "builder.meta.json"
    if not builder_meta.is_file():
        problems.append("review-only requires builder.meta.json in artifacts dir")
    else:
        expected_stdout = prior.get("builder_stdout_sha256")
        if not expected_stdout:
            problems.append("resume_state missing builder_stdout_sha256")
        else:
            try:
                meta = json.loads(builder_meta.read_text(encoding="utf-8"))
                actual = meta.get("stdout_sha256") if isinstance(meta, dict) else None
                if actual != expected_stdout:
                    problems.append("builder stdout log hash drift vs resume_state")
                builder_stdout = artifacts / "builder.stdout.txt"
                if hashlib.sha256(builder_stdout.read_bytes()).hexdigest() != expected_stdout:
                    problems.append("builder stdout content hash drift vs resume_state")
            except (OSError, json.JSONDecodeError):
                problems.append("builder metadata or stdout log is unreadable")
    return problems


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
