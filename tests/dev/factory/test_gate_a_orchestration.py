"""Offline tests for Gate A orchestration helpers."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dev.factory.gate_a_mcp.checkout import canonical_evidence_root
from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME, MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness
from dev.factory.gate_a_trial.config_hashes import (
    MANDATORY_EFFECTIVE_CONFIG_KEYS,
    compare_config_inventory,
)
from dev.factory.gate_a_trial.orchestration import (
    GateAOrchestrationError,
    GateATurnCertification,
    ProfileBundle,
    assert_config_stable,
    certify_positive_turn,
    run_admitted_gate_a_turn,
)
from dev.factory.gate_a_trial.positive_binding import sha256_hex_of_file
from dev.factory.gate_a_trial.stream_json import StreamJsonSummary, ToolCallObservation
from dev.factory.order_scoped.binding import INTERNAL_STAGE_BRIEF_HASH, INTERNAL_STAGE_ORDER_ID
from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS


def test_assert_config_stable_detects_drift() -> None:
    problems = assert_config_stable(
        {"cli-config.json": "aaa"},
        {"cli-config.json": "bbb"},
        required_keys=frozenset({"cli-config.json"}),
    )
    assert problems
    assert "drift" in problems[0]


def test_assert_config_stable_flags_missing_post_key() -> None:
    problems = assert_config_stable(
        {"cli-config.json": "aaa"},
        {},
        required_keys=frozenset({"cli-config.json"}),
    )
    assert any("missing post-enable" in p for p in problems)


def test_run_admitted_gate_a_turn_expired_skips_mcp() -> None:
    def _forbid(*_a: object, **_k: object) -> object:
        raise AssertionError("MCP must not start when admission expired")

    with patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", side_effect=_forbid):
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            pass_cursor_api_key=False,
        )
    assert not result.ok
    assert any("expired" in p for p in result.problems)


def test_run_admitted_gate_a_turn_rejects_run_discovery_false() -> None:
    result = run_admitted_gate_a_turn(
        order_id=INTERNAL_STAGE_ORDER_ID,
        brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        run_discovery=False,
    )
    assert not result.ok
    assert any("run_discovery" in p for p in result.problems)


def test_compare_config_inventory_detects_approval_mutation() -> None:
    baseline = {".cursor/mcp-approvals.json": "aaa"}
    observed = {".cursor/mcp-approvals.json": "bbb"}
    problems = compare_config_inventory(baseline, observed)
    assert any("changed" in p for p in problems)


def test_certify_positive_turn_rejects_missing_brief_hash() -> None:
    stream = {
        "stdout": "{}",
        "stream_init_acceptable": True,
        "returncode": 0,
    }
    payload = {
        "ok": True,
        "order_id": INTERNAL_STAGE_ORDER_ID,
        "artifact_path": "/tmp/allowed.txt",
    }
    with patch(
        "dev.factory.gate_a_trial.orchestration.positive_gate_a_tool_satisfied",
        return_value=payload,
    ):
        cert = certify_positive_turn(
            stream,
            prestarted=None,
            witness=None,
            admitted_order_id=INTERNAL_STAGE_ORDER_ID,
            admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        )
    assert not cert.ok
    assert any("brief_hash" in p for p in cert.problems)


def test_run_admitted_gate_a_turn_rejects_non_canonical_brief() -> None:
    result = run_admitted_gate_a_turn(
        order_id=INTERNAL_STAGE_ORDER_ID,
        brief_hash="not-the-canonical-brief-hash",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert not result.ok
    assert any("canonical" in p for p in result.problems)


def test_run_admitted_gate_a_turn_rejects_non_fixture_order() -> None:
    result = run_admitted_gate_a_turn(
        order_id="real-production-order",
        brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert not result.ok
    assert any("synthetic fixture" in p for p in result.problems)


class _FakePrestarted:
    witness_nonce = "adapter-minted-nonce"
    server_pid = os.getpid()
    capability_token = "gate-a-capability-token-must-not-leak"
    control_dir = Path("/tmp/gate-a-control-unused")

    def cleanup(self) -> None:
        return None


def _fake_gate_a_sandbox(home: Path) -> object:
    from dev.factory.gate_a_trial.cursor_cli_sandbox import GateACursorCliSandbox

    profile = home / "gate-a-fake.sb"
    profile.write_text("(version 1)\n(allow default)\n", encoding="utf-8")
    return GateACursorCliSandbox(
        profile_path=profile,
        profile_sha256="0" * 64,
        home_dir=home,
    )


def _security_baseline_hashes(mandatory: dict[str, str]) -> dict[str, str]:
    return {
        **mandatory,
        "cli-config.json#steering": "baseline-steering",
        "cli-config.json#top_level_keys": "baseline-keys",
    }


def _ok_headless_stream() -> dict[str, object]:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "composer-2.5",
                    "apiKeySource": "env",
                },
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "OK",
                },
            ),
        ],
    )
    return {
        "stdout": stdout,
        "stream_init_acceptable": True,
        "stream_final_result_acceptable": True,
        "returncode": 0,
        "parsed_tool_calls": [],
    }


def _seed_warmup_project_files(home: Path, workspace: Path, *, digest: str = "A" * 44) -> None:
    from dev.factory.gate_a_trial.project_files import (
        MCP_APPROVALS_BASENAME,
        expected_cursor_project_slug,
    )

    slug = expected_cursor_project_slug(workspace)
    project_dir = home / ".cursor" / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / MCP_APPROVALS_BASENAME).write_text(
        json.dumps([digest]) + "\n",
        encoding="utf-8",
    )


def _fake_bundle(workspace: Path, config_dir: Path) -> ProfileBundle:
    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}
    return ProfileBundle(
        cursor_executable="/usr/bin/false",
        cursor_config_dir=config_dir,
        workspace=workspace,
        home_dir="/tmp/home",
        mcp_server_home="/tmp/mcp-home",
        profile={"workspace_mcp_config": str(workspace / ".cursor" / "mcp.json")},
        pre_enable_config_hashes=mandatory,
        discovery_env={},
        model_env={},
    )


def test_run_admitted_gate_a_turn_positive_synthetic_path_uses_adapter_witness(
    tmp_path: Path,
) -> None:
    """Reachable ok when adapter-owned MCP witness flows through without env forgery."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    fake = _FakePrestarted()
    cert = GateATurnCertification(
        ok=True, problems=[], allowed_txt_evidence=str(tmp_path / "allowed.txt")
    )
    (tmp_path / "allowed.txt").write_text("x", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    _seed_warmup_project_files(home, workspace)
    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}
    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
            return_value=config_dir,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.dispose_trial_workspace",
            return_value=None,
        ) as dispose_ws,
        patch(
            "dev.factory.gate_a_trial.orchestration.dispose_trial_config_dir",
            return_value=None,
        ) as dispose_cfg,
        patch(
            "dev.factory.gate_a_trial.orchestration.dispose_trial_path",
            return_value=None,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.dispose_isolated_home",
            return_value=None,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", return_value=fake),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
            return_value=_fake_bundle(workspace, config_dir),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.discover_and_gate_gate_a_mcp",
            return_value={
                "gate_passed": True,
                "effective_config_hashes_post_enable": dict(mandatory),
            },
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.load_qualified_witness",
        ) as load_witness,
        patch(
            "dev.factory.gate_a_trial.orchestration.run_headless_stream_json",
            return_value=_ok_headless_stream(),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.certify_positive_turn",
            return_value=cert,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prepare_gate_a_cursor_cli_sandbox",
            return_value=_fake_gate_a_sandbox(home),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.security_config_fingerprints",
            return_value=_security_baseline_hashes(mandatory),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.compare_post_warmup_gate_a_security",
            return_value=[],
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.compare_post_positive_gate_a_security",
            return_value=[],
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_matches_prestarted",
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_order_binding",
        ),
    ):
        import time

        from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness

        load_witness.return_value = QualifiedProcessWitness(
            witness_nonce=fake.witness_nonce,
            server_pid=fake.server_pid,
            listen_host="127.0.0.1",
            listen_port=9,
            mcp_url_path="/mcp",
            qualified_for_gate_a=True,
            minted_monotonic=time.monotonic(),
            settle_observed_seconds=90.0,
            order_id=INTERNAL_STAGE_ORDER_ID,
        )
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )
    assert result.ok
    assert result.certification is not None
    assert result.certification.ok
    dispose_ws.assert_called_once_with(workspace)
    dispose_cfg.assert_called_once_with(config_dir)


def test_run_admitted_gate_a_turn_rejects_cli_time_config_mutation(tmp_path: Path) -> None:
    """Policy drift after the positive CLI must fail even when discovery hashes are stale."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "cli-config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "approvalMode": "allowlist",
                "permissions": {"allow": [], "deny": []},
            },
        )
        + "\n",
        encoding="utf-8",
    )
    (config_dir / "mcp.json").write_text('{"mcpServers":{}}\n', encoding="utf-8")
    wmc = workspace / ".cursor" / "mcp.json"
    wmc.parent.mkdir(parents=True)
    wmc.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    fake = _FakePrestarted()
    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}
    stale_post_discovery = dict(mandatory)

    def _mutate_cli_config(
        _cursor_executable: str, prompt: str, **_kwargs: object
    ) -> dict[str, object]:
        if "OK" in prompt:
            return _ok_headless_stream()
        path = config_dir / "cli-config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["permissions"]["allow"] = ["Shell"]
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        return _ok_headless_stream()

    home = tmp_path / "home"
    home.mkdir()
    _seed_warmup_project_files(home, workspace)
    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
            return_value=config_dir,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", return_value=fake),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
            return_value=_fake_bundle(workspace, config_dir),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.discover_and_gate_gate_a_mcp",
            return_value={
                "gate_passed": True,
                "effective_config_hashes_post_enable": stale_post_discovery,
            },
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.load_qualified_witness",
        ) as load_witness,
        patch(
            "dev.factory.gate_a_trial.orchestration.run_headless_stream_json",
            side_effect=_mutate_cli_config,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prepare_gate_a_cursor_cli_sandbox",
            return_value=_fake_gate_a_sandbox(home),
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_matches_prestarted",
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_order_binding",
        ),
    ):
        import time

        from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness

        load_witness.return_value = QualifiedProcessWitness(
            witness_nonce=fake.witness_nonce,
            server_pid=fake.server_pid,
            listen_host="127.0.0.1",
            listen_port=9,
            mcp_url_path="/mcp",
            qualified_for_gate_a=True,
            minted_monotonic=time.monotonic(),
            settle_observed_seconds=90.0,
            order_id=INTERNAL_STAGE_ORDER_ID,
        )

        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )
    assert not result.ok
    assert any("drift" in p for p in result.problems)


def test_run_admitted_gate_a_turn_witness_mismatch_rejected_at_binding(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    fake = _FakePrestarted()

    home = tmp_path / "home"
    home.mkdir()
    _seed_warmup_project_files(home, workspace)
    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}
    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
            return_value=config_dir,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", return_value=fake),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
            return_value=_fake_bundle(workspace, config_dir),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.discover_and_gate_gate_a_mcp",
            return_value={
                "gate_passed": True,
                "effective_config_hashes_post_enable": dict(mandatory),
            },
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.load_qualified_witness",
        ) as load_witness,
        patch(
            "dev.factory.gate_a_trial.orchestration.prepare_gate_a_cursor_cli_sandbox",
            return_value=_fake_gate_a_sandbox(home),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.run_headless_stream_json",
            return_value=_ok_headless_stream(),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.security_config_fingerprints",
            return_value=_security_baseline_hashes(mandatory),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.compare_post_warmup_gate_a_security",
            return_value=[],
        ),
    ):
        import time

        from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness

        load_witness.return_value = QualifiedProcessWitness(
            witness_nonce="forged-not-from-handle",
            server_pid=fake.server_pid,
            listen_host="127.0.0.1",
            listen_port=9,
            mcp_url_path="/mcp",
            qualified_for_gate_a=True,
            minted_monotonic=time.monotonic(),
            settle_observed_seconds=90.0,
            order_id=INTERNAL_STAGE_ORDER_ID,
        )
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )
    assert not result.ok
    assert any("witness_nonce mismatch" in p for p in result.problems)


def test_run_admitted_gate_a_turn_rejects_post_cli_approval_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = tmp_path / "cfg"
    from dev.factory.gate_a_trial.cursor_profile import materialize_cursor_config_dir

    materialize_cursor_config_dir(
        config_dir,
        cursor_executable=sys.executable,
        python_executable=sys.executable,
        workspace=workspace,
    )
    fake = _FakePrestarted()
    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}

    def _noop_cli(_cursor_executable: str, prompt: str, **_kwargs: object) -> dict[str, object]:
        if "OK" in prompt:
            return _ok_headless_stream()
        return _ok_headless_stream()

    home = tmp_path / "home"
    home.mkdir()
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp-approvals.json").write_text("mutated\n", encoding="utf-8")

    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
            return_value=config_dir,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", return_value=fake),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
            return_value=_fake_bundle(workspace, config_dir),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.discover_and_gate_gate_a_mcp",
            return_value={
                "gate_passed": True,
                "effective_config_hashes_post_enable": dict(mandatory),
            },
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.load_qualified_witness",
        ) as load_witness,
        patch(
            "dev.factory.gate_a_trial.orchestration.run_headless_stream_json",
            side_effect=_noop_cli,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prepare_gate_a_cursor_cli_sandbox",
            return_value=_fake_gate_a_sandbox(home),
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_matches_prestarted",
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_order_binding",
        ),
    ):
        import time

        from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness

        load_witness.return_value = QualifiedProcessWitness(
            witness_nonce=fake.witness_nonce,
            server_pid=fake.server_pid,
            listen_host="127.0.0.1",
            listen_port=9,
            mcp_url_path="/mcp",
            qualified_for_gate_a=True,
            minted_monotonic=time.monotonic(),
            settle_observed_seconds=90.0,
            order_id=INTERNAL_STAGE_ORDER_ID,
        )
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )
    assert not result.ok
    assert any("mcp-approvals" in p or "forbidden home cursor" in p for p in result.problems)


def test_run_admitted_gate_a_turn_cleanup_failure_prevents_success(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    fake = _FakePrestarted()
    cert = GateATurnCertification(
        ok=True, problems=[], allowed_txt_evidence=str(tmp_path / "allowed.txt")
    )
    (tmp_path / "allowed.txt").write_text("x", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    _seed_warmup_project_files(home, workspace)
    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}

    def _fail_dispose(_path: Path) -> str:
        return "simulated workspace dispose failure"

    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
            return_value=config_dir,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.dispose_trial_workspace",
            side_effect=_fail_dispose,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", return_value=fake),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
            return_value=_fake_bundle(workspace, config_dir),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.discover_and_gate_gate_a_mcp",
            return_value={
                "gate_passed": True,
                "effective_config_hashes_post_enable": dict(mandatory),
            },
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.load_qualified_witness",
        ) as load_witness,
        patch(
            "dev.factory.gate_a_trial.orchestration.run_headless_stream_json",
            return_value=_ok_headless_stream(),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.certify_positive_turn",
            return_value=cert,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prepare_gate_a_cursor_cli_sandbox",
            return_value=_fake_gate_a_sandbox(home),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.security_config_fingerprints",
            return_value=_security_baseline_hashes(mandatory),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.compare_post_warmup_gate_a_security",
            return_value=[],
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.compare_post_positive_gate_a_security",
            return_value=[],
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_matches_prestarted",
        ),
        patch(
            "omnigent.factory.gate_a.admission.validate_witness_order_binding",
        ),
    ):
        import time

        from dev.factory.gate_a_mcp.process_witness import QualifiedProcessWitness

        load_witness.return_value = QualifiedProcessWitness(
            witness_nonce=fake.witness_nonce,
            server_pid=fake.server_pid,
            listen_host="127.0.0.1",
            listen_port=9,
            mcp_url_path="/mcp",
            qualified_for_gate_a=True,
            minted_monotonic=time.monotonic(),
            settle_observed_seconds=90.0,
            order_id=INTERNAL_STAGE_ORDER_ID,
        )
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )
    assert not result.ok
    assert any("cleanup" in p for p in result.problems)


def test_run_admitted_gate_a_turn_cleans_up_prestarted_on_error(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    fake = _FakePrestarted()
    cleaned = False

    def _cleanup() -> None:
        nonlocal cleaned
        cleaned = True

    fake.cleanup = _cleanup  # type: ignore[method-assign]

    home = tmp_path / "home"
    home.mkdir()
    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
            return_value=workspace,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
            return_value=config_dir,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
            return_value=home,
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
            return_value={"cursor_state_absent": True},
        ),
        patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", return_value=fake),
        patch(
            "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
            side_effect=GateAOrchestrationError("profile failed"),
        ),
    ):
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )
    assert not result.ok
    assert cleaned


def _certify_stream_ok() -> dict[str, object]:
    return {
        "stdout": "{}",
        "stream_init_acceptable": True,
        "returncode": 0,
    }


def _certify_summary_mock(*, mcp_result: object | None = None) -> StreamJsonSummary:
    result = (
        mcp_result
        if mcp_result is not None
        else {"artifact_path": "/should-be-redacted/allowed.txt"}
    )
    return StreamJsonSummary(
        tool_calls=[
            ToolCallObservation(
                name=TOOL_NAME,
                status="completed",
                call_id="adapter-success-mock",
                result={
                    "success": {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                        "isError": False,
                        "systemReminders": [],
                    }
                },
                mcp_server=MCP_SERVER_NAME,
            ),
        ],
        matched_2026_call_ids={"adapter-success-mock"},
    )


def _certify_mcp_payload(artifact: Path, *, nonce: str, pid: int) -> dict[str, object]:
    digest = sha256_hex_of_file(artifact)
    return {
        "ok": True,
        "gate_qualified": True,
        "child_ok": True,
        "artifact": BOUND_ARTIFACT_FILENAME,
        "artifact_path": str(artifact),
        "artifact_sha256": digest,
        "command_hash": "bcdcb760acf36ca6b580aeaa97310b31e0fbefd6c26366a51a674f850c4fa01d",
        "manifest_hash": "8213f576c7eb0c6fb84a45ad27b2a5013e4affa7c825775619066bffecb19f7c",
        "probe_sha256": "6ca023b3fb521d831788ca301a073fef6d9afc60dfaac3076406a463c3af5cff",
        "order_id": INTERNAL_STAGE_ORDER_ID,
        "brief_hash": INTERNAL_STAGE_BRIEF_HASH,
        "settle_observed_seconds": GATE_A_MIN_SETTLE_SECONDS,
        "witness_nonce": nonce,
        "server_pid": pid,
    }


def _jsonable_gate_a_result(obj: object) -> object:
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import fields

        return {f.name: _jsonable_gate_a_result(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _jsonable_gate_a_result(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable_gate_a_result(v) for v in obj]
    return obj


def _gate_a_result_blob(result: object) -> str:
    return json.dumps(_jsonable_gate_a_result(result))


def test_certify_positive_turn_unmocked_adapter_stream_parser(tmp_path: Path) -> None:
    """Adapter certification must accept a real stream-json MCP payload under a per-turn root."""
    turn_parent = tmp_path / "adapter-turn-evidence"
    turn_parent.mkdir()
    turn_root = canonical_evidence_root(turn_parent)
    archive_dir = turn_root / f"evidence-unmocked-{os.getpid()}"
    archive_dir.mkdir(parents=True)
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME
    artifact.write_text("allowed\n", encoding="utf-8")

    fake = _FakePrestarted()
    witness = QualifiedProcessWitness(
        witness_nonce=fake.witness_nonce,
        server_pid=fake.server_pid,
        listen_host="127.0.0.1",
        listen_port=9,
        mcp_url_path="/mcp",
        qualified_for_gate_a=True,
        minted_monotonic=time.monotonic(),
        settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
        order_id=INTERNAL_STAGE_ORDER_ID,
    )
    payload = _certify_mcp_payload(artifact, nonce=fake.witness_nonce, pid=fake.server_pid)
    call_id = "adapter-certify-unmocked"
    shell_id = "shell-deny-unmocked"
    command = "touch .gate_a_trial_shell_marker"
    shell_args = {"command": command}
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id,
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {},
                            }
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {},
                            },
                            "result": {
                                "success": {
                                    "content": [{"type": "text", "text": json.dumps(payload)}],
                                    "isError": False,
                                    "systemReminders": [],
                                }
                            },
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": shell_id,
                    "tool_call": {"shellToolCall": {"args": shell_args}},
                }
            ),
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": shell_id,
                    "tool_call": {
                        "shellToolCall": {
                            "args": shell_args,
                            "result": {"rejected": {"reason": "rejected by allowlist"}},
                        }
                    },
                }
            ),
        ]
    )
    cert = certify_positive_turn(
        {"stdout": stdout, "stream_init_acceptable": True, "returncode": 0},
        prestarted=fake,
        witness=witness,
        evidence_dirs_before=set(),
        admitted_order_id=INTERNAL_STAGE_ORDER_ID,
        admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        evidence_root_parent=turn_parent,
    )
    assert cert.ok
    assert cert.mcp_payload is not None
    assert "artifact_path" not in cert.mcp_payload
    assert cert.allowed_artifact_sha256 == sha256_hex_of_file(artifact)
    summary_blob = json.dumps(_jsonable_gate_a_result(cert.stream_summary))
    assert "artifact_path" not in summary_blob
    assert str(artifact) not in summary_blob


def test_certify_positive_turn_empty_before_rejects_shared_root_accepts_turn_root(
    tmp_path: Path,
) -> None:
    """Fresh per-turn evidence must not treat the shared global archive as dirs_before."""
    turn_parent = tmp_path / "adapter-turn-evidence"
    turn_parent.mkdir()
    shared_parent = tmp_path / "shared-global-evidence"
    shared_parent.mkdir()
    turn_root = canonical_evidence_root(turn_parent)
    shared_root = canonical_evidence_root(shared_parent)

    shared_dir = shared_root / f"evidence-shared-{os.getpid()}"
    shared_dir.mkdir()
    shared_artifact = shared_dir / BOUND_ARTIFACT_FILENAME
    shared_artifact.write_text("allowed\n", encoding="utf-8")

    turn_dir = turn_root / f"evidence-turn-{os.getpid()}"
    turn_dir.mkdir()
    turn_artifact = turn_dir / BOUND_ARTIFACT_FILENAME
    turn_artifact.write_text("allowed\n", encoding="utf-8")

    fake = _FakePrestarted()
    witness = QualifiedProcessWitness(
        witness_nonce=fake.witness_nonce,
        server_pid=fake.server_pid,
        listen_host="127.0.0.1",
        listen_port=9,
        mcp_url_path="/mcp",
        qualified_for_gate_a=True,
        minted_monotonic=time.monotonic(),
        settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
        order_id=INTERNAL_STAGE_ORDER_ID,
    )
    empty_before: set[str] = set()

    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.positive_gate_a_tool_satisfied",
            return_value=_certify_mcp_payload(
                shared_artifact,
                nonce=fake.witness_nonce,
                pid=fake.server_pid,
            ),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.parse_stream_json",
            return_value=_certify_summary_mock(),
        ),
    ):
        rejected = certify_positive_turn(
            _certify_stream_ok(),
            prestarted=fake,
            witness=witness,
            evidence_dirs_before=empty_before,
            admitted_order_id=INTERNAL_STAGE_ORDER_ID,
            admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            evidence_root_parent=turn_parent,
        )
    assert not rejected.ok
    assert any("outside evidence root" in p for p in rejected.problems)

    with (
        patch(
            "dev.factory.gate_a_trial.orchestration.positive_gate_a_tool_satisfied",
            return_value=_certify_mcp_payload(
                turn_artifact,
                nonce=fake.witness_nonce,
                pid=fake.server_pid,
            ),
        ),
        patch(
            "dev.factory.gate_a_trial.orchestration.parse_stream_json",
            return_value=_certify_summary_mock(),
        ),
    ):
        accepted = certify_positive_turn(
            _certify_stream_ok(),
            prestarted=fake,
            witness=witness,
            evidence_dirs_before=empty_before,
            admitted_order_id=INTERNAL_STAGE_ORDER_ID,
            admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            evidence_root_parent=turn_parent,
        )
    assert accepted.ok
    assert accepted.allowed_artifact_sha256 == sha256_hex_of_file(turn_artifact)
    assert accepted.allowed_txt_evidence == ""
    assert accepted.mcp_payload is not None
    assert "artifact_path" not in accepted.mcp_payload


def test_run_admitted_gate_a_turn_success_has_no_dangling_evidence_path(
    tmp_path: Path,
) -> None:
    """Successful adapter turns dispose per-turn evidence and return hash-only certification."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    turn_evidence_parent = tmp_path / "turn-evidence"
    turn_evidence_parent.mkdir()
    turn_root = canonical_evidence_root(turn_evidence_parent)
    archive_dir = turn_root / f"evidence-adapter-{os.getpid()}"
    artifact = archive_dir / BOUND_ARTIFACT_FILENAME

    fake = _FakePrestarted()
    home = tmp_path / "home"
    home.mkdir()
    _seed_warmup_project_files(home, workspace)
    mandatory = {key: f"hash-{key}" for key in MANDATORY_EFFECTIVE_CONFIG_KEYS}
    mcp_payload: dict[str, object] = {}
    expected_digest = ""
    disposed_turn_root: Path | None = None

    def _positive_cli_after_evidence_snapshot(
        *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        nonlocal expected_digest
        archive_dir.mkdir(parents=True, exist_ok=True)
        artifact.write_text("allowed\n", encoding="utf-8")
        expected_digest = sha256_hex_of_file(artifact)
        mcp_payload.clear()
        mcp_payload.update(
            _certify_mcp_payload(
                artifact,
                nonce=fake.witness_nonce,
                pid=fake.server_pid,
            ),
        )
        from dev.factory.gate_a_mcp.preflight import preflight_ready_marker_path

        mcp_home = home / "mcp-stdio-server-home"
        preflight_ready_marker_path(mcp_home).write_text(
            json.dumps(
                {
                    "qualified_for_gate_a": True,
                    "server_pid": fake.server_pid,
                    "settle_observed_seconds": GATE_A_MIN_SETTLE_SECONDS,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return _certify_stream_ok()

    def _headless_warmup_then_positive(
        _cursor_executable: str, prompt: str, **_kwargs: object
    ) -> dict[str, object]:
        if "Do not use any tools" in prompt:
            return _ok_headless_stream()
        return _positive_cli_after_evidence_snapshot()

    def _capture_dispose(path: Path, *, label: str = "") -> str | None:
        nonlocal disposed_turn_root
        if label == "turn evidence root":
            disposed_turn_root = path
            if path.exists():
                import shutil

                shutil.rmtree(path)
        return None

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_workspace",
                return_value=workspace,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_disposable_trial_config_dir",
                return_value=config_dir,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration._materialize_turn_evidence_root",
                return_value=turn_evidence_parent,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.dispose_trial_path",
                side_effect=_capture_dispose,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.dispose_trial_workspace", return_value=None
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.dispose_trial_config_dir",
                return_value=None,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.dispose_isolated_home", return_value=None
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_isolated_home",
                return_value=home,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.prove_isolated_home_empty",
                return_value={"cursor_state_absent": True},
            ),
        )
        stack.enter_context(
            patch("dev.factory.gate_a_trial.orchestration.start_bound_mcp", return_value=fake)
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.materialize_order_profile",
                return_value=_fake_bundle(workspace, config_dir),
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.discover_and_gate_gate_a_mcp",
                return_value={
                    "gate_passed": True,
                    "effective_config_hashes_post_enable": dict(mandatory),
                },
            ),
        )
        load_witness = stack.enter_context(
            patch("dev.factory.gate_a_trial.orchestration.load_qualified_witness"),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.run_headless_stream_json",
                side_effect=_headless_warmup_then_positive,
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.positive_gate_a_tool_satisfied",
                side_effect=lambda *_a, **_k: dict(mcp_payload),
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.parse_stream_json",
                side_effect=lambda _stdout: _certify_summary_mock(mcp_result=dict(mcp_payload)),
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.prepare_gate_a_cursor_cli_sandbox",
                return_value=_fake_gate_a_sandbox(home),
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.security_config_fingerprints",
                return_value=_security_baseline_hashes(mandatory),
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.compare_post_warmup_gate_a_security",
                return_value=[],
            ),
        )
        stack.enter_context(
            patch(
                "dev.factory.gate_a_trial.orchestration.compare_post_positive_gate_a_security",
                return_value=[],
            ),
        )
        stack.enter_context(
            patch("omnigent.factory.gate_a.admission.validate_witness_matches_prestarted"),
        )
        stack.enter_context(
            patch("omnigent.factory.gate_a.admission.validate_witness_order_binding"),
        )
        load_witness.return_value = QualifiedProcessWitness(
            witness_nonce=fake.witness_nonce,
            server_pid=fake.server_pid,
            listen_host="127.0.0.1",
            listen_port=9,
            mcp_url_path="/mcp",
            qualified_for_gate_a=True,
            minted_monotonic=time.monotonic(),
            settle_observed_seconds=GATE_A_MIN_SETTLE_SECONDS,
            order_id=INTERNAL_STAGE_ORDER_ID,
        )
        result = run_admitted_gate_a_turn(
            order_id=INTERNAL_STAGE_ORDER_ID,
            brief_hash=INTERNAL_STAGE_BRIEF_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            pass_cursor_api_key=True,
        )

    assert result.ok
    assert result.prestarted is None
    assert result.certification is not None
    cert = result.certification
    assert cert.allowed_txt_evidence == ""
    assert cert.allowed_artifact_sha256 == expected_digest
    assert not artifact.is_file()
    assert cert.mcp_payload is not None
    assert "artifact_path" not in cert.mcp_payload
    assert disposed_turn_root == turn_evidence_parent
    assert not turn_evidence_parent.exists()
    blob = _gate_a_result_blob(result)
    assert fake.capability_token not in blob
    assert str(artifact) not in blob
    assert cert.stream_summary is not None
    summary_blob = json.dumps(_jsonable_gate_a_result(cert.stream_summary))
    assert "artifact_path" not in summary_blob
    assert str(artifact) not in summary_blob
