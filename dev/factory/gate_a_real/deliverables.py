"""Deliverable inventory (tracked + untracked) and freeze manifests."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeliverableRecord:
    relpath: str
    sha256: str
    size_bytes: int
    git_tracked: bool


@dataclass(frozen=True)
class DeliverableInventory:
    records: tuple[DeliverableRecord, ...]
    manifest_sha256: str


def _git_tracked_paths(workspace: Path) -> set[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "ls-files", "-z"],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    raw = proc.stdout
    if not raw:
        return set()
    return {part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part}


def _resolve_deliverable_path(workspace: Path, rel: str) -> Path:
    workspace_resolved = workspace.resolve()
    candidate = workspace / rel
    if candidate.is_symlink():
        raise ValueError(f"deliverable must not be a symlink: {rel}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace_resolved)
    except ValueError:
        raise ValueError(f"deliverable path escapes workspace: {rel}") from None
    return resolved


def collect_deliverables(workspace: Path, relpaths: tuple[str, ...]) -> DeliverableInventory:
    tracked = _git_tracked_paths(workspace)
    records: list[DeliverableRecord] = []
    for rel in relpaths:
        path = _resolve_deliverable_path(workspace, rel)
        if not path.is_file():
            raise FileNotFoundError(f"deliverable missing: {rel}")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        records.append(
            DeliverableRecord(
                relpath=rel,
                sha256=digest,
                size_bytes=len(data),
                git_tracked=rel in tracked,
            )
        )
    manifest_body = json.dumps(
        [
            {
                "relpath": r.relpath,
                "sha256": r.sha256,
                "size_bytes": r.size_bytes,
                "git_tracked": r.git_tracked,
            }
            for r in records
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
    return DeliverableInventory(records=tuple(records), manifest_sha256=manifest_sha256)


def inventories_match(left: DeliverableInventory, right: DeliverableInventory) -> bool:
    return left.manifest_sha256 == right.manifest_sha256


def frozen_files_match(inventory: DeliverableInventory, freeze_dir: Path) -> bool:
    for record in inventory.records:
        frozen = freeze_dir / "files" / record.relpath
        if not frozen.is_file():
            return False
        if hashlib.sha256(frozen.read_bytes()).hexdigest() != record.sha256:
            return False
    return True


def write_freeze_candidate(
    inventory: DeliverableInventory,
    workspace: Path,
    freeze_dir: Path,
) -> Path:
    if freeze_dir.exists():
        shutil.rmtree(freeze_dir)
    freeze_dir.mkdir(parents=True)
    manifest_path = freeze_dir / "manifest.json"
    manifest_path.write_bytes(
        json.dumps(
            [
                {
                    "relpath": r.relpath,
                    "sha256": r.sha256,
                    "size_bytes": r.size_bytes,
                    "git_tracked": r.git_tracked,
                }
                for r in inventory.records
            ],
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    for record in inventory.records:
        src = workspace / record.relpath
        dest = freeze_dir / "files" / record.relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if hashlib.sha256(dest.read_bytes()).hexdigest() != record.sha256:
            raise ValueError(f"frozen file hash differs from inventory: {record.relpath}")
    return manifest_path
