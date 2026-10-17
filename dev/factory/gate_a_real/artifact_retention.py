"""Retention for Gate A real-task artifact trees under a pinned artifacts root."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from dev.factory.gate_a_real.constants import (
    DEFAULT_ARTIFACTS_KEEP_HOURS,
    DEFAULT_ARTIFACTS_KEEP_LAST,
    KEEP_HOURS_ENV,
    KEEP_LAST_ENV,
    REAL_TASK_ARTIFACTS_ENV,
    REAL_TASK_ARTIFACTS_ROOT_ENV,
    REAL_TASK_RUN_PR_NUMBER_ENV,
    REAL_TASK_RUN_PR_STATE_ENV,
)
from dev.factory.gate_a_real.receipt import RECEIPT_SCHEMA, utc_now_iso

_logger = logging.getLogger(__name__)

RUN_METADATA_FILE = "run.json"
RUN_METADATA_SCHEMA = "omnigent.factory.gate_a_real.run/v1"
RUN_LOCK_FILE = "run.lock"
RECEIPT_FILE = "receipt.json"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class FactoryRunRecord:
    path: Path
    name: str
    sort_time: datetime
    in_progress: bool
    unmerged: bool


@dataclass(frozen=True)
class ArtifactRetentionConfig:
    keep_last: int
    keep_hours: float


@dataclass(frozen=True)
class PruneResult:
    kept: tuple[str, ...]
    deleted: tuple[str, ...]
    skipped: tuple[str, ...]


def retention_config_from_environ(environ: dict[str, str] | None = None) -> ArtifactRetentionConfig:
    env = environ if environ is not None else dict(os.environ)
    keep_last_raw = env.get(KEEP_LAST_ENV, "").strip()
    keep_hours_raw = env.get(KEEP_HOURS_ENV, "").strip()
    keep_last = DEFAULT_ARTIFACTS_KEEP_LAST if not keep_last_raw else int(keep_last_raw)
    keep_hours = DEFAULT_ARTIFACTS_KEEP_HOURS if not keep_hours_raw else float(keep_hours_raw)
    if keep_last < 0:
        raise ValueError(f"{KEEP_LAST_ENV} must be >= 0")
    if keep_hours < 0:
        raise ValueError(f"{KEEP_HOURS_ENV} must be >= 0")
    return ArtifactRetentionConfig(keep_last=keep_last, keep_hours=keep_hours)


def resolve_artifacts_root(
    *,
    environ: dict[str, str] | None = None,
    current_run_dir: Path | None = None,
) -> Path | None:
    """Return the artifacts root to prune, or None when pruning should be skipped."""
    env = environ if environ is not None else dict(os.environ)
    root_raw = env.get(REAL_TASK_ARTIFACTS_ROOT_ENV, "").strip()
    if root_raw:
        return _require_resolved_dir(root_raw)
    if current_run_dir is not None:
        return current_run_dir.resolve().parent
    artifacts_raw = env.get(REAL_TASK_ARTIFACTS_ENV, "").strip()
    if artifacts_raw:
        return _require_resolved_dir(artifacts_raw).parent
    return None


def begin_artifact_run(artifacts: Path, task_id: str) -> None:
    """Record run start metadata and an active lock for retention."""
    started_at = utc_now_iso()
    pid = os.getpid()
    payload: dict[str, Any] = {
        "schema": RUN_METADATA_SCHEMA,
        "task_id": task_id,
        "status": "in_progress",
        "started_at": started_at,
        "pid": pid,
    }
    _write_run_json(artifacts, payload)
    lock_path = artifacts / RUN_LOCK_FILE
    lock_path.write_text(f"{pid}\n", encoding="utf-8")


def finish_artifact_run(
    artifacts: Path,
    *,
    environ: dict[str, str] | None = None,
) -> None:
    """Finalize run.json and drop the active lock before retention pruning."""
    env = environ if environ is not None else dict(os.environ)
    lock_path = artifacts / RUN_LOCK_FILE
    with contextlib.suppress(OSError):
        lock_path.unlink()
    metadata = _run_metadata(artifacts) or {}
    receipt = _receipt_metadata(artifacts)
    status = "in_progress"
    completed_at: str | None = None
    if receipt is not None:
        completed_at = receipt.get("completed_at") if isinstance(receipt.get("completed_at"), str) else None
        status = "completed" if receipt.get("ok") is True else "failed"
    pr_number, pr_state, pr_merged = _pr_fields_from_environ(env)
    payload: dict[str, Any] = {
        "schema": RUN_METADATA_SCHEMA,
        "task_id": metadata.get("task_id") or (receipt or {}).get("task_id"),
        "status": status,
        "started_at": metadata.get("started_at") or utc_now_iso(),
        "completed_at": completed_at,
        "pid": metadata.get("pid"),
    }
    if pr_number is not None:
        payload["pr_number"] = pr_number
    if pr_state is not None:
        payload["pr_state"] = pr_state
    if pr_merged is not None:
        payload["pr_merged"] = pr_merged
    _write_run_json(artifacts, payload)


def _write_run_json(artifacts: Path, payload: dict[str, Any]) -> None:
    path = artifacts / RUN_METADATA_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _pr_fields_from_environ(
    env: dict[str, str],
) -> tuple[int | None, str | None, bool | None]:
    number_raw = env.get(REAL_TASK_RUN_PR_NUMBER_ENV, "").strip()
    state_raw = env.get(REAL_TASK_RUN_PR_STATE_ENV, "").strip()
    pr_number: int | None = None
    if number_raw:
        try:
            pr_number = int(number_raw)
        except ValueError:
            pr_number = None
    pr_state = state_raw or None
    pr_merged: bool | None = None
    if pr_state is not None:
        lowered = pr_state.lower()
        if lowered in {"merged", "closed"}:
            pr_merged = True
        elif lowered in {"open", "unmerged"}:
            pr_merged = False
    return pr_number, pr_state, pr_merged


def _require_resolved_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("artifacts root must be an absolute path")
    return path.resolve()


def _parse_iso_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _run_metadata(path: Path) -> dict[str, Any] | None:
    meta_path = path / RUN_METADATA_FILE
    if not meta_path.is_file() or meta_path.is_symlink():
        return None
    data = _load_json_object(meta_path)
    if data is None:
        return None
    schema = data.get("schema")
    if schema is not None and schema != RUN_METADATA_SCHEMA:
        return None
    return data


def _receipt_metadata(path: Path) -> dict[str, Any] | None:
    receipt_path = path / RECEIPT_FILE
    if not receipt_path.is_file() or receipt_path.is_symlink():
        return None
    data = _load_json_object(receipt_path)
    if data is None:
        return None
    schema = data.get("schema")
    if schema is not None and schema != RECEIPT_SCHEMA:
        return None
    return data


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def _read_lock_pid(run_dir: Path) -> int | None:
    lock_path = run_dir / RUN_LOCK_FILE
    if not lock_path.is_file() or lock_path.is_symlink():
        return None
    try:
        return int(lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _metadata_pid(metadata: dict[str, Any] | None) -> int | None:
    if metadata is None:
        return None
    pid = metadata.get("pid")
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str) and pid.strip().isdigit():
        return int(pid.strip())
    return None


def _has_active_lock_or_process(run_dir: Path, metadata: dict[str, Any] | None) -> bool:
    lock_pid = _read_lock_pid(run_dir)
    if lock_pid is not None and _pid_alive(lock_pid):
        return True
    meta_pid = _metadata_pid(metadata)
    return meta_pid is not None and _pid_alive(meta_pid)


def _run_age(run_dir: Path, metadata: dict[str, Any] | None, now: datetime) -> timedelta:
    for source in (metadata.get("started_at") if metadata else None,):
        if isinstance(source, str):
            parsed = _parse_iso_timestamp(source)
            if parsed is not None:
                return now - parsed
    try:
        mtime = datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        mtime = now
    return now - mtime


def _is_abandoned(
    run_dir: Path,
    metadata: dict[str, Any] | None,
    *,
    now: datetime,
    abandoned_after: timedelta,
) -> bool:
    receipt_path = run_dir / RECEIPT_FILE
    if receipt_path.is_file():
        return False
    if _has_active_lock_or_process(run_dir, metadata):
        return False
    return _run_age(run_dir, metadata, now) >= abandoned_after


def _run_in_progress(
    run_dir: Path,
    metadata: dict[str, Any] | None,
    *,
    now: datetime,
    abandoned_after: timedelta,
) -> bool:
    if _is_abandoned(run_dir, metadata, now=now, abandoned_after=abandoned_after):
        return False
    if metadata is not None:
        status = metadata.get("status")
        if isinstance(status, str) and status.strip().lower() in {
            "in_progress",
            "running",
            "in-progress",
        }:
            return True
        if metadata.get("in_progress") is True:
            return True
    receipt = _receipt_metadata(run_dir)
    if receipt is not None:
        completed_at = receipt.get("completed_at")
        if isinstance(completed_at, str) and completed_at.strip():
            return False
        return True
    if _has_active_lock_or_process(run_dir, metadata):
        return True
    resume = run_dir / "resume_state.json"
    builder_stdout = run_dir / "builder.stdout.txt"
    if resume.is_file() or builder_stdout.is_file():
        return True
    return False


def _run_unmerged(metadata: dict[str, Any] | None) -> bool:
    if metadata is None:
        return False
    if metadata.get("pr_merged") is False:
        return True
    if metadata.get("merged") is False:
        return True
    pr_state = metadata.get("pr_state")
    if isinstance(pr_state, str) and pr_state.strip().lower() in {"open", "unmerged"}:
        return True
    return False


def _run_sort_time(run_dir: Path, metadata: dict[str, Any] | None) -> datetime:
    for source in (
        metadata.get("completed_at") if metadata else None,
        metadata.get("started_at") if metadata else None,
    ):
        if isinstance(source, str):
            parsed = _parse_iso_timestamp(source)
            if parsed is not None:
                return parsed
    receipt = _receipt_metadata(run_dir)
    if receipt is not None:
        completed = receipt.get("completed_at")
        if isinstance(completed, str):
            parsed = _parse_iso_timestamp(completed)
            if parsed is not None:
                return parsed
    try:
        mtime = run_dir.stat().st_mtime
    except OSError:
        mtime = 0.0
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


def is_artifact_run_dir(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    if _TASK_ID_RE.fullmatch(path.name):
        return True
    receipt = path / RECEIPT_FILE
    if receipt.is_file() and not receipt.is_symlink():
        data = _load_json_object(receipt)
        if data is not None and data.get("schema") == RECEIPT_SCHEMA:
            return True
    return False


def is_factory_run_dir(path: Path) -> bool:
    """Backward-compatible alias for :func:`is_artifact_run_dir`."""
    return is_artifact_run_dir(path)


def list_factory_run_dirs(
    artifacts_root: Path,
    *,
    now: datetime | None = None,
    config: ArtifactRetentionConfig | None = None,
) -> list[FactoryRunRecord]:
    root = artifacts_root.resolve()
    if not root.is_dir():
        return []
    instant = now if now is not None else datetime.now(timezone.utc)
    cfg = config if config is not None else retention_config_from_environ()
    abandoned_after = timedelta(hours=cfg.keep_hours)
    records: list[FactoryRunRecord] = []
    for entry in root.iterdir():
        if not is_artifact_run_dir(entry):
            continue
        try:
            resolved = _assert_child_of_root(entry, root)
        except ValueError:
            continue
        metadata = _run_metadata(resolved)
        records.append(
            FactoryRunRecord(
                path=resolved,
                name=resolved.name,
                sort_time=_run_sort_time(resolved, metadata),
                in_progress=_run_in_progress(
                    resolved,
                    metadata,
                    now=instant,
                    abandoned_after=abandoned_after,
                ),
                unmerged=_run_unmerged(metadata),
            )
        )
    records.sort(key=lambda item: item.sort_time, reverse=True)
    return records


def select_runs_to_delete(
    records: list[FactoryRunRecord],
    *,
    config: ArtifactRetentionConfig,
    now: datetime,
    protect_paths: frozenset[Path] = frozenset(),
) -> tuple[list[FactoryRunRecord], list[FactoryRunRecord]]:
    """Return (to_keep, to_delete) for factory run records under a root."""
    if not records:
        return [], []
    keep_paths: set[Path] = set()
    min_age = timedelta(hours=config.keep_hours)
    newest = records[: max(config.keep_last, 0)]
    for record in records:
        if record.path in protect_paths:
            keep_paths.add(record.path)
            continue
        if record.in_progress or record.unmerged:
            keep_paths.add(record.path)
            continue
        if now - record.sort_time < min_age:
            keep_paths.add(record.path)
            continue
    for record in newest:
        keep_paths.add(record.path)
    to_keep = [record for record in records if record.path in keep_paths]
    to_delete = [record for record in records if record.path not in keep_paths]
    return to_keep, to_delete


def _assert_child_of_root(path: Path, root: Path) -> Path:
    root_resolved = root.resolve()
    resolved = path.resolve()
    if resolved == root_resolved:
        raise ValueError("run path must not equal artifacts root")
    if resolved.is_symlink():
        raise ValueError("run path must not be a symlink")
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError("run path escapes artifacts root") from None
    cursor = root_resolved
    for part in resolved.relative_to(root_resolved).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("symlink along run path")
    return resolved


def _safe_remove_run_dir(run_dir: Path, artifacts_root: Path) -> Literal["deleted", "skipped"]:
    try:
        resolved = _assert_child_of_root(run_dir, artifacts_root)
    except ValueError as exc:
        _logger.warning("skip delete %s: %s", run_dir, exc)
        return "skipped"
    if not resolved.is_dir():
        return "skipped"
    shutil.rmtree(resolved)
    return "deleted"


def prune_artifacts_root(
    artifacts_root: Path,
    *,
    config: ArtifactRetentionConfig | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    protect_paths: frozenset[Path] = frozenset(),
) -> PruneResult:
    cfg = config if config is not None else retention_config_from_environ()
    instant = now if now is not None else datetime.now(timezone.utc)
    root = artifacts_root.resolve()
    records = list_factory_run_dirs(root, now=instant, config=cfg)
    _keep, to_delete = select_runs_to_delete(
        records,
        config=cfg,
        now=instant,
        protect_paths=protect_paths,
    )
    kept_names = tuple(record.name for record in records if record not in to_delete)
    deleted: list[str] = []
    skipped: list[str] = []
    for record in to_delete:
        if dry_run:
            _logger.info("dry-run: would delete factory run artifacts %s", record.path)
            deleted.append(record.name)
            continue
        outcome = _safe_remove_run_dir(record.path, root)
        if outcome == "deleted":
            _logger.info("deleted factory run artifacts %s", record.path)
            deleted.append(record.name)
        else:
            skipped.append(record.name)
    return PruneResult(kept=kept_names, deleted=tuple(deleted), skipped=tuple(skipped))


def prune_after_real_task_run(
    current_run_dir: Path,
    *,
    environ: dict[str, str] | None = None,
) -> PruneResult | None:
    """Best-effort retention sweep; never raises to callers."""
    try:
        root = resolve_artifacts_root(environ=environ, current_run_dir=current_run_dir)
        if root is None:
            return None
        protect = frozenset({current_run_dir.resolve()})
        return prune_artifacts_root(root, protect_paths=protect)
    except Exception:
        _logger.exception("artifact retention prune failed (run continues)")
        return None
