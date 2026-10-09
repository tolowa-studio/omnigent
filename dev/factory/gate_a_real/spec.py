"""Load and bind one explicit JSON real-task spec (workspace, expiry, hash)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RealTaskSpecError(ValueError):
    """Spec rejected before any Cursor CLI subprocess is spawned."""


@dataclass(frozen=True)
class RealTaskSpec:
    task_id: str
    workspace: Path
    expires_at: datetime
    spec_sha256: str
    prompt: str
    deliverable_paths: tuple[str, ...]
    verify_command: tuple[str, ...]
    config_hashes: dict[str, str]
    review_config_hashes: dict[str, str]
    raw: dict[str, Any]


def _parse_expires(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RealTaskSpecError(f"expires_at is not valid ISO-8601: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def canonical_spec_sha256(document: dict[str, Any]) -> str:
    payload = {k: v for k, v in document.items() if k != "spec_sha256"}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def load_real_task_spec(path: Path) -> RealTaskSpec:
    if not path.is_file():
        raise RealTaskSpecError(f"spec file not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RealTaskSpecError(f"spec is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RealTaskSpecError("spec root must be a JSON object")

    task_id = document.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RealTaskSpecError("task_id is required")
    task_id = task_id.strip()

    workspace_raw = document.get("workspace")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        raise RealTaskSpecError("workspace is required")
    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_absolute():
        raise RealTaskSpecError("workspace must be an absolute path")
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise RealTaskSpecError(f"workspace is not a directory: {workspace}")

    expires_raw = document.get("expires_at")
    if not isinstance(expires_raw, str) or not expires_raw.strip():
        raise RealTaskSpecError("expires_at is required")
    expires_at = _parse_expires(expires_raw.strip())

    bound_hash = document.get("spec_sha256")
    if not isinstance(bound_hash, str) or len(bound_hash) != 64:
        raise RealTaskSpecError("spec_sha256 must be a 64-char hex digest")
    computed = canonical_spec_sha256(document)
    if bound_hash.lower() != computed:
        raise RealTaskSpecError("spec_sha256 does not match canonical spec body")

    prompt = document.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RealTaskSpecError("prompt is required")

    deliverables = document.get("deliverable_paths")
    if not isinstance(deliverables, list) or not deliverables:
        raise RealTaskSpecError("deliverable_paths must be a non-empty list of strings")
    rel_paths: list[str] = []
    for item in deliverables:
        if not isinstance(item, str) or not item.strip():
            raise RealTaskSpecError("deliverable_paths entries must be non-empty strings")
        raw = item.strip()
        if Path(raw).is_absolute():
            raise RealTaskSpecError(f"deliverable path must be workspace-relative: {item}")
        rel = raw.lstrip("/")
        if not rel or rel.startswith("..") or ".." in Path(rel).parts:
            raise RealTaskSpecError(f"deliverable path must stay inside workspace: {item}")
        rel_paths.append(rel)

    verify = document.get("verify_command")
    if not isinstance(verify, list) or not verify:
        raise RealTaskSpecError("verify_command must be a non-empty argv list")
    verify_argv: list[str] = []
    for part in verify:
        if not isinstance(part, str):
            raise RealTaskSpecError("verify_command entries must be strings")
        verify_argv.append(part)

    config_hashes = document.get("config_hashes")
    if not isinstance(config_hashes, dict) or not config_hashes:
        raise RealTaskSpecError("config_hashes is required")
    hashes: dict[str, str] = {}
    for key, value in config_hashes.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) != 64:
            raise RealTaskSpecError("config_hashes values must be 64-char hex digests")
        hashes[key] = value.lower()

    review_config_hashes = document.get("review_config_hashes")
    if not isinstance(review_config_hashes, dict) or not review_config_hashes:
        raise RealTaskSpecError("review_config_hashes is required")
    review_hashes: dict[str, str] = {}
    for key, value in review_config_hashes.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) != 64:
            raise RealTaskSpecError("review_config_hashes values must be 64-char hex digests")
        review_hashes[key] = value.lower()

    return RealTaskSpec(
        task_id=task_id,
        workspace=workspace,
        expires_at=expires_at,
        spec_sha256=bound_hash.lower(),
        prompt=prompt,
        deliverable_paths=tuple(rel_paths),
        verify_command=tuple(verify_argv),
        config_hashes=hashes,
        review_config_hashes=review_hashes,
        raw=document,
    )


def validate_not_expired(spec: RealTaskSpec, *, now: datetime | None = None) -> None:
    moment = now or datetime.now(timezone.utc)
    if moment >= spec.expires_at:
        raise RealTaskSpecError("spec expired")
