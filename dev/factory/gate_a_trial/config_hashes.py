"""SHA-256 fingerprints for disposable Gate A Cursor CLI config artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from dev.factory.gate_a_trial.project_files import (
    MCP_APPROVALS_BASENAME,
    REPO_JSON_BASENAME,
    expected_cursor_project_slug,
    project_mcp_approvals_relpath,
    project_repo_json_relpath,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Top-level ``cli-config.json`` keys the harness materializes (operator-owned).
CLI_CONFIG_MATERIALIZED_TOP_LEVEL: frozenset[str] = frozenset({"version", "editor"})

# Security policy pins — must not drift across discovery, warmup, or positive CLI.
CLI_CONFIG_POLICY_TOP_LEVEL: frozenset[str] = frozenset({"approvalMode", "permissions"})

# Cursor-documented session/cache fields (may change without weakening the gate).
CLI_CONFIG_RUNTIME_CACHE_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "authInfo",
        "autoAcceptWebSearch",
        "display",
        "exploreSubagentModel",
        "hasChangedDefaultModel",
        "hints",
        "maxMode",
        "model",
        "modelParameters",
        "modelSelectionHistory",
        "modelSlashCommands",
        "network",
        "notifications",
        "privacyCache",
        "selectedModel",
    }
)

# Active steering toggles — must match the post-warmup baseline exactly.
CLI_CONFIG_STEERING_TOP_LEVEL: frozenset[str] = frozenset({"rewind", "steering"})

CLI_CONFIG_ACCEPTED_TOP_LEVEL: frozenset[str] = (
    CLI_CONFIG_MATERIALIZED_TOP_LEVEL
    | CLI_CONFIG_POLICY_TOP_LEVEL
    | CLI_CONFIG_RUNTIME_CACHE_TOP_LEVEL
    | CLI_CONFIG_STEERING_TOP_LEVEL
)

CLI_CONFIG_RUNTIME_ADDED_KEY_CATEGORIES: dict[str, str] = {
    "materialized": "Harness-written baseline fields (version, editor).",
    "policy": "Operator pins (approvalMode, permissions); hashed for drift detection.",
    "runtime_cache": "Cursor-managed session cache (model, authInfo, privacyCache, …).",
    "steering": (
        "Active steering toggles (steering, rewind); absent→both true allowed once after "
        "no-tool warmup; must not change after warmup baseline is pinned."
    ),
}

# Fingerprints the adapter pins after materialization (includes dynamic workspace MCP URL).
MANDATORY_EFFECTIVE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "cli-config.json",
        "mcp.json",
        "workspace/.cursor/mcp.json",
    }
)

# Never fingerprint secret payloads; record presence only.
_REDACTED_PRESENT = "redacted:present"
_SECRET_BASENAMES = frozenset(
    {
        "credentials.json",
        "auth.json",
        "secrets.json",
    }
)
_SKIP_INVENTORY_BASENAMES = frozenset(
    {
        "statsig-cache.json",
    }
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def effective_config_hashes(
    *,
    cursor_config_dir: Path,
    workspace_mcp_config: Path | None,
) -> dict[str, str]:
    """
    Stable hashes for the trial profile the harness materializes.

    ``cli-config.json`` is hashed from parsed JSON (sorted keys) so incidental
    key order does not change the fingerprint.
    """
    out: dict[str, str] = {}
    cli_path = cursor_config_dir / "cli-config.json"
    if cli_path.is_file():
        out["cli-config.json"] = cli_config_security_policy_fingerprint(cli_path)
    global_mcp = cursor_config_dir / "mcp.json"
    if global_mcp.is_file():
        out["mcp.json"] = sha256_file(global_mcp)
    if workspace_mcp_config is not None and workspace_mcp_config.is_file():
        rel = "workspace/.cursor/mcp.json"
        out[rel] = sha256_file(workspace_mcp_config)
    return out


def pin_mandatory_fingerprints(effective_hashes: dict[str, str]) -> dict[str, str]:
    """Extract mandatory labels; raise when materialization omitted a required artifact."""
    missing = sorted(k for k in MANDATORY_EFFECTIVE_CONFIG_KEYS if k not in effective_hashes)
    if missing:
        raise ValueError(f"mandatory config fingerprints missing after materialization: {missing}")
    return {key: effective_hashes[key] for key in sorted(MANDATORY_EFFECTIVE_CONFIG_KEYS)}


def _read_cli_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"cli-config.json must be an object: {path}")
    return payload


def cli_config_security_policy_fingerprint(cli_config_path: Path) -> str:
    """Canonical hash of ``approvalMode`` and ``permissions`` only."""
    payload = _read_cli_config(cli_config_path)
    policy = {
        "approvalMode": payload.get("approvalMode"),
        "permissions": payload.get("permissions"),
    }
    return _sha256_bytes(_canonical_json_bytes(policy))


def cli_config_steering_fingerprint(cli_config_path: Path) -> str:
    payload = _read_cli_config(cli_config_path)
    steering = _cli_config_steering_slice(payload)
    return _sha256_bytes(_canonical_json_bytes(steering))


def _cli_config_steering_slice(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in sorted(CLI_CONFIG_STEERING_TOP_LEVEL) if key in payload}


def cli_config_steering_absent_fingerprint() -> str:
    """Fingerprint when ``steering`` and ``rewind`` are both absent (pre-warmup materialization)."""
    return _sha256_bytes(_canonical_json_bytes({}))


def steering_baseline_was_absent(steering_fingerprint_sha: str) -> bool:
    return steering_fingerprint_sha == cli_config_steering_absent_fingerprint()


def is_permitted_cursor_steering_warmup_init(cli_config_path: Path) -> bool:
    """
    True only when Cursor adds both toggles as boolean ``true`` and nothing else steering-related.
    """
    if not cli_config_path.is_file():
        return False
    payload = _read_cli_config(cli_config_path)
    present = {key for key in CLI_CONFIG_STEERING_TOP_LEVEL if key in payload}
    if present != CLI_CONFIG_STEERING_TOP_LEVEL:
        return False
    for key in CLI_CONFIG_STEERING_TOP_LEVEL:
        if type(payload[key]) is not bool:
            return False
    return payload.get("steering") is True and payload.get("rewind") is True


def cli_config_top_level_key_fingerprint(cli_config_path: Path) -> str:
    payload = _read_cli_config(cli_config_path)
    return _sha256_bytes(_canonical_json_bytes(sorted(payload.keys())))


def compare_cli_config_steering(
    baseline_steering_sha: str,
    cli_config_path: Path,
) -> list[str]:
    if not cli_config_path.is_file():
        return ["cli-config.json missing for steering check"]
    observed = cli_config_steering_fingerprint(cli_config_path)
    if observed != baseline_steering_sha:
        return ["active steering changed in cli-config.json"]
    return []


def compare_cli_config_steering_after_warmup(
    baseline_steering_sha: str,
    cli_config_path: Path,
) -> list[str]:
    """
    Post no-tool warmup: pin policy/MCP from discovery; allow absent→both-true init only.
    """
    if not cli_config_path.is_file():
        return ["cli-config.json missing for steering check"]
    observed = cli_config_steering_fingerprint(cli_config_path)
    if observed == baseline_steering_sha:
        return []
    if steering_baseline_was_absent(baseline_steering_sha) and is_permitted_cursor_steering_warmup_init(
        cli_config_path,
    ):
        return []
    return ["active steering changed in cli-config.json"]


def unknown_cli_config_top_level_key_problems(cli_config_path: Path) -> list[str]:
    if not cli_config_path.is_file():
        return ["cli-config.json missing for top-level key check"]
    payload = _read_cli_config(cli_config_path)
    unknown = sorted(key for key in payload if key not in CLI_CONFIG_ACCEPTED_TOP_LEVEL)
    if unknown:
        return [f"cli-config.json unexpected top-level keys: {unknown}"]
    return []


def compare_cli_config_top_level_keys(
    baseline_keys_sha: str,
    cli_config_path: Path,
) -> list[str]:
    if not cli_config_path.is_file():
        return ["cli-config.json missing for top-level key check"]
    problems = unknown_cli_config_top_level_key_problems(cli_config_path)
    observed = cli_config_top_level_key_fingerprint(cli_config_path)
    if observed != baseline_keys_sha:
        problems.append("cli-config.json top-level key set changed")
    return problems


def security_config_fingerprints(
    *,
    cursor_config_dir: Path,
    workspace_mcp_config: Path | None,
) -> dict[str, str]:
    """Mandatory pins plus policy/steering slices for post-warmup baselines."""
    out = effective_config_hashes(
        cursor_config_dir=cursor_config_dir,
        workspace_mcp_config=workspace_mcp_config,
    )
    cli_path = cursor_config_dir / "cli-config.json"
    if cli_path.is_file():
        out["cli-config.json#steering"] = cli_config_steering_fingerprint(cli_path)
        out["cli-config.json#top_level_keys"] = cli_config_top_level_key_fingerprint(cli_path)
    return out


def is_allowed_home_cursor_session_relpath(
    rel_posix: str,
    *,
    expected_project_slug: str | None = None,
    allow_repo_json: bool = False,
) -> bool:
    """
    Allowed ``HOME`` session artifacts after sandboxed no-tool warmup.

    Session files must live under the adapter workspace project slug when *expected_project_slug*
    is set. Project-scoped ``mcp-approvals.json`` and (after positive) ``repo.json`` are allowed
    only for that slug.
    """
    if rel_posix == ".cursor/agent-cli-state.json":
        return True
    parts = rel_posix.split("/")
    if len(parts) >= 3 and parts[0] == ".cursor" and parts[1] == "projects":
        slug = parts[2]
        if expected_project_slug is not None and slug != expected_project_slug:
            return False
        if (
            len(parts) == 4
            and parts[3] == MCP_APPROVALS_BASENAME
            and rel_posix == project_mcp_approvals_relpath(slug)
        ):
            return True
        if (
            allow_repo_json
            and len(parts) == 4
            and parts[3] == REPO_JSON_BASENAME
            and rel_posix == project_repo_json_relpath(slug)
        ):
            return True
    if len(parts) == 4 and parts[0] == ".cursor" and parts[1] == "projects" and parts[3] == "worker.log":
        return bool(parts[2])
    if (
        len(parts) == 4
        and parts[0] == ".cursor"
        and parts[1] == "projects"
        and parts[3] == ".workspace-trusted"
    ):
        return bool(parts[2])
    if (
        len(parts) == 6
        and parts[0] == ".cursor"
        and parts[1] == "projects"
        and parts[3] == "agent-transcripts"
        and _UUID_RE.fullmatch(parts[4])
        and parts[5] == f"{parts[4]}.jsonl"
    ):
        return bool(parts[2])
    return False


_FORBIDDEN_HOME_CURSOR_PREFIXES = (
    ".cursor/plugins/",
    ".cursor/skills-cursor/",
    ".cursor/hooks/",
    ".cursor/rules/",
    ".cursor/skills/",
    ".cursor/mcp-approvals.json",
)


def scan_forbidden_home_cursor_paths(
    home_dir: Path,
    *,
    expected_project_slug: str | None = None,
    allow_repo_json: bool = False,
) -> list[str]:
    """Fail closed on hooks, plugins, skills, approvals, or other non-session HOME state."""
    cursor_home = home_dir / ".cursor"
    if not cursor_home.is_dir():
        return []
    problems: list[str] = []
    for path in sorted(cursor_home.rglob("*")):
        if path.is_symlink():
            rel = path.relative_to(home_dir).as_posix()
            problems.append(f"forbidden home cursor symlink: {rel}")
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(home_dir).as_posix()
        for prefix in _FORBIDDEN_HOME_CURSOR_PREFIXES:
            if rel == prefix.rstrip("/") or rel.startswith(prefix):
                problems.append(f"forbidden home cursor path: {rel}")
                break
        else:
            if rel.endswith("/mcp-auth.json") or rel.endswith("mcp-auth.json"):
                problems.append(f"forbidden home cursor path: {rel}")
            elif not is_allowed_home_cursor_session_relpath(
                rel,
                expected_project_slug=expected_project_slug,
                allow_repo_json=allow_repo_json,
            ):
                problems.append(f"unexpected home cursor path: {rel}")
    return problems


def compare_post_warmup_gate_a_security(
    post_discovery_baseline: dict[str, str],
    *,
    cursor_config_dir: Path,
    workspace_mcp_config: Path | None,
) -> list[str]:
    """Fail closed when warmup mutates mandatory MCP/policy pins (not a new baseline)."""
    observed = security_config_fingerprints(
        cursor_config_dir=cursor_config_dir,
        workspace_mcp_config=workspace_mcp_config,
    )
    problems = compare_mandatory_config_fingerprints(post_discovery_baseline, observed)
    cli_path = cursor_config_dir / "cli-config.json"
    steering_base = post_discovery_baseline.get("cli-config.json#steering")
    if steering_base is not None:
        problems.extend(compare_cli_config_steering_after_warmup(steering_base, cli_path))
    problems.extend(unknown_cli_config_top_level_key_problems(cli_path))
    return problems


def compare_post_positive_gate_a_security(
    baseline: dict[str, str],
    *,
    cursor_config_dir: Path,
    workspace_mcp_config: Path | None,
    home_dir: Path,
    trial_workspace: Path | None = None,
) -> list[str]:
    """V7 post-positive gate: policy pins, steering, mandatory MCP config, HOME session shape."""
    from dev.factory.gate_a_trial.project_files import (
        GateAProjectFilesBaseline,
        validate_project_files_after_positive,
    )

    observed = security_config_fingerprints(
        cursor_config_dir=cursor_config_dir,
        workspace_mcp_config=workspace_mcp_config,
    )
    problems = compare_mandatory_config_fingerprints(baseline, observed)
    cli_path = cursor_config_dir / "cli-config.json"
    steering_base = baseline.get("cli-config.json#steering")
    if steering_base is not None:
        problems.extend(compare_cli_config_steering(steering_base, cli_path))
    keys_base = baseline.get("cli-config.json#top_level_keys")
    if keys_base is not None:
        problems.extend(compare_cli_config_top_level_keys(keys_base, cli_path))
    project_slug = (
        expected_cursor_project_slug(trial_workspace)
        if trial_workspace is not None
        else None
    )
    problems.extend(
        scan_forbidden_home_cursor_paths(
            home_dir,
            expected_project_slug=project_slug,
            allow_repo_json=trial_workspace is not None,
        ),
    )
    project_baseline = GateAProjectFilesBaseline.from_fingerprint_map(baseline)
    if trial_workspace is not None:
        if project_baseline is None:
            problems.append("project files baseline missing after warmup")
        else:
            problems.extend(
                validate_project_files_after_positive(home_dir, trial_workspace, project_baseline),
            )
    return problems


def compare_config_inventory(
    baseline: dict[str, str],
    observed: dict[str, str],
) -> list[str]:
    """Fail closed when post-CLI config inventory diverges from the post-discovery baseline."""
    problems: list[str] = []
    keys = set(baseline) | set(observed)
    for key in sorted(keys):
        if key not in baseline:
            problems.append(f"config inventory new after baseline: {key}")
        elif key not in observed:
            problems.append(f"config inventory missing vs baseline: {key}")
        elif observed[key] != baseline[key]:
            problems.append(f"config inventory changed for {key}")
    return problems


def compare_mandatory_config_fingerprints(
    pre_enable: dict[str, str],
    post_observed: dict[str, str],
) -> list[str]:
    """
    Fail closed when mandatory pins drift between pre-enable and post-enable discovery.
    """
    problems: list[str] = []
    try:
        pinned_pre = pin_mandatory_fingerprints(pre_enable)
    except ValueError as exc:
        return [str(exc)]
    for key, pre_val in pinned_pre.items():
        post_val = post_observed.get(key)
        if post_val is None:
            problems.append(f"config hash missing post-enable: {key}")
        elif post_val != pre_val:
            problems.append(f"config hash drift for {key}: pre={pre_val} post={post_val}")
    return problems


def _fingerprint_config_file(path: Path, label: str, out: dict[str, str]) -> None:
    if not path.is_file():
        return
    if path.name in _SKIP_INVENTORY_BASENAMES:
        return
    if path.name in _SECRET_BASENAMES:
        out[label] = _REDACTED_PRESENT
        return
    out[label] = sha256_file(path)


def trial_private_config_fingerprints(
    *,
    cursor_config_dir: Path,
    workspace_mcp_config: Path | None,
    home_dir: Path | None = None,
) -> dict[str, str]:
    """
    Inventory hashes for config the trial controls or that ``mcp enable`` may touch.

    Secret-store files are never hashed; only a redacted presence marker is recorded.
    """
    out = dict(
        effective_config_hashes(
            cursor_config_dir=cursor_config_dir,
            workspace_mcp_config=workspace_mcp_config,
        )
    )
    if cursor_config_dir.is_dir():
        for path in sorted(cursor_config_dir.iterdir()):
            if not path.is_file():
                continue
            label = path.name
            if label in out:
                continue
            _fingerprint_config_file(path, label, out)
    if home_dir is not None:
        cursor_home = home_dir / ".cursor"
        if cursor_home.is_dir():
            for path in sorted(cursor_home.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(home_dir).as_posix()
                if rel in out:
                    continue
                _fingerprint_config_file(path, rel, out)
    return out
