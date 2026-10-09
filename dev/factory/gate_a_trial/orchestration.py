"""Reusable Gate A Cursor CLI orchestration (profile, MCP, stream, certification)."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev.factory.gate_a_mcp.checkout import (
    artifact_path_under_canonical_evidence_root,
    is_regular_bound_artifact_file,
    list_evidence_archive_dir_names,
)
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME, MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_mcp.process_witness import (
    ProcessWitnessError,
    QualifiedProcessWitness,
    load_qualified_witness,
)
from dev.factory.gate_a_trial.config_hashes import (
    compare_mandatory_config_fingerprints,
    compare_post_positive_gate_a_security,
    compare_post_warmup_gate_a_security,
    effective_config_hashes,
    pin_mandatory_fingerprints,
    scan_forbidden_home_cursor_paths,
    security_config_fingerprints,
)
from dev.factory.gate_a_trial.constants import TRIAL_HEADLESS_MODEL
from dev.factory.gate_a_trial.cursor_cli_sandbox import (
    GateACursorCliSandbox,
    GateACursorCliSandboxError,
    denied_home_trees_have_content,
    prepare_gate_a_cursor_cli_sandbox,
)
from dev.factory.gate_a_trial.cursor_profile import (
    materialize_cursor_config_dir,
    resolve_trial_cursor_executable,
)
from dev.factory.gate_a_trial.mcp_discovery import discover_and_gate_gate_a_mcp
from dev.factory.gate_a_trial.positive_binding import verify_positive_mcp_receipt
from dev.factory.gate_a_trial.prestarted_mcp import PrestartedGateAMcp, start_prestarted_gate_a_mcp
from dev.factory.gate_a_trial.project_files import (
    establish_project_files_baseline_after_warmup,
    expected_cursor_project_slug,
)
from dev.factory.gate_a_trial.secret_redact import redact_mapping_strings
from dev.factory.gate_a_trial.stream_json import (
    GateAPositiveMcpPayloadMode,
    StreamJsonSummary,
    ToolCallObservation,
    headless_stream_final_result_acceptable,
    headless_stream_init_acceptable,
    headless_stream_permission_mode_note,
    parse_stream_json,
    positive_gate_a_tool_satisfied,
)
from dev.factory.gate_a_trial.subprocess_session import run_in_new_session
from dev.factory.gate_a_trial.trial_env import (
    default_isolated_home_parent,
    dispose_isolated_home,
    materialize_isolated_home,
    prove_isolated_home_empty,
    sanitized_cursor_cli_env,
)
from dev.factory.gate_a_trial.workspace_layout import (
    dispose_trial_config_dir,
    dispose_trial_path,
    dispose_trial_workspace,
    materialize_disposable_trial_config_dir,
    materialize_disposable_trial_workspace,
)
from dev.factory.order_scoped.binding import INTERNAL_STAGE_BRIEF_HASH

_NEGATIVE_HEADLESS_TIMEOUT_SECONDS = 180.0
_POSITIVE_HEADLESS_TIMEOUT_SECONDS = 600.0
_NO_TOOL_WARMUP_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class ProfileBundle:
    """Disposable Cursor profile + workspace materialized for one admitted order."""

    cursor_executable: str
    cursor_config_dir: Path
    workspace: Path
    home_dir: str | None
    mcp_server_home: str | None
    profile: dict[str, Any]
    pre_enable_config_hashes: dict[str, str]
    discovery_env: dict[str, str]
    model_env: dict[str, str]


@dataclass
class GateATurnCertification:
    """Outcome of certifying one headless positive Gate A CLI turn."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    stream_summary: StreamJsonSummary | None = None
    mcp_payload: dict[str, Any] | None = None
    allowed_txt_evidence: str = ""
    allowed_artifact_sha256: str = ""


@dataclass
class GateAResult:
    """Result of ``run_admitted_gate_a_turn`` (library entry for production adapter)."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    certification: GateATurnCertification | None = None
    post_enable_config_hashes: dict[str, str] = field(default_factory=dict)
    prestarted: PrestartedGateAMcp | None = None


class GateAOrchestrationError(RuntimeError):
    """Fail-closed orchestration failure before or after CLI spawn."""


class _TurnComplete(Exception):
    """Internal sentinel to exit orchestration after assigning a pending result."""


def no_tool_warmup_prompt() -> str:
    return "Reply with exactly: OK. Do not use any tools."


def validate_no_tool_warmup_stream_result(stream_result: dict[str, Any]) -> list[str]:
    """Fail closed when warmup invokes tools or omits a successful final stream result."""
    problems: list[str] = []
    parsed_calls = stream_result.get("parsed_tool_calls")
    if isinstance(parsed_calls, list) and parsed_calls:
        problems.append("no-tool warmup: parsed tool call in stream")
    stdout = str(stream_result.get("stdout") or "")
    from dev.factory.gate_a_trial import stream_json as _stream_json

    summary = _stream_json.parse_stream_json(stdout)
    if summary.tool_calls:
        problems.append("no-tool warmup: tool call observed in stream")
    if summary.unparsed_tool_call_variants:
        problems.append("no-tool warmup: unparsed tool-call evidence in stream")
    if summary.legacy_tool_call_events:
        problems.append("no-tool warmup: legacy tool-call event in stream")
    final_ok = stream_result.get("stream_final_result_acceptable")
    if final_ok is None:
        final_ok, final_reason = headless_stream_final_result_acceptable(stdout)
        if not final_ok:
            problems.append(f"no-tool warmup: {final_reason}")
    elif not bool(final_ok):
        reason = stream_result.get("stream_final_result_failure")
        if isinstance(reason, str) and reason:
            problems.append(f"no-tool warmup: {reason}")
        else:
            problems.append("no-tool warmup: stream final result unacceptable")
    return problems


def positive_headless_prompt() -> str:
    tool = f"{MCP_SERVER_NAME}:{TOOL_NAME}"
    return (
        f"Call the MCP tool {tool} with an empty JSON object {{}} as arguments. "
        "Do not use Shell, Write, Read, or any other native file tool. "
        f"Return and report only the MCP tool result; the only acceptable artifact is "
        f"{BOUND_ARTIFACT_FILENAME} from that result."
    )


def run_headless_stream_json(
    cursor_executable: str,
    prompt: str,
    *,
    workspace: Path,
    env: dict[str, str],
    timeout_seconds: float,
    model: str = TRIAL_HEADLESS_MODEL,
    gate_a_sandbox: GateACursorCliSandbox | None = None,
) -> dict[str, Any]:
    """Run one headless agent turn and capture stream-json stdout."""
    argv = [
        cursor_executable,
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--trust",
    ]
    if gate_a_sandbox is not None:
        argv = gate_a_sandbox.wrap_argv(argv)
    proc = run_in_new_session(
        argv,
        cwd=str(workspace),
        env=env,
        timeout_seconds=timeout_seconds,
    )
    if proc.error:
        return redact_mapping_strings({"argv": argv, "error": proc.error}, env=env)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    summary = parse_stream_json(stdout)
    payload: dict[str, Any] = {
        "argv": argv,
        "requested_model": model,
        "executed_init_model": summary.init_model,
        "api_key_source": summary.api_key_source,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "parsed_tool_calls": [
            {
                "name": c.name,
                "status": c.status,
                "mcp_server": c.mcp_server,
            }
            for c in summary.tool_calls
        ],
    }
    if proc.timed_out:
        payload["timed_out"] = True
    init_ok, init_reason = headless_stream_init_acceptable(summary)
    payload["stream_init_acceptable"] = init_ok
    if not init_ok:
        payload["stream_init_failure"] = init_reason
    final_ok, final_reason = headless_stream_final_result_acceptable(stdout)
    payload["stream_final_result_acceptable"] = final_ok
    if not final_ok:
        payload["stream_final_result_failure"] = final_reason
    perm_note = headless_stream_permission_mode_note(summary)
    if perm_note:
        payload["stream_permission_mode_note"] = perm_note
    if summary.permission_mode:
        payload["stream_permission_mode"] = summary.permission_mode
    return redact_mapping_strings(payload, env=env)


def assert_config_stable(
    pre_hashes: dict[str, str],
    post_hashes: dict[str, str],
    *,
    required_keys: frozenset[str] | None = None,
) -> list[str]:
    """Return problems when post-enable fingerprints drift or omit required keys."""
    problems: list[str] = []
    keys = required_keys if required_keys is not None else frozenset(pre_hashes)
    for key in sorted(keys):
        pre_val = pre_hashes.get(key)
        post_val = post_hashes.get(key)
        if pre_val is None:
            problems.append(f"config hash missing pre-enable: {key}")
            continue
        if post_val is None:
            problems.append(f"config hash missing post-enable: {key}")
            continue
        if post_val != pre_val:
            problems.append(f"config hash drift for {key}: admission={pre_val} post={post_val}")
    return problems


def _admission_expired(expires_at: datetime, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now(timezone.utc)
    expires = expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return moment >= expires


def _validate_synthetic_fixture_order(order_id: str) -> list[str]:
    from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID

    if order_id != INTERNAL_STAGE_ORDER_ID:
        return [
            f"order_id must be synthetic fixture {INTERNAL_STAGE_ORDER_ID!r}, got {order_id!r}",
        ]
    return []


def materialize_order_profile(
    *,
    config_dir: Path,
    workspace: Path | None = None,
    python_executable: str | None = None,
    prestarted_mcp: PrestartedGateAMcp | None = None,
    pass_cursor_api_key: bool = False,
    isolated_home: bool = True,
) -> ProfileBundle:
    """Materialize disposable profile, workspace, and sanitized CLI env for one order."""
    cursor = resolve_trial_cursor_executable()
    python = python_executable or sys.executable
    trial_workspace = workspace or materialize_disposable_trial_workspace()
    home_dir: str | None = None
    mcp_server_home: str | None = None
    if isolated_home:
        home_path = materialize_isolated_home(default_isolated_home_parent())
        home_dir = str(home_path)
        mcp_home_path = home_path / "mcp-stdio-server-home"
        mcp_home_path.mkdir(parents=False, exist_ok=False)
        mcp_server_home = str(mcp_home_path)
        proof = prove_isolated_home_empty(home_path)
        if not proof.get("cursor_state_absent"):
            raise GateAOrchestrationError(
                "isolated HOME pre-flight failed: inherited .cursor state detected"
            )

    profile = materialize_cursor_config_dir(
        config_dir,
        cursor_executable=cursor,
        python_executable=python,
        workspace=trial_workspace,
        mcp_disposable_home=mcp_server_home,
        prestarted_mcp=prestarted_mcp,
    )
    pre_hashes = dict(profile.get("effective_config_hashes") or {})
    discovery_env = sanitized_cursor_cli_env(
        cursor_config_dir=str(config_dir),
        home_dir=home_dir,
        pass_cursor_api_key=False,
    )
    model_env = sanitized_cursor_cli_env(
        cursor_config_dir=str(config_dir),
        home_dir=home_dir,
        pass_cursor_api_key=pass_cursor_api_key,
    )
    workspace_mcp = profile.get("workspace_mcp_config")
    wmc_path = Path(workspace_mcp) if isinstance(workspace_mcp, str) else None
    pre_hashes = effective_config_hashes(
        cursor_config_dir=config_dir,
        workspace_mcp_config=wmc_path,
    )
    return ProfileBundle(
        cursor_executable=cursor,
        cursor_config_dir=config_dir,
        workspace=trial_workspace,
        home_dir=home_dir,
        mcp_server_home=mcp_server_home,
        profile=profile,
        pre_enable_config_hashes=pre_hashes,
        discovery_env=discovery_env,
        model_env=model_env,
    )


def start_bound_mcp(
    *,
    python_executable: str | None = None,
    disposable_home: Path | None = None,
    evidence_root: Path | None = None,
    admitted_brief_hash: str | None = None,
) -> PrestartedGateAMcp:
    """Start loopback Gate A MCP and return a qualified process-bound handle."""
    python = python_executable or sys.executable
    control_parent = default_isolated_home_parent()
    try:
        return start_prestarted_gate_a_mcp(
            python_executable=python,
            control_parent=control_parent,
            disposable_home=disposable_home,
            evidence_root=evidence_root,
            admitted_brief_hash=admitted_brief_hash,
        )
    except ProcessWitnessError as exc:
        raise GateAOrchestrationError(
            f"prestarted MCP failed to publish qualified witness: {exc}"
        ) from exc


def _redact_certified_mcp_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ephemeral on-disk paths; synthetic adapter results are hash-bound only."""
    redacted = dict(payload)
    redacted.pop("artifact_path", None)
    return redacted


def _redact_json_text_payload(text: str) -> str:
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if not isinstance(parsed, dict):
        return text
    redacted = dict(parsed)
    redacted.pop("artifact_path", None)
    return json.dumps(redacted)


def _redact_mcp_tool_result(result: Any) -> Any:
    """Remove artifact paths from MCP tool results returned to callers."""
    if isinstance(result, dict):
        if "success" in result:
            success = result.get("success")
            if isinstance(success, dict) and isinstance(success.get("content"), list):
                new_success = dict(success)
                new_blocks: list[Any] = []
                for block in success["content"]:
                    if not isinstance(block, dict):
                        new_blocks.append(block)
                        continue
                    new_block = dict(block)
                    text_val = new_block.get("text")
                    if isinstance(text_val, str):
                        new_block["text"] = _redact_json_text_payload(text_val)
                    elif isinstance(text_val, dict):
                        nested = dict(text_val)
                        inner = nested.get("text")
                        if isinstance(inner, str):
                            nested["text"] = _redact_json_text_payload(inner)
                        new_block["text"] = nested
                    new_blocks.append(new_block)
                new_success["content"] = new_blocks
                return {**result, "success": new_success}
        return {k: _redact_mcp_tool_result(v) for k, v in result.items() if k != "artifact_path"}
    if isinstance(result, str):
        return _redact_json_text_payload(result)
    if isinstance(result, list):
        return [_redact_mcp_tool_result(item) for item in result]
    return result


def _redact_stream_summary_for_return(summary: StreamJsonSummary) -> StreamJsonSummary:
    from dev.factory.gate_a_mcp.constants import TOOL_NAME

    redacted_calls: list[ToolCallObservation] = []
    for call in summary.tool_calls:
        if call.name == TOOL_NAME and call.status == "completed":
            redacted_calls.append(
                ToolCallObservation(
                    name=call.name,
                    status=call.status,
                    call_id=call.call_id,
                    args=call.args,
                    result=_redact_mcp_tool_result(call.result),
                    mcp_server=call.mcp_server,
                ),
            )
        else:
            redacted_calls.append(call)
    return StreamJsonSummary(
        init_model=summary.init_model,
        api_key_source=summary.api_key_source,
        permission_mode=summary.permission_mode,
        tool_calls=redacted_calls,
        unparsed_tool_call_variants=summary.unparsed_tool_call_variants,
        matched_2026_call_ids=set(summary.matched_2026_call_ids),
        legacy_tool_call_events=summary.legacy_tool_call_events,
    )


def certify_positive_turn(
    stream_result: dict[str, Any],
    *,
    prestarted: PrestartedGateAMcp | None,
    witness: QualifiedProcessWitness | None,
    evidence_dirs_before: set[str] | None = None,
    admitted_order_id: str | None = None,
    admitted_brief_hash: str | None = None,
    evidence_root_parent: Path | None = None,
    mcp_server_home: Path | None = None,
) -> GateATurnCertification:
    """Certify one positive headless stream against witness-bound MCP receipt rules."""
    problems: list[str] = []
    stdout = str(stream_result.get("stdout") or "")
    summary = parse_stream_json(stdout)
    init_ok = bool(stream_result.get("stream_init_acceptable"))
    cli_failed = bool(
        stream_result.get("error")
        or stream_result.get("timed_out")
        or stream_result.get("returncode") != 0
    )
    if cli_failed:
        problems.append("headless CLI failed (nonzero exit, timeout, or subprocess error)")
    if not init_ok:
        problems.append(f"stream init rejected: {stream_result.get('stream_init_failure')}")
    adapter_stream = evidence_root_parent is not None and admitted_brief_hash is not None
    mcp_mode = (
        GateAPositiveMcpPayloadMode.ADAPTER
        if adapter_stream
        else GateAPositiveMcpPayloadMode.LEGACY_V34
    )
    payload = (
        positive_gate_a_tool_satisfied(
            summary,
            TOOL_NAME,
            mcp_payload_mode=mcp_mode,
            evidence_root_parent=evidence_root_parent,
            admitted_brief_hash=admitted_brief_hash,
        )
        if init_ok and not cli_failed
        else None
    )
    verified_sha = ""
    certified_payload: dict[str, Any] | None = None
    if payload is None and init_ok and not cli_failed:
        problems.append("positive MCP tool call not satisfied in stream")
    if payload is not None:
        if admitted_order_id is not None:
            payload_order = payload.get("order_id")
            if payload_order != admitted_order_id:
                problems.append("MCP payload order_id does not match admitted synthetic order")
        if admitted_brief_hash is not None:
            payload_brief = payload.get("brief_hash")
            if not isinstance(payload_brief, str) or not payload_brief.strip():
                problems.append("MCP payload brief_hash missing or empty")
            elif payload_brief != admitted_brief_hash:
                problems.append("MCP payload brief_hash does not match admission")
        artifact_path = payload.get("artifact_path")
        if not isinstance(artifact_path, str):
            problems.append("positive MCP payload missing artifact_path")
        else:
            path = Path(artifact_path)
            under, reason, _ = artifact_path_under_canonical_evidence_root(
                path,
                parent=evidence_root_parent,
            )
            if path.name != BOUND_ARTIFACT_FILENAME:
                problems.append(
                    f"artifact basename must be {BOUND_ARTIFACT_FILENAME!r}, got {path.name!r}"
                )
            elif not under:
                problems.append(f"artifact_path outside evidence root: {reason}")
            else:
                regular, reg_reason = is_regular_bound_artifact_file(path)
                if not regular:
                    problems.append(f"artifact_path not a regular file: {reg_reason}")
                elif evidence_root_parent is None:
                    problems.append("evidence_root_parent required for adapter artifact binding")
                else:
                    dirs_before = (
                        evidence_dirs_before
                        if evidence_dirs_before is not None
                        else list_evidence_archive_dir_names(evidence_root_parent)
                    )
                    binding_ok, binding_problems = verify_positive_mcp_receipt(
                        payload,
                        artifact_path=path,
                        prestarted=prestarted,
                        witness=witness,
                        evidence_dirs_before=dirs_before,
                        evidence_root_parent=evidence_root_parent,
                        mcp_server_home=mcp_server_home,
                    )
                    if not binding_ok:
                        problems.extend(binding_problems)
                    else:
                        digest = payload.get("artifact_sha256")
                        if isinstance(digest, str) and len(digest) == 64:
                            verified_sha = digest
                            certified_payload = _redact_certified_mcp_payload(payload)
                        else:
                            problems.append("artifact_sha256 must be a 64-char hex digest")
        if summary.attempted_tool_named("Read") is not None:
            problems.append("stream contained native Read tool call")
        if not summary.native_shell_write_calls_denied():
            problems.append("stream contained Shell/Write not fully denied")
    ok = not problems and bool(verified_sha)
    return_gate_payload = certified_payload if ok else payload
    return_summary = _redact_stream_summary_for_return(summary) if ok else summary
    return GateATurnCertification(
        ok=ok,
        problems=problems,
        stream_summary=return_summary,
        mcp_payload=return_gate_payload,
        allowed_txt_evidence="",
        allowed_artifact_sha256=verified_sha if ok else "",
    )


def _materialize_turn_evidence_root() -> Path:
    parent = default_isolated_home_parent() / "turn-evidence"
    parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(prefix="omnigent-gate-a-turn-evidence-", dir=str(parent)),
    )


def _finalize_gate_a_result(result: GateAResult, cleanup_problems: list[str]) -> GateAResult:
    problems = list(result.problems)
    ok = result.ok
    if cleanup_problems:
        problems.extend(f"cleanup: {p}" for p in cleanup_problems)
        ok = False
    return GateAResult(
        ok=ok,
        problems=problems,
        certification=result.certification,
        post_enable_config_hashes=result.post_enable_config_hashes,
        prestarted=None,
    )


def run_admitted_gate_a_turn(
    *,
    order_id: str,
    brief_hash: str,
    expires_at: datetime,
    pass_cursor_api_key: bool = False,
    run_discovery: bool = True,
) -> GateAResult:
    """
    One admitted order: prestart MCP → profile → discovery → positive CLI → certify.

    Creates a disposable no-remote git workspace and private ``CURSOR_CONFIG_DIR`` per
    turn; never writes into caller-supplied paths. Witness nonce/pid are read only from
    the adapter-owned ``start_bound_mcp`` handle. ``pass_cursor_api_key`` is required
    for a successful positive CLI certification.
    """
    pending: GateAResult | None = None
    cleanup_problems: list[str] = []
    prestarted: PrestartedGateAMcp | None = None
    adapter_workspace: Path | None = None
    adapter_config_dir: Path | None = None
    isolated_home: Path | None = None
    turn_evidence_root: Path | None = None
    gate_a_sandbox: GateACursorCliSandbox | None = None

    def _complete(result: GateAResult) -> None:
        nonlocal pending
        pending = result
        raise _TurnComplete()

    try:
        if not run_discovery:
            _complete(GateAResult(ok=False, problems=["run_discovery=False is not allowed"]))

        if _admission_expired(expires_at):
            _complete(GateAResult(ok=False, problems=["admission expired before orchestration"]))

        fixture_problems = _validate_synthetic_fixture_order(order_id)
        if fixture_problems:
            _complete(GateAResult(ok=False, problems=fixture_problems))

        if brief_hash != INTERNAL_STAGE_BRIEF_HASH:
            _complete(
                GateAResult(
                    ok=False,
                    problems=["brief_hash must match the canonical synthetic fixture brief hash"],
                ),
            )

        adapter_workspace = materialize_disposable_trial_workspace()
        adapter_config_dir = materialize_disposable_trial_config_dir()
        config_dir = adapter_config_dir
        workspace = adapter_workspace

        isolated_home = materialize_isolated_home(default_isolated_home_parent())
        mcp_home = isolated_home / "mcp-stdio-server-home"
        mcp_home.mkdir(parents=False, exist_ok=False)
        proof = prove_isolated_home_empty(isolated_home)
        if not proof.get("cursor_state_absent"):
            _complete(
                GateAResult(
                    ok=False,
                    problems=["isolated HOME pre-flight failed: inherited .cursor state"],
                ),
            )

        if _admission_expired(expires_at):
            _complete(GateAResult(ok=False, problems=["admission expired after preflight"]))

        turn_evidence_root = _materialize_turn_evidence_root()
        prestarted = start_bound_mcp(
            disposable_home=mcp_home,
            evidence_root=turn_evidence_root,
            admitted_brief_hash=brief_hash,
        )

        bundle = materialize_order_profile(
            config_dir=config_dir,
            workspace=workspace,
            prestarted_mcp=prestarted,
            pass_cursor_api_key=pass_cursor_api_key,
            isolated_home=False,
        )

        try:
            pin_mandatory_fingerprints(bundle.pre_enable_config_hashes)
        except ValueError as exc:
            _complete(GateAResult(ok=False, problems=[str(exc)], prestarted=prestarted))

        bundle = ProfileBundle(
            cursor_executable=bundle.cursor_executable,
            cursor_config_dir=bundle.cursor_config_dir,
            workspace=bundle.workspace,
            home_dir=str(isolated_home),
            mcp_server_home=str(mcp_home),
            profile=bundle.profile,
            pre_enable_config_hashes=bundle.pre_enable_config_hashes,
            discovery_env=sanitized_cursor_cli_env(
                cursor_config_dir=str(config_dir),
                home_dir=str(isolated_home),
                pass_cursor_api_key=False,
            ),
            model_env=sanitized_cursor_cli_env(
                cursor_config_dir=str(config_dir),
                home_dir=str(isolated_home),
                pass_cursor_api_key=pass_cursor_api_key,
            ),
        )

        try:
            gate_a_sandbox = prepare_gate_a_cursor_cli_sandbox(isolated_home)
        except GateACursorCliSandboxError as exc:
            _complete(
                GateAResult(
                    ok=False,
                    problems=[str(exc)],
                    prestarted=prestarted,
                ),
            )

        discovery = discover_and_gate_gate_a_mcp(
            bundle.cursor_executable,
            str(bundle.workspace),
            bundle.discovery_env,
            cursor_config_dir=config_dir,
            workspace_mcp_config=bundle.profile.get("workspace_mcp_config"),
            gate_a_sandbox=gate_a_sandbox,
        )
        if not discovery.get("gate_passed"):
            reasons = discovery.get("gate_failure_reasons") or ["discovery gate failed"]
            _complete(
                GateAResult(
                    ok=False,
                    problems=[f"discovery: {r}" for r in reasons],
                    prestarted=prestarted,
                ),
            )
        post_discovery_denied = denied_home_trees_have_content(isolated_home)
        if post_discovery_denied:
            _complete(
                GateAResult(
                    ok=False,
                    problems=post_discovery_denied,
                    prestarted=prestarted,
                ),
            )

        post_hashes = dict(discovery.get("effective_config_hashes_post_enable") or {})

        drift = compare_mandatory_config_fingerprints(
            bundle.pre_enable_config_hashes,
            post_hashes,
        )
        if drift:
            _complete(GateAResult(ok=False, problems=drift, prestarted=prestarted))

        wmc = bundle.profile.get("workspace_mcp_config")
        wmc_path = Path(wmc) if isinstance(wmc, str) else None

        post_discovery_inventory = security_config_fingerprints(
            cursor_config_dir=config_dir,
            workspace_mcp_config=wmc_path,
        )

        warmup = run_headless_stream_json(
            bundle.cursor_executable,
            no_tool_warmup_prompt(),
            workspace=bundle.workspace,
            env=bundle.model_env,
            timeout_seconds=_NO_TOOL_WARMUP_TIMEOUT_SECONDS,
            gate_a_sandbox=gate_a_sandbox,
        )
        sandbox_problems = denied_home_trees_have_content(isolated_home)
        if sandbox_problems:
            _complete(
                GateAResult(
                    ok=False,
                    problems=sandbox_problems,
                    prestarted=prestarted,
                    post_enable_config_hashes=post_discovery_inventory,
                ),
            )
        if warmup.get("error"):
            _complete(
                GateAResult(
                    ok=False,
                    problems=[f"no-tool warmup: {warmup['error']}"],
                    prestarted=prestarted,
                    post_enable_config_hashes=post_discovery_inventory,
                ),
            )
        warmup_rc = warmup.get("returncode")
        if warmup.get("timed_out") or warmup_rc is None or int(warmup_rc) != 0:
            _complete(
                GateAResult(
                    ok=False,
                    problems=["no-tool warmup CLI failed"],
                    prestarted=prestarted,
                    post_enable_config_hashes=post_discovery_inventory,
                ),
            )
        if not warmup.get("stream_init_acceptable"):
            _complete(
                GateAResult(
                    ok=False,
                    problems=["no-tool warmup stream init unacceptable"],
                    prestarted=prestarted,
                    post_enable_config_hashes=post_discovery_inventory,
                ),
            )
        warmup_problems = validate_no_tool_warmup_stream_result(warmup)
        if warmup_problems:
            _complete(
                GateAResult(
                    ok=False,
                    problems=warmup_problems,
                    prestarted=prestarted,
                    post_enable_config_hashes=post_discovery_inventory,
                ),
            )
        warmup_config_drift = compare_post_warmup_gate_a_security(
            post_discovery_inventory,
            cursor_config_dir=config_dir,
            workspace_mcp_config=wmc_path,
        )
        if warmup_config_drift:
            _complete(
                GateAResult(
                    ok=False,
                    problems=warmup_config_drift,
                    prestarted=prestarted,
                    post_enable_config_hashes=post_discovery_inventory,
                ),
            )

        project_slug = expected_cursor_project_slug(bundle.workspace)
        project_file_problems = scan_forbidden_home_cursor_paths(
            isolated_home,
            expected_project_slug=project_slug,
            allow_repo_json=False,
        )
        project_baseline, establish_problems = establish_project_files_baseline_after_warmup(
            isolated_home,
            bundle.workspace,
        )
        project_file_problems.extend(establish_problems)
        if project_file_problems:
            _complete(
                GateAResult(
                    ok=False,
                    problems=project_file_problems,
                    prestarted=prestarted,
                    post_enable_config_hashes=post_discovery_inventory,
                ),
            )

        post_warmup_inventory = security_config_fingerprints(
            cursor_config_dir=config_dir,
            workspace_mcp_config=wmc_path,
        )
        if project_baseline is not None:
            post_warmup_inventory.update(project_baseline.as_fingerprint_map())

        if not pass_cursor_api_key:
            _complete(
                GateAResult(
                    ok=False,
                    problems=["positive CLI requires pass_cursor_api_key"],
                    prestarted=prestarted,
                    post_enable_config_hashes=post_warmup_inventory,
                ),
            )

        witness = load_qualified_witness(prestarted.control_dir)
        from omnigent.factory.gate_a.admission import (
            FactoryGateAAdmissionError,
            FactoryWorkOrderAdmission,
            validate_witness_matches_prestarted,
            validate_witness_order_binding,
        )

        admission_record = FactoryWorkOrderAdmission(
            order_id=order_id,
            brief_hash=brief_hash,
            expires_at=expires_at,
        )
        try:
            validate_witness_matches_prestarted(prestarted, witness)
            validate_witness_order_binding(admission_record, witness)
        except FactoryGateAAdmissionError as exc:
            _complete(
                GateAResult(
                    ok=False,
                    problems=[str(exc)],
                    prestarted=prestarted,
                    post_enable_config_hashes=post_warmup_inventory,
                ),
            )

        if _admission_expired(expires_at):
            _complete(
                GateAResult(
                    ok=False,
                    problems=["admission expired before positive CLI"],
                    prestarted=prestarted,
                    post_enable_config_hashes=post_warmup_inventory,
                ),
            )

        evidence_before = list_evidence_archive_dir_names(turn_evidence_root)
        stream_result = run_headless_stream_json(
            bundle.cursor_executable,
            positive_headless_prompt(),
            workspace=bundle.workspace,
            env=bundle.model_env,
            timeout_seconds=_POSITIVE_HEADLESS_TIMEOUT_SECONDS,
            gate_a_sandbox=gate_a_sandbox,
        )
        sandbox_problems = denied_home_trees_have_content(isolated_home)
        if sandbox_problems:
            _complete(
                GateAResult(
                    ok=False,
                    problems=sandbox_problems,
                    prestarted=prestarted,
                    post_enable_config_hashes=post_warmup_inventory,
                ),
            )

        if _admission_expired(expires_at):
            _complete(
                GateAResult(
                    ok=False,
                    problems=["admission expired after positive CLI"],
                    prestarted=prestarted,
                    post_enable_config_hashes=post_warmup_inventory,
                ),
            )

        drift_after_cli = compare_post_positive_gate_a_security(
            post_warmup_inventory,
            cursor_config_dir=config_dir,
            workspace_mcp_config=wmc_path,
            home_dir=isolated_home,
            trial_workspace=bundle.workspace,
        )
        if drift_after_cli:
            _complete(
                GateAResult(
                    ok=False,
                    problems=drift_after_cli,
                    prestarted=prestarted,
                    post_enable_config_hashes=post_warmup_inventory,
                ),
            )

        certification = certify_positive_turn(
            stream_result,
            prestarted=prestarted,
            witness=witness,
            evidence_dirs_before=evidence_before,
            admitted_order_id=order_id,
            admitted_brief_hash=brief_hash,
            evidence_root_parent=turn_evidence_root,
            mcp_server_home=mcp_home,
        )
        _complete(
            GateAResult(
                ok=certification.ok,
                problems=certification.problems,
                certification=certification,
                post_enable_config_hashes=post_warmup_inventory,
                prestarted=prestarted,
            ),
        )
    except _TurnComplete:
        pass
    except GateAOrchestrationError as exc:
        pending = GateAResult(ok=False, problems=[str(exc)], prestarted=prestarted)
    finally:
        if gate_a_sandbox is not None:
            gate_a_sandbox.cleanup()
            gate_a_sandbox = None
        if prestarted is not None:
            try:
                prestarted.cleanup()
            except Exception as exc:  # noqa: BLE001 — record and continue other disposals
                cleanup_problems.append(f"prestarted MCP cleanup: {exc}")
            prestarted = None
        if adapter_workspace is not None:
            problem = dispose_trial_workspace(adapter_workspace)
            if problem:
                cleanup_problems.append(problem)
            adapter_workspace = None
        if adapter_config_dir is not None:
            problem = dispose_trial_config_dir(adapter_config_dir)
            if problem:
                cleanup_problems.append(problem)
            adapter_config_dir = None
        if turn_evidence_root is not None:
            problem = dispose_trial_path(turn_evidence_root, label="turn evidence root")
            if problem:
                cleanup_problems.append(problem)
            turn_evidence_root = None
        if isolated_home is not None:
            problem = dispose_isolated_home(isolated_home)
            if problem:
                cleanup_problems.append(problem)
            isolated_home = None

    if pending is None:
        pending = GateAResult(ok=False, problems=["orchestration aborted without result"])
    return _finalize_gate_a_result(pending, cleanup_problems)
