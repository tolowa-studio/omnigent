"""V12 narrow Cursor project-dir file contract for Gate A adapter trials."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_BASE64_DIGEST_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-",
)

MCP_APPROVALS_BASENAME = "mcp-approvals.json"
REPO_JSON_BASENAME = "repo.json"

BASELINE_KEY_PROJECT_SLUG = "gate_a.project_files.slug"
BASELINE_KEY_MCP_APPROVALS_SHA256 = "gate_a.project_files.mcp_approvals_sha256"
BASELINE_KEY_APPROVAL_DIGEST_SHA256 = "gate_a.project_files.approval_digest_sha256"


def _cursor_cli_canonical_project_slug(workspace: Path) -> str:
    """
    Project-state directory key used by Cursor CLI 2026.10.01+ for *workspace*.

    Unlike ``cursor_project_key`` (slash-only), the CLI replaces every
    non-alphanumeric character—including underscores—with hyphens.
    """
    text = str(workspace.resolve()).strip("/")
    if not text:
        return "root"
    return "".join(ch if ch.isalnum() else "-" for ch in text)


def expected_cursor_project_slug(workspace: Path) -> str:
    return _cursor_cli_canonical_project_slug(workspace)


def project_mcp_approvals_relpath(slug: str) -> str:
    return f".cursor/projects/{slug}/{MCP_APPROVALS_BASENAME}"


def project_repo_json_relpath(slug: str) -> str:
    return f".cursor/projects/{slug}/{REPO_JSON_BASENAME}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_44_char_base64_digest(value: str) -> bool:
    if len(value) != 44:
        return False
    return all(ch in _BASE64_DIGEST_CHARS for ch in value)


def parse_mcp_approvals_file(path: Path) -> tuple[str | None, str | None, list[str]]:
    """
    Validate ``mcp-approvals.json`` shape.

    Returns ``(file_sha256, digest_sha256, problems)`` without exposing the digest value.
    """
    problems: list[str] = []
    if path.is_symlink():
        return None, None, ["mcp-approvals.json must not be a symlink"]
    if not path.is_file():
        return None, None, ["mcp-approvals.json missing"]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, None, [f"mcp-approvals.json unreadable: {exc}"]
    file_sha = _sha256_bytes(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return file_sha, None, ["mcp-approvals.json is not valid JSON"]
    if not isinstance(payload, list):
        return file_sha, None, ["mcp-approvals.json must be a JSON list"]
    if len(payload) != 1:
        return file_sha, None, ["mcp-approvals.json must contain exactly one entry"]
    entry = payload[0]
    if not isinstance(entry, str):
        return file_sha, None, ["mcp-approvals.json entry must be a string"]
    if not _is_44_char_base64_digest(entry):
        return file_sha, None, ["mcp-approvals.json entry must be a 44-character base64 digest"]
    return file_sha, _sha256_text(entry), problems


def parse_repo_json_file(path: Path) -> tuple[str | None, list[str]]:
    """Validate ``repo.json`` one-key UUID shape; returns ``(file_sha256, problems)``."""
    if path.is_symlink():
        return None, ["repo.json must not be a symlink"]
    if not path.is_file():
        return None, ["repo.json missing"]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, [f"repo.json unreadable: {exc}"]
    file_sha = _sha256_bytes(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return file_sha, ["repo.json is not valid JSON"]
    if not isinstance(payload, dict):
        return file_sha, ["repo.json must be a JSON object"]
    keys = list(payload.keys())
    if keys != ["id"]:
        return file_sha, ["repo.json must contain exactly the key id"]
    repo_id = payload["id"]
    if not isinstance(repo_id, str):
        return file_sha, ["repo.json id must be a string"]
    if len(repo_id) != 36 or not _UUID_RE.fullmatch(repo_id):
        return file_sha, ["repo.json id must be a UUID-shaped string"]
    return file_sha, []


@dataclass(frozen=True)
class GateAProjectFilesBaseline:
    project_slug: str
    mcp_approvals_file_sha256: str
    approval_digest_sha256: str

    def as_fingerprint_map(self) -> dict[str, str]:
        return {
            BASELINE_KEY_PROJECT_SLUG: self.project_slug,
            BASELINE_KEY_MCP_APPROVALS_SHA256: self.mcp_approvals_file_sha256,
            BASELINE_KEY_APPROVAL_DIGEST_SHA256: self.approval_digest_sha256,
        }

    @classmethod
    def from_fingerprint_map(cls, pins: dict[str, str]) -> GateAProjectFilesBaseline | None:
        slug = pins.get(BASELINE_KEY_PROJECT_SLUG)
        file_sha = pins.get(BASELINE_KEY_MCP_APPROVALS_SHA256)
        digest_sha = pins.get(BASELINE_KEY_APPROVAL_DIGEST_SHA256)
        if not slug or not file_sha or not digest_sha:
            return None
        return cls(
            project_slug=slug,
            mcp_approvals_file_sha256=file_sha,
            approval_digest_sha256=digest_sha,
        )


def _project_dir(home_dir: Path, slug: str) -> Path:
    return home_dir / ".cursor" / "projects" / slug


def _list_project_slug_dirs(home_dir: Path) -> list[str]:
    projects_root = home_dir / ".cursor" / "projects"
    if not projects_root.is_dir():
        return []
    slugs: list[str] = []
    for child in sorted(projects_root.iterdir()):
        if child.is_symlink():
            slugs.append(child.name)
            continue
        if child.is_dir():
            slugs.append(child.name)
    return slugs


def _find_extra_mcp_approvals(home_dir: Path, *, allowed_relpath: str) -> list[str]:
    cursor_home = home_dir / ".cursor"
    if not cursor_home.is_dir():
        return []
    problems: list[str] = []
    for path in sorted(cursor_home.rglob(MCP_APPROVALS_BASENAME)):
        if path.is_symlink():
            problems.append(f"forbidden symlink mcp-approvals path: {path.relative_to(home_dir).as_posix()}")
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(home_dir).as_posix()
        if rel != allowed_relpath:
            problems.append(f"unexpected mcp-approvals path: {rel}")
    return problems


def _reject_symlink_project_tree(project_dir: Path) -> list[str]:
    if project_dir.is_symlink():
        return ["cursor project directory must not be a symlink"]
    if not project_dir.exists():
        return []
    problems: list[str] = []
    for path in project_dir.rglob("*"):
        if path.is_symlink():
            problems.append(
                f"symlink under cursor project directory: {path.relative_to(project_dir.parent.parent).as_posix()}",
            )
    return problems


def establish_project_files_baseline_after_warmup(
    home_dir: Path,
    trial_workspace: Path,
) -> tuple[GateAProjectFilesBaseline | None, list[str]]:
    """Pin project ``mcp-approvals.json`` after sandboxed no-tool warmup."""
    slug = expected_cursor_project_slug(trial_workspace)
    slugs = _list_project_slug_dirs(home_dir)
    if slugs != [slug]:
        return None, [f"expected exactly one cursor project directory {slug}, found {slugs!r}"]

    project_dir = _project_dir(home_dir, slug)
    problems = _reject_symlink_project_tree(project_dir)
    allowed_rel = project_mcp_approvals_relpath(slug)
    problems.extend(_find_extra_mcp_approvals(home_dir, allowed_relpath=allowed_rel))

    approvals_path = project_dir / MCP_APPROVALS_BASENAME
    file_sha, digest_sha, parse_problems = parse_mcp_approvals_file(approvals_path)
    problems.extend(parse_problems)
    if file_sha is None or digest_sha is None:
        return None, problems

    repo_path = project_dir / REPO_JSON_BASENAME
    if repo_path.exists() or repo_path.is_symlink():
        problems.append("repo.json must not exist after no-tool warmup")

    if problems:
        return None, problems

    return (
        GateAProjectFilesBaseline(
            project_slug=slug,
            mcp_approvals_file_sha256=file_sha,
            approval_digest_sha256=digest_sha,
        ),
        [],
    )


def validate_project_files_after_positive(
    home_dir: Path,
    trial_workspace: Path,
    baseline: GateAProjectFilesBaseline,
) -> list[str]:
    """Re-check approvals pin and require valid ``repo.json`` in the same project dir."""
    expected_slug = expected_cursor_project_slug(trial_workspace)
    if baseline.project_slug != expected_slug:
        return ["project files baseline slug does not match trial workspace"]

    slugs = _list_project_slug_dirs(home_dir)
    if slugs != [expected_slug]:
        return [f"expected exactly one cursor project directory {expected_slug}, found {slugs!r}"]

    project_dir = _project_dir(home_dir, expected_slug)
    problems = _reject_symlink_project_tree(project_dir)
    allowed_rel = project_mcp_approvals_relpath(expected_slug)
    problems.extend(_find_extra_mcp_approvals(home_dir, allowed_relpath=allowed_rel))

    approvals_path = project_dir / MCP_APPROVALS_BASENAME
    file_sha, digest_sha, parse_problems = parse_mcp_approvals_file(approvals_path)
    problems.extend(parse_problems)
    if file_sha is None or digest_sha is None:
        return problems
    if file_sha != baseline.mcp_approvals_file_sha256:
        problems.append("mcp-approvals.json file hash changed after positive turn")
    if digest_sha != baseline.approval_digest_sha256:
        problems.append("mcp-approvals.json digest changed after positive turn")

    repo_path = project_dir / REPO_JSON_BASENAME
    _, repo_problems = parse_repo_json_file(repo_path)
    problems.extend(repo_problems)

    return problems
