"""Real-task Gate A orchestration (build → verify → freeze → review → mutation check)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dev.factory.gate_a_real.agent_run import (
    AgentRunCapture,
    builder_stdout_acceptable,
    review_stdout_forbidden_tool_violations,
    review_stdout_passes,
    run_agent_capture,
)
from dev.factory.gate_a_real.constants import (
    BUILD_TIMEOUT_SECONDS,
    REAL_TASK_CONFIG_KEYS,
    REAL_TASK_ENV,
    REVIEW_TIMEOUT_SECONDS,
    VERIFY_TIMEOUT_SECONDS,
    resolve_cursor_executable,
)
from dev.factory.gate_a_real.deliverables import (
    DeliverableInventory,
    collect_deliverables,
    frozen_files_match,
    inventories_match,
    write_freeze_candidate,
)
from dev.factory.gate_a_real.profile import (
    materialize_real_task_cursor_config_dir,
    materialize_real_task_review_config_dir,
    real_task_cursor_cli_env,
)
from dev.factory.gate_a_real.receipt import (
    RealTaskReceipt,
    load_resume_state,
    utc_now_iso,
    validate_review_only_resume,
    write_resume_state,
)
from dev.factory.gate_a_real.spec import RealTaskSpec, RealTaskSpecError, validate_not_expired
from dev.factory.gate_a_trial.config_hashes import effective_config_hashes
from dev.factory.gate_a_trial.cursor_cli_sandbox import (
    GateACursorCliSandbox,
    GateACursorCliSandboxError,
    prepare_gate_a_cursor_cli_sandbox,
)
from dev.factory.gate_a_trial.mcp_discovery import discover_and_assert_zero_mcp_servers
from dev.factory.gate_a_trial.trial_env import (
    materialize_isolated_home,
    pre_enable_home_must_be_pristine,
)


class RealTaskGateError(RuntimeError):
    """Real-task Gate A failed closed."""


@dataclass
class RealTaskRunOptions:
    artifacts_dir: Path
    review_only: bool = False


@dataclass
class RealTaskRunResult:
    receipt: RealTaskReceipt
    problems: list[str] = field(default_factory=list)


def real_task_gate_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return env.get(REAL_TASK_ENV, "").strip() == "1"


def _artifacts_outside_workspace(artifacts: Path, workspace: Path) -> list[str]:
    artifacts_resolved = artifacts.resolve()
    workspace_resolved = workspace.resolve()
    problems: list[str] = []
    if artifacts_resolved == workspace_resolved:
        problems.append("artifacts_dir must not equal workspace")
    try:
        artifacts_resolved.relative_to(workspace_resolved)
        problems.append("artifacts_dir must be outside the task workspace")
    except ValueError:
        pass
    return problems


def _workspace_mcp_fingerprint(workspace: Path) -> str | None:
    config = workspace / ".cursor" / "mcp.json"
    try:
        config.resolve().relative_to(workspace.resolve())
    except ValueError:
        raise RealTaskGateError("workspace MCP path escapes workspace") from None
    if config.is_symlink():
        raise RealTaskGateError("workspace MCP config must not be a symlink")
    if not config.exists():
        return None
    if not config.is_file():
        raise RealTaskGateError("workspace MCP config is not a file")
    return hashlib.sha256(config.read_bytes()).hexdigest()


def _fail_receipt(spec: RealTaskSpec, problems: list[str]) -> RealTaskReceipt:
    return RealTaskReceipt(
        ok=False,
        task_id=spec.task_id,
        spec_sha256=spec.spec_sha256,
        workspace=str(spec.workspace),
        problems=problems,
        builder_session_ids=[],
        review_session_ids=[],
        builder_exit_code=None,
        review_exit_code=None,
        verify_exit_code=None,
        deliverable_manifest_sha256=None,
        post_review_manifest_sha256=None,
        freeze_manifest_path=None,
        review_pass=False,
        completed_at=utc_now_iso(),
    )


def _validate_config_hashes(
    observed: dict[str, str],
    *,
    pinned: dict[str, str],
    label: str,
) -> list[str]:
    problems: list[str] = []
    for key, expected in pinned.items():
        actual = observed.get(key)
        if actual is None:
            problems.append(f"missing {label} config fingerprint: {key}")
        elif actual.lower() != expected.lower():
            problems.append(f"{label} config hash drift for {key}")
    for key in observed:
        if key not in pinned:
            problems.append(f"unexpected {label} config artifact fingerprint: {key}")
    return problems


def _validate_builder_config_hashes(spec: RealTaskSpec, observed: dict[str, str]) -> list[str]:
    return _validate_config_hashes(observed, pinned=spec.config_hashes, label="builder")


def _validate_review_config_hashes(spec: RealTaskSpec, observed: dict[str, str]) -> list[str]:
    return _validate_config_hashes(observed, pinned=spec.review_config_hashes, label="review")


def _observed_profile_hashes(profile: dict[str, Any]) -> dict[str, str]:
    cursor_dir = Path(profile["cursor_config_dir"])
    try:
        hashes = effective_config_hashes(cursor_config_dir=cursor_dir, workspace_mcp_config=None)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {key: hashes[key] for key in sorted(REAL_TASK_CONFIG_KEYS) if key in hashes}


def _merge_drift_into_receipt_file(artifacts: Path, spec: RealTaskSpec, drift: list[str]) -> None:
    receipt_path = artifacts / "receipt.json"
    fail = _fail_receipt(spec, drift)
    if receipt_path.is_file():
        try:
            prev = json.loads(receipt_path.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                fail.builder_session_ids = list(prev.get("builder_session_ids") or [])
                fail.review_session_ids = list(prev.get("review_session_ids") or [])
                fail.builder_exit_code = prev.get("builder_exit_code")
                fail.review_exit_code = prev.get("review_exit_code")
                fail.verify_exit_code = prev.get("verify_exit_code")
                fail.deliverable_manifest_sha256 = prev.get("deliverable_manifest_sha256")
                fail.post_review_manifest_sha256 = prev.get("post_review_manifest_sha256")
                fail.freeze_manifest_path = prev.get("freeze_manifest_path")
                fail.builder_log_path = prev.get("builder_log_path")
                fail.review_log_path = prev.get("review_log_path")
                fail.verify_log_path = prev.get("verify_log_path")
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    fail.write(receipt_path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_agent_meta(path: Path, capture: AgentRunCapture) -> None:
    _write_text(
        path,
        json.dumps(
            {
                "argv": capture.argv,
                "exit_code": capture.exit_code,
                "session_ids": list(capture.session_ids),
                "timed_out": capture.timed_out,
                "pid": capture.pid,
                "pgid": capture.pgid,
                "stdout_log_path": capture.stdout_log_path,
                "stderr_log_path": capture.stderr_log_path,
                "stdout_sha256": capture.stdout_sha256,
                "stderr_sha256": capture.stderr_sha256,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _run_verify(spec: RealTaskSpec) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(spec.verify_command),
            cwd=str(spec.workspace),
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined


def _sandbox_prefix(sandbox: GateACursorCliSandbox, argv: list[str]) -> list[str]:
    wrapped = sandbox.wrap_argv(argv)
    return wrapped[: len(wrapped) - len(argv)]


def build_agent_argv(
    cursor_executable: str,
    spec: RealTaskSpec,
    *,
    for_review: bool,
    inventory: DeliverableInventory | None,
    freeze_dir: Path | None,
    verify_exit_code: int | None,
    verify_log: str | None,
) -> list[str]:
    from dev.factory.gate_a_real.constants import BUILD_MODEL, REVIEW_MODEL

    if for_review:
        assert inventory is not None
        assert freeze_dir is not None
        lines = [
            "Independent read-only review of a Gate A real-task freeze candidate.",
            "Use only Read, Grep, and Glob. Do not invoke Shell, Write, Edit, Delete,",
            "WebFetch, MCP, GetMcpTools, or any other tool, even for discovery.",
            "Inspect the frozen files and verification evidence before deciding.",
            "The LAST line of your final response must be exactly REVIEW: PASS",
            "only if deliverables and verification are acceptable.",
            "Otherwise the LAST line must be exactly REVIEW: FAIL.",
            "Put a newline before that verdict line and no text after it.",
            "",
            "Task objective / builder prompt:",
            spec.prompt,
            "",
            f"spec_sha256: {spec.spec_sha256}",
            f"verify_command: {json.dumps(list(spec.verify_command))}",
            f"verify_exit_code: {verify_exit_code}",
            "verify_log:",
            (verify_log or "")[:8000],
            "",
            "Deliverable manifest sha256:",
            inventory.manifest_sha256,
            "Deliverable files:",
        ]
        for record in inventory.records:
            lines.append(f"- {record.relpath} sha256={record.sha256} bytes={record.size_bytes}")
        lines.append("")
        lines.append("Frozen candidate files (read-only copies):")
        for record in inventory.records:
            frozen = freeze_dir / "files" / record.relpath
            lines.append(f"- {frozen}")
        prompt = "\n".join(lines)
        return [
            cursor_executable,
            "--print",
            "--output-format",
            "stream-json",
            "--mode",
            "ask",
            "--model",
            REVIEW_MODEL,
            "--sandbox",
            "enabled",
            "--trust",
            "--workspace",
            str(spec.workspace),
            prompt,
        ]

    argv = [
        cursor_executable,
        "--print",
        "--output-format",
        "stream-json",
        "--model",
        BUILD_MODEL,
        "--sandbox",
        "enabled",
        "--trust",
        "--workspace",
        str(spec.workspace),
    ]
    argv.append(spec.prompt)
    return argv


def _evaluate_builder(capture: AgentRunCapture) -> list[str]:
    problems: list[str] = []
    if capture.timed_out:
        problems.append("builder timed out")
    if capture.exit_code != 0:
        problems.append(f"builder exit code {capture.exit_code}")
    if not capture.session_ids:
        problems.append("builder missing Cursor session_id in stream-json")
    if capture.unexpected_mcp_activity:
        problems.append("unexpected MCP tool activity during builder")
    final_ok, final_reason = builder_stdout_acceptable(capture.stdout)
    if not final_ok:
        problems.append(
            final_reason or "builder missing successful terminal stream-json result event"
        )
    return problems


def _evaluate_review(capture: AgentRunCapture) -> list[str]:
    problems: list[str] = []
    if capture.timed_out:
        problems.append("review timed out")
    if capture.exit_code != 0:
        problems.append(f"review exit code {capture.exit_code}")
    if not capture.session_ids:
        problems.append("review missing Cursor session_id in stream-json")
    if capture.unexpected_mcp_activity:
        problems.append("unexpected MCP tool activity during review")
    problems.extend(review_stdout_forbidden_tool_violations(capture.stdout))
    if not review_stdout_passes(capture.stdout):
        problems.append(
            "review did not emit standalone REVIEW: PASS in terminal stream-json result"
        )
    return problems


def _freeze_manifest_sha256(freeze_manifest: Path) -> str:
    return hashlib.sha256(freeze_manifest.read_bytes()).hexdigest()


def _next_review_attempt_dir(artifacts: Path) -> Path:
    parent = artifacts / "review-attempts"
    parent.mkdir(exist_ok=True)
    index = 1
    while (parent / f"{index:04d}").exists():
        index += 1
    attempt = parent / f"{index:04d}"
    attempt.mkdir()
    return attempt


def run_real_task_gate(spec: RealTaskSpec, options: RealTaskRunOptions) -> RealTaskRunResult:
    if not real_task_gate_enabled():
        raise RealTaskGateError(f"{REAL_TASK_ENV}=1 is required for the real-task Gate A path")

    problems: list[str] = []
    try:
        validate_not_expired(spec)
    except RealTaskSpecError as exc:
        receipt = _fail_receipt(spec, [str(exc)])
        return RealTaskRunResult(receipt=receipt, problems=[str(exc)])

    artifacts = options.artifacts_dir.resolve()
    layout_problems = _artifacts_outside_workspace(artifacts, spec.workspace)
    if layout_problems:
        receipt = _fail_receipt(spec, layout_problems)
        return RealTaskRunResult(receipt=receipt, problems=layout_problems)

    artifacts.mkdir(parents=True, exist_ok=True)
    state_path = artifacts / "resume_state.json"
    prior_state = load_resume_state(state_path)

    if not options.review_only and (
        prior_state is not None or (artifacts / "builder.stdout.txt").exists()
    ):
        msg = "builder attempt already recorded; use --review-only or a fresh artifacts dir"
        receipt = _fail_receipt(spec, [msg])
        receipt.write(artifacts / "receipt.json")
        return RealTaskRunResult(receipt=receipt, problems=[msg])

    cursor_executable = resolve_cursor_executable()
    config_parent = Path(tempfile.mkdtemp(prefix="gate-a-real-config-", dir=str(artifacts)))
    review_config_parent: Path | None = None
    sandbox: GateACursorCliSandbox | None = None
    review_sandbox: GateACursorCliSandbox | None = None
    profile: dict[str, Any] | None = None
    review_profile: dict[str, Any] | None = None
    run_result: RealTaskRunResult | None = None

    try:
        profile = materialize_real_task_cursor_config_dir(config_parent)
        config_problems = _validate_builder_config_hashes(spec, profile["effective_config_hashes"])
        if config_problems:
            receipt = _fail_receipt(spec, config_problems)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=config_problems)

        home_parent = artifacts / "isolated-home-parent"
        home = materialize_isolated_home(home_parent)
        home_problems = pre_enable_home_must_be_pristine(home)
        if home_problems:
            receipt = _fail_receipt(spec, home_problems)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=home_problems)

        try:
            sandbox = prepare_gate_a_cursor_cli_sandbox(home)
        except GateACursorCliSandboxError as exc:
            receipt = _fail_receipt(spec, [str(exc)])
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=[str(exc)])

        try:
            env = real_task_cursor_cli_env(
                cursor_config_dir=profile["cursor_config_dir"],
                home_dir=str(home),
            )
        except ValueError as exc:
            receipt = _fail_receipt(spec, [str(exc)])
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=[str(exc)])
        try:
            workspace_mcp_before = _workspace_mcp_fingerprint(spec.workspace)
        except (RealTaskGateError, OSError) as exc:
            receipt = _fail_receipt(spec, [str(exc)])
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=[str(exc)])

        mcp_gate = discover_and_assert_zero_mcp_servers(
            cursor_executable,
            str(spec.workspace),
            env,
            gate_a_sandbox=sandbox,
        )
        _write_text(
            artifacts / "mcp_preflight.json", json.dumps(mcp_gate, indent=2, sort_keys=True) + "\n"
        )
        if not mcp_gate.get("gate_passed"):
            mcp_problems = list(mcp_gate.get("gate_failure_reasons") or [])
            if not mcp_problems:
                mcp_problems = ["mcp preflight failed closed"]
            receipt = _fail_receipt(spec, mcp_problems)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=mcp_problems)

        builder_session_ids: list[str] = []
        builder_exit_code: int | None = None
        builder_stdout_sha: str | None = None
        builder_ok = False

        if options.review_only:
            if prior_state is None:
                msg = "review-only requires resume_state.json"
                receipt = _fail_receipt(spec, [msg])
                receipt.write(artifacts / "receipt.json")
                return RealTaskRunResult(receipt=receipt, problems=[msg])
            drift = validate_review_only_resume(spec, artifacts, prior_state)
            if drift:
                receipt = _fail_receipt(spec, drift)
                receipt.write(artifacts / "receipt.json")
                return RealTaskRunResult(receipt=receipt, problems=drift)
            raw_ids = prior_state.get("builder_session_ids") or []
            if isinstance(raw_ids, list):
                builder_session_ids = [str(x) for x in raw_ids]
            builder_exit_code = int(prior_state.get("builder_exit_code") or 0)
            builder_stdout_sha = (
                str(prior_state["builder_stdout_sha256"])
                if prior_state.get("builder_stdout_sha256")
                else None
            )
            builder_ok = True
        else:
            builder_argv = build_agent_argv(
                cursor_executable,
                spec,
                for_review=False,
                inventory=None,
                freeze_dir=None,
                verify_exit_code=None,
                verify_log=None,
            )
            builder_stdout_log = artifacts / "builder.stdout.txt"
            builder_stderr_log = artifacts / "builder.stderr.txt"
            builder_capture = run_agent_capture(
                builder_argv,
                cwd=str(spec.workspace),
                env=env,
                timeout_seconds=BUILD_TIMEOUT_SECONDS,
                sandbox_argv_wrapper=_sandbox_prefix(sandbox, builder_argv),
                stdout_log=builder_stdout_log,
                stderr_log=builder_stderr_log,
            )
            _write_agent_meta(artifacts / "builder.meta.json", builder_capture)
            builder_exit_code = builder_capture.exit_code
            builder_session_ids = list(builder_capture.session_ids)
            builder_stdout_sha = builder_capture.stdout_sha256
            builder_problems = _evaluate_builder(builder_capture)
            problems.extend(builder_problems)
            builder_ok = not builder_problems
            write_resume_state(
                state_path,
                {
                    "builder_ok": builder_ok,
                    "spec_sha256": spec.spec_sha256,
                    "workspace": str(spec.workspace),
                    "builder_session_ids": builder_session_ids,
                    "builder_exit_code": builder_exit_code,
                    "builder_stdout_sha256": builder_stdout_sha,
                    "deliverable_manifest_sha256": prior_state.get("deliverable_manifest_sha256")
                    if prior_state
                    else None,
                },
            )
            if not builder_ok:
                receipt = RealTaskReceipt(
                    ok=False,
                    task_id=spec.task_id,
                    spec_sha256=spec.spec_sha256,
                    workspace=str(spec.workspace),
                    problems=problems,
                    builder_session_ids=builder_session_ids,
                    review_session_ids=[],
                    builder_exit_code=builder_exit_code,
                    review_exit_code=None,
                    verify_exit_code=None,
                    deliverable_manifest_sha256=None,
                    post_review_manifest_sha256=None,
                    freeze_manifest_path=None,
                    review_pass=False,
                    completed_at=utc_now_iso(),
                )
                receipt.write(artifacts / "receipt.json")
                return RealTaskRunResult(receipt=receipt, problems=problems)

        try:
            inventory = collect_deliverables(spec.workspace, spec.deliverable_paths)
        except (OSError, ValueError) as exc:
            problems.append(str(exc))
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)

        if options.review_only and prior_state:
            expected_manifest = prior_state.get("deliverable_manifest_sha256")
            if expected_manifest and inventory.manifest_sha256 != expected_manifest:
                problems.append("deliverable manifest drift vs resume_state before review")
                receipt = _fail_receipt(spec, problems)
                receipt.builder_session_ids = builder_session_ids
                receipt.builder_exit_code = builder_exit_code
                receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
                receipt.write(artifacts / "receipt.json")
                return RealTaskRunResult(receipt=receipt, problems=problems)

        verify_code, verify_log = _run_verify(spec)
        _write_text(artifacts / "verify.log", verify_log)
        _write_text(
            artifacts / "verify.meta.json",
            json.dumps(
                {
                    "argv": list(spec.verify_command),
                    "exit_code": verify_code,
                    "log_sha256": hashlib.sha256(verify_log.encode("utf-8")).hexdigest(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        if verify_code != 0:
            problems.append(f"verify command exit code {verify_code}")
            receipt = RealTaskReceipt(
                ok=False,
                task_id=spec.task_id,
                spec_sha256=spec.spec_sha256,
                workspace=str(spec.workspace),
                problems=problems,
                builder_session_ids=builder_session_ids,
                review_session_ids=[],
                builder_exit_code=builder_exit_code,
                review_exit_code=None,
                verify_exit_code=verify_code,
                deliverable_manifest_sha256=inventory.manifest_sha256,
                post_review_manifest_sha256=None,
                freeze_manifest_path=None,
                review_pass=False,
                completed_at=utc_now_iso(),
            )
            receipt.write(artifacts / "receipt.json")
            write_resume_state(
                state_path,
                {
                    "builder_ok": builder_ok,
                    "spec_sha256": spec.spec_sha256,
                    "workspace": str(spec.workspace),
                    "builder_session_ids": builder_session_ids,
                    "builder_exit_code": builder_exit_code,
                    "builder_stdout_sha256": builder_stdout_sha,
                    "deliverable_manifest_sha256": inventory.manifest_sha256,
                    "verify_exit_code": verify_code,
                },
            )
            return RealTaskRunResult(receipt=receipt, problems=problems)

        try:
            verified_inventory = collect_deliverables(spec.workspace, spec.deliverable_paths)
        except (OSError, ValueError) as exc:
            problems.append(f"post-verification deliverable scan failed: {exc}")
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)
        if options.review_only and not inventories_match(inventory, verified_inventory):
            problems.append("deliverables changed during review-only verification")
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)
        inventory = verified_inventory

        freeze_dir = artifacts / "freeze"
        try:
            freeze_manifest = write_freeze_candidate(inventory, spec.workspace, freeze_dir)
        except (OSError, ValueError) as exc:
            problems.append(f"freeze failed: {exc}")
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)
        freeze_sha = _freeze_manifest_sha256(freeze_manifest)
        review_config_parent = Path(
            tempfile.mkdtemp(prefix="gate-a-review-config-", dir=str(artifacts))
        )
        review_profile = materialize_real_task_review_config_dir(review_config_parent)
        review_config_problems = _validate_review_config_hashes(
            spec, review_profile["effective_config_hashes"]
        )
        if review_config_problems:
            problems.extend(review_config_problems)
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.freeze_manifest_path = str(freeze_manifest)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)
        _write_text(
            artifacts / "review.profile.json",
            json.dumps(review_profile["effective_config_hashes"], indent=2, sort_keys=True) + "\n",
        )

        review_home_parent = artifacts / "isolated-review-home-parent"
        review_home = materialize_isolated_home(review_home_parent)
        review_home_problems = pre_enable_home_must_be_pristine(review_home)
        if review_home_problems:
            problems.extend(review_home_problems)
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.freeze_manifest_path = str(freeze_manifest)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)

        try:
            review_sandbox = prepare_gate_a_cursor_cli_sandbox(review_home)
        except GateACursorCliSandboxError as exc:
            problems.append(str(exc))
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.freeze_manifest_path = str(freeze_manifest)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)

        try:
            review_env = real_task_cursor_cli_env(
                cursor_config_dir=review_profile["cursor_config_dir"],
                home_dir=str(review_home),
            )
        except ValueError as exc:
            problems.append(str(exc))
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.freeze_manifest_path = str(freeze_manifest)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)

        try:
            mcp_config_unchanged = (
                _workspace_mcp_fingerprint(spec.workspace) == workspace_mcp_before
            )
        except (RealTaskGateError, OSError):
            mcp_config_unchanged = False
        if not mcp_config_unchanged:
            problems.append("workspace MCP config changed before review")
        review_mcp_gate = discover_and_assert_zero_mcp_servers(
            cursor_executable,
            str(spec.workspace),
            review_env,
            gate_a_sandbox=review_sandbox,
        )
        _write_text(
            artifacts / "mcp_review_preflight.json",
            json.dumps(review_mcp_gate, indent=2, sort_keys=True) + "\n",
        )
        if not review_mcp_gate.get("gate_passed"):
            problems.extend(
                review_mcp_gate.get("gate_failure_reasons") or ["review MCP preflight failed"]
            )
        if problems:
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.freeze_manifest_path = str(freeze_manifest)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)

        builder_config_drift = _validate_builder_config_hashes(
            spec, _observed_profile_hashes(profile)
        )
        if builder_config_drift:
            problems.extend(builder_config_drift)
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.freeze_manifest_path = str(freeze_manifest)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)

        review_config_drift = _validate_review_config_hashes(
            spec, _observed_profile_hashes(review_profile)
        )
        if review_config_drift:
            problems.extend(review_config_drift)
            receipt = _fail_receipt(spec, problems)
            receipt.builder_session_ids = builder_session_ids
            receipt.builder_exit_code = builder_exit_code
            receipt.verify_exit_code = verify_code
            receipt.deliverable_manifest_sha256 = inventory.manifest_sha256
            receipt.freeze_manifest_path = str(freeze_manifest)
            receipt.write(artifacts / "receipt.json")
            return RealTaskRunResult(receipt=receipt, problems=problems)

        review_argv = build_agent_argv(
            cursor_executable,
            spec,
            for_review=True,
            inventory=inventory,
            freeze_dir=freeze_dir,
            verify_exit_code=verify_code,
            verify_log=verify_log,
        )
        review_attempt = _next_review_attempt_dir(artifacts)
        review_stdout_log = review_attempt / "stdout.stream-json.log"
        review_stderr_log = review_attempt / "stderr.log"
        review_capture = run_agent_capture(
            review_argv,
            cwd=str(spec.workspace),
            env=review_env,
            timeout_seconds=REVIEW_TIMEOUT_SECONDS,
            sandbox_argv_wrapper=_sandbox_prefix(review_sandbox, review_argv),
            stdout_log=review_stdout_log,
            stderr_log=review_stderr_log,
        )
        _write_agent_meta(review_attempt / "meta.json", review_capture)
        review_problems = _evaluate_review(review_capture)
        problems.extend(review_problems)
        try:
            if _workspace_mcp_fingerprint(spec.workspace) != workspace_mcp_before:
                problems.append("workspace MCP config changed during review")
        except (RealTaskGateError, OSError) as exc:
            problems.append(str(exc))

        post_manifest_sha256: str | None = None
        post_inventory: DeliverableInventory | None = None
        try:
            post_inventory = collect_deliverables(spec.workspace, spec.deliverable_paths)
            post_manifest_sha256 = post_inventory.manifest_sha256
        except (OSError, ValueError) as exc:
            problems.append(f"post-review deliverable scan failed: {exc}")

        scan_ok = post_inventory is not None
        mutation_ok = scan_ok and inventories_match(inventory, post_inventory)
        if not mutation_ok:
            problems.append("deliverables changed after review (post-review mutation)")
        try:
            frozen_intact = frozen_files_match(inventory, freeze_dir) and (
                _freeze_manifest_sha256(freeze_manifest) == freeze_sha
            )
        except OSError:
            frozen_intact = False
        if not frozen_intact:
            problems.append("frozen candidate changed during review")

        review_pass = not review_problems and mutation_ok and scan_ok and frozen_intact
        ok = builder_ok and review_pass and not problems

        receipt = RealTaskReceipt(
            ok=ok,
            task_id=spec.task_id,
            spec_sha256=spec.spec_sha256,
            workspace=str(spec.workspace),
            problems=problems,
            builder_session_ids=builder_session_ids,
            review_session_ids=list(review_capture.session_ids),
            builder_exit_code=builder_exit_code,
            review_exit_code=review_capture.exit_code,
            verify_exit_code=verify_code,
            deliverable_manifest_sha256=inventory.manifest_sha256,
            post_review_manifest_sha256=post_manifest_sha256,
            freeze_manifest_path=str(freeze_manifest),
            review_pass=review_pass,
            completed_at=utc_now_iso(),
            builder_log_path=str(artifacts / "builder.stdout.txt"),
            review_log_path=str(review_stdout_log),
            verify_log_path=str(artifacts / "verify.log"),
        )
        receipt.write(artifacts / "receipt.json")
        write_resume_state(
            state_path,
            {
                "builder_ok": builder_ok,
                "spec_sha256": spec.spec_sha256,
                "workspace": str(spec.workspace),
                "builder_session_ids": builder_session_ids,
                "builder_exit_code": builder_exit_code,
                "builder_stdout_sha256": builder_stdout_sha,
                "deliverable_manifest_sha256": inventory.manifest_sha256,
                "freeze_manifest_sha256": freeze_sha,
                "post_review_manifest_sha256": post_manifest_sha256,
                "verify_exit_code": verify_code,
                "review_pass": receipt.review_pass,
                "receipt_ok": receipt.ok,
            },
        )
        run_result = RealTaskRunResult(receipt=receipt, problems=problems)
        return run_result  # noqa: RET504 — keep reference for config-drift finally hook
    finally:
        drift: list[str] = []
        if profile is not None:
            drift.extend(_validate_builder_config_hashes(spec, _observed_profile_hashes(profile)))
        if review_profile is not None:
            drift.extend(
                _validate_review_config_hashes(spec, _observed_profile_hashes(review_profile))
            )
        if drift:
            _merge_drift_into_receipt_file(artifacts, spec, drift)
            if run_result is not None:
                run_result.receipt.ok = False
                run_result.receipt.review_pass = False
                for item in drift:
                    if item not in run_result.problems:
                        run_result.problems.append(item)
                run_result.receipt.problems = list(run_result.problems)
        if sandbox is not None:
            sandbox.cleanup()
        if review_sandbox is not None:
            review_sandbox.cleanup()
        shutil.rmtree(config_parent, ignore_errors=True)
        if review_config_parent is not None:
            shutil.rmtree(review_config_parent, ignore_errors=True)
