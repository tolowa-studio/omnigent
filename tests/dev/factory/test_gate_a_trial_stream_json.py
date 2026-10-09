"""Unit tests for Gate A trial stream-json parsing (no Cursor subprocess)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from dev.factory.gate_a_mcp.checkout import canonical_evidence_root
from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME
from dev.factory.gate_a_trial.stream_json import (
    GateAPositiveMcpPayloadMode,
    headless_stream_init_acceptable,
    negative_attempt_satisfied,
    parse_stream_json,
    positive_gate_a_tool_satisfied,
)
from dev.factory.order_scoped.binding import INTERNAL_STAGE_BRIEF_HASH, INTERNAL_STAGE_ORDER_ID
from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS


def _line(event: dict) -> str:
    return json.dumps(event)


def _gate_a_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_local_gate_a_trial_transcript(transcript_name: str) -> str:
    path = _gate_a_repo_root() / "dev/factory/gate_a_trial/transcripts" / transcript_name
    if not path.is_file():
        pytest.skip(
            "local-evidence-missing: Gate A historical CLI transcript not present "
            f"({transcript_name}); ignored under dev/factory/gate_a_trial/transcripts/"
        )
    return path.read_text(encoding="utf-8")


def test_parse_stream_json_init_model() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-4-sonnet",
                    "apiKeySource": "env",
                }
            ),
            _line({"type": "assistant", "message": {}}),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.init_model == "claude-4-sonnet"
    assert summary.api_key_source == "env"
    assert headless_stream_init_acceptable(summary)[0]


def test_native_shell_write_fail_closed_on_successful_write() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-w-bad",
                    "tool_call": {
                        "editToolCall": {
                            "args": {"path": "x.txt"},
                            "result": {"ok": True},
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()


def test_negative_shell_requires_attempt_and_permission_denial() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "name": "Shell",
                    "status": "error",
                    "result": "Permission denied by allowlist",
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.legacy_tool_call_events == 1
    assert not negative_attempt_satisfied("deny_shell_marker", summary)
    assert not summary.native_shell_write_calls_denied()


def test_negative_shell_refusal_without_tool_call_not_satisfied() -> None:
    stdout = _line(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I cannot"}]}}
    )
    summary = parse_stream_json(stdout)
    assert not negative_attempt_satisfied("deny_shell_marker", summary)


def test_positive_gate_a_tool_payload() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-mcp-payload",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-mcp-payload",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": _mcp_success_result_payload(payload),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    parsed = positive_gate_a_tool_satisfied(summary, TOOL_NAME)
    assert parsed is not None
    assert parsed["artifact_path"] == _allowed_payload()["artifact_path"]


def test_v11_cursor_2026_mcp_completed_string_result_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-v11-mcp-string",
            "tool_call": {
                "mcpToolCall": {
                    "result": json.dumps(payload),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v11_cursor_2026_mcp_completed_list_result_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-v11-mcp-list",
            "tool_call": {
                "mcpToolCall": {
                    "result": [{"type": "text", "text": json.dumps(payload)}],
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_parse_stream_json_cursor_2026_mcp_started_completed() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-mcp-1",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                                "toolName": TOOL_NAME,
                                "args": {},
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-mcp-1",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": _mcp_success_result_payload(payload),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    parsed = positive_gate_a_tool_satisfied(summary, TOOL_NAME)
    assert parsed is not None
    assert parsed["artifact_path"] == _allowed_payload()["artifact_path"]


def test_parse_stream_json_cursor_2026_shell_denied() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-sh-1",
                    "tool_call": {
                        "shellToolCall": {"args": {"command": "touch x"}},
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-sh-1",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "rejected": {
                                    "reason": "Permission denied by allowlist",
                                }
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert negative_attempt_satisfied("deny_shell_marker", summary)


def test_negative_malformed_gate_a_tool_schema_validation_error() -> None:
    err_text = (
        f"Error: Error executing tool {TOOL_NAME}: "
        "1 validation error for call[execute_internal_stage_order]\n"
        "command\n"
        "  Extra inputs are not permitted "
        "[type=extra_forbidden, input_value='id', input_type=str]"
    )
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-bad-schema",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {"command": "id"},
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-bad-schema",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {"command": "id"},
                            },
                            "result": _mcp_success_result_text(err_text),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert negative_attempt_satisfied("deny_malformed_gate_a_tool", summary)


def test_negative_write_cursor_2026_write_permission_denied() -> None:
    write_args = {"path": ".gate_a_trial_write_marker", "streamContent": "trial\n"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-w-1",
                    "tool_call": {"editToolCall": {"args": write_args}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-w-1",
                    "tool_call": {
                        "editToolCall": {
                            "args": write_args,
                            "result": {
                                "writePermissionDenied": {
                                    "path": ".gate_a_trial_write_marker",
                                    "error": "Blocked by permissions configuration",
                                    "isReadonly": False,
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert negative_attempt_satisfied("deny_write_marker", summary)


def test_negative_malformed_gate_a_tool_cursor_2026_rejected() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-bad-1",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                                "toolName": TOOL_NAME,
                                "args": {"command": "id"},
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-bad-1",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": {
                                "rejected": {
                                    "reason": (
                                        f"Error executing tool {TOOL_NAME}: "
                                        "forbidden argument: command"
                                    ),
                                }
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert negative_attempt_satisfied("deny_malformed_gate_a_tool", summary)


def test_adversarial_shell_sandbox_text_not_allowlist_denial() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-sh-sandbox",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": "touch .gate_a_trial_shell_marker"},
                            "result": "zsh:1: permission denied: .gate_a_trial_shell_marker",
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()
    assert not negative_attempt_satisfied("deny_shell_marker", summary)


def test_adversarial_running_shell_fails_native_gate() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-sh-run",
                    "tool_call": {"shellToolCall": {"args": {"command": "sleep 60"}}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-deny",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "rejected": {"reason": "not allowed by tool policy"},
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_unknown_tool_call_variant_fails_native_gate() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-unknown",
                    "tool_call": {"futureToolCall": {"args": {}}},
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_mcp_error_status_with_ok_payload_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-mcp-err",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": {
                                "rejected": {
                                    "reason": "ignored",
                                },
                                "success": {
                                    "content": [{"type": "text", "text": json.dumps(payload)}],
                                },
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_positive_mcp_with_successful_write_side_effect() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-w-leak",
                    "tool_call": {
                        "editToolCall": {
                            "args": {"path": "leak.txt"},
                            "result": {"bytesWritten": 3},
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "name": "mcp",
                    "status": "completed",
                    "args": {
                        "providerIdentifier": "omnigent-factory-gate-a-mcp",
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                    "result": [{"type": "text", "text": json.dumps(payload)}],
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_completed_missing_call_id_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "shellToolCall": {
                    "result": {"rejected": {"reason": "not allowed by tool policy"}},
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()
    assert summary.attempted_tool_named("Shell") is None


def test_adversarial_started_non_dict_tool_call_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "call-bad-shape",
            "tool_call": "not-a-dict",
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_dual_recognized_variant_keys_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-dual",
            "tool_call": {
                "shellToolCall": {
                    "result": {"rejected": {"reason": "not allowed by tool policy"}},
                },
                "mcpToolCall": {
                    "result": {
                        "success": {"content": [{"type": "text", "text": "{}"}]},
                    }
                },
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_variant_unknown_sibling_key_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-sibling",
            "tool_call": {
                "shellToolCall": {
                    "result": {"rejected": {"reason": "not allowed by tool policy"}},
                },
                "shadowToolCall": {"args": {}},
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_shell_rejected_plus_failure_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-sh-mixed",
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {"reason": "not allowed by tool policy"},
                        "failure": {"message": "also ran"},
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()
    assert not negative_attempt_satisfied("deny_shell_marker", summary)


def test_adversarial_mcp_rejected_plus_success_is_unparsed() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-mixed",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "rejected": {"reason": "ignored"},
                        "success": {
                            "content": [{"type": "text", "text": json.dumps(payload)}],
                        },
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_invalid_json_line_fails_closed() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-4-sonnet",
                    "apiKeySource": "env",
                }
            ),
            "not-json{",
            _line(
                {
                    "type": "tool_call",
                    "name": "mcp",
                    "status": "completed",
                    "args": {
                        "providerIdentifier": "omnigent-factory-gate-a-mcp",
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                    "result": [{"type": "text", "text": json.dumps(payload)}],
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert any(v.get("reason") == "invalid_json" for v in summary.unparsed_tool_call_variants)
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_non_dict_json_line_fails_closed() -> None:
    stdout = "\n".join(
        [
            '["not","a","stream","event"]',
            _line(
                {
                    "type": "tool_call",
                    "name": "Shell",
                    "status": "error",
                    "result": "Permission denied by allowlist",
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert any(v.get("reason") == "non_dict_json" for v in summary.unparsed_tool_call_variants)
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_unrecognized_subtype_with_variant_not_legacy() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "progress",
            "call_id": "call-prog",
            "tool_call": {
                "shellToolCall": {
                    "args": {"command": "touch leak"},
                    "result": {"success": {"exitCode": 0}},
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert summary.unparsed_tool_call_variants[0].get("reason") == "unrecognized_tool_call_subtype"
    assert not summary.native_shell_write_calls_denied()
    assert summary.attempted_tool_named("Shell") is None


def test_adversarial_mcp_started_shell_completed_same_call_id() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-flip",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                                "toolName": TOOL_NAME,
                                "args": {},
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-flip",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {"success": {"exitCode": 0, "stdout": "wrote marker"}},
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert any(
        v.get("started_kind") == "mcp" and v.get("completed_kind") == "shell"
        for v in summary.unparsed_tool_call_variants
    )
    assert not summary.native_shell_write_calls_denied()
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_shell_started_mcp_completed_same_call_id() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-flip-2",
                    "tool_call": {"shellToolCall": {"args": {"command": "id"}}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-flip-2",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": _mcp_success_result_payload(payload),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert any(
        v.get("started_kind") == "shell" and v.get("completed_kind") == "mcp"
        for v in summary.unparsed_tool_call_variants
    )
    assert not summary.native_shell_write_calls_denied()
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_shell_success_plus_policy_message_is_not_denial() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-sh-hide",
            "tool_call": {
                "shellToolCall": {
                    "args": {"command": "touch .gate_a_trial_shell_marker"},
                    "result": {
                        "success": {"exitCode": 0, "stdout": "ran"},
                        "message": "rejected by policy",
                    },
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()
    assert not negative_attempt_satisfied("deny_shell_marker", summary)


def test_adversarial_shell_iserror_with_exit_code_is_not_denial() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-sh-iserror",
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "isError": True,
                        "stdout": "rejected by policy",
                        "exitCode": 0,
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_mcp_success_plus_failure_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-arms",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": "omnigent-factory-gate-a-mcp",
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                    "result": {
                        "failure": {"message": "spawn failed"},
                        "success": {"content": [{"type": "text", "text": json.dumps(payload)}]},
                    },
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_duplicate_start_cannot_replace_running_shell() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-reuse",
                    "tool_call": {"shellToolCall": {"args": {"command": "touch leak"}}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-reuse",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                                "toolName": TOOL_NAME,
                                "args": {},
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-reuse",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": {
                                "success": {
                                    "content": [{"type": "text", "text": json.dumps(payload)}]
                                }
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_progress_list_tool_call_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "progress",
            "tool_call": [
                {"shellToolCall": {"result": {"success": {"exitCode": 0, "stdout": "ran"}}}}
            ],
        }
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not summary.native_shell_write_calls_denied()
    assert summary.attempted_tool_named("Shell") is None


def test_adversarial_write_ok_true_beside_rejected_is_not_denial() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-w-ok",
                    "tool_call": {
                        "editToolCall": {
                            "args": {"path": "leak.txt", "streamContent": "trial\n"},
                            "result": {
                                "ok": True,
                                "rejected": {"reason": "Permission denied by allowlist"},
                            },
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-mcp-ok",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                                "toolName": TOOL_NAME,
                                "args": {},
                            },
                            "result": {
                                "success": {
                                    "content": [{"type": "text", "text": json.dumps(payload)}]
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not summary.native_shell_write_calls_denied()
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_rejected_dict_with_nested_stdout_is_not_denial() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-sh-nested",
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {
                            "reason": "Permission denied by allowlist",
                            "stdout": "ran",
                            "exitCode": 0,
                        }
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_mcp_error_arm_beside_success_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-error-arm",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "error": {"errorMessage": "spawn failed"},
                        "success": {"content": [{"type": "text", "text": json.dumps(payload)}]},
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_mcp_success_content_with_nested_error_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-in-success-err",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "success": {
                            "content": [{"type": "text", "text": json.dumps(payload)}],
                            "error": {"errorMessage": "spawn failed"},
                        }
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_mcp_success_content_with_denial_arm_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-in-success-rej",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "success": {
                            "content": [{"type": "text", "text": json.dumps(payload)}],
                            "rejected": {"reason": "Permission denied by allowlist"},
                        }
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_mcp_top_level_policy_text_beside_success_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-policy-sibling",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "success": {
                            "content": [{"type": "text", "text": json.dumps(payload)}],
                        },
                        "message": "rejected by policy",
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_mcp_ok_false_beside_success_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-ok-false",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "ok": False,
                        "success": {
                            "content": [{"type": "text", "text": json.dumps(payload)}],
                        },
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_legacy_mcp_error_field_with_clean_payload_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "name": "mcp",
            "status": "completed",
            "error": "spawn failed",
            "args": {
                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                "toolName": TOOL_NAME,
                "args": {},
            },
            "result": [{"type": "text", "text": json.dumps(payload)}],
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert summary.unparsed_tool_call_variants


def test_adversarial_mcp_content_non_text_sibling_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-mixed-content",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "success": {
                            "content": [
                                {"type": "text", "text": json.dumps(payload)},
                                {"type": "error", "text": "spawn failed"},
                            ]
                        }
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_mcp_iserror_text_block_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-iserror-block",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "success": {
                            "content": [
                                {"type": "text", "text": json.dumps(payload), "isError": True},
                            ]
                        }
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_mcp_ok_true_with_error_field_in_payload_is_not_positive() -> None:
    payload = {
        "ok": True,
        "artifact": "allowed.txt",
        "artifact_path": "/tmp/wt/allowed.txt",
        "error": "spawn failed",
    }
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-payload-error",
            "tool_call": {
                "mcpToolCall": {
                    "result": _mcp_success_result_payload(payload),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adversarial_rejected_with_nested_output_stdout_is_not_denial() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-sh-output-nested",
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {
                            "reason": "Permission denied by allowlist",
                            "output": {"stdout": "ran", "exitCode": 0},
                        }
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()


def test_adversarial_mcp_second_content_block_is_not_positive() -> None:
    payload = {"ok": True, "artifact": "allowed.txt", "artifact_path": "/tmp/wt/allowed.txt"}
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-mcp-blocks",
            "tool_call": {
                "mcpToolCall": {
                    "result": {
                        "success": {
                            "content": [
                                {"type": "text", "text": json.dumps(payload)},
                                {
                                    "type": "text",
                                    "text": json.dumps({"ok": False, "error": "spawn failed"}),
                                },
                            ]
                        }
                    }
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def _normalize_mcp_tool_result_for_tests(result: dict) -> dict:
    if set(result.keys()) != {"success"}:
        return result
    success = result.get("success")
    if not isinstance(success, dict):
        return result
    normalized = dict(success)
    if "isError" not in normalized:
        normalized["isError"] = False
    if "systemReminders" not in normalized:
        normalized["systemReminders"] = []
    return {"success": normalized}


def _mcp_2026_nested_text_success_payload(payload: dict) -> dict:
    return {
        "success": {
            "content": [{"text": {"text": json.dumps(payload)}}],
            "isError": False,
            "systemReminders": [],
        }
    }


def _mcp_success_result_payload(payload: dict) -> dict:
    return _normalize_mcp_tool_result_for_tests(
        {"success": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
    )


def _mcp_success_result_text(text: str) -> dict:
    return _normalize_mcp_tool_result_for_tests(
        {"success": {"content": [{"type": "text", "text": text}]}}
    )


def _mcp_variant_completed(call_id: str, result: dict) -> str:
    result = _normalize_mcp_tool_result_for_tests(result)
    return _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": call_id,
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": "omnigent-factory-gate-a-mcp",
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                    "result": result,
                }
            },
        }
    )


_UNIT_WITNESS_NONCE = "stream-json-unit-witness-nonce"
_UNIT_SERVER_PID = 424_242


def _adapter_turn_payload(
    artifact: Path,
    *,
    brief_hash: str = INTERNAL_STAGE_BRIEF_HASH,
) -> dict:
    artifact.write_text("allowed\n", encoding="utf-8")
    base = dict(_allowed_payload())
    base["artifact_path"] = str(artifact)
    import hashlib

    base["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    base["brief_hash"] = brief_hash
    return base


def _adapter_positive_stdout(
    payload: dict,
    *,
    call_id: str = "adapter-turn-mcp",
) -> str:
    return "\n".join(
        [
            _v16_clean_mcp_started_line(call_id),
            _line(
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
                            "result": _mcp_success_result_payload(payload),
                        }
                    },
                }
            ),
        ]
    )


def _allowed_payload() -> dict:
    artifact_path = str(canonical_evidence_root() / "evidence-unit" / "allowed.txt")
    return {
        "ok": True,
        "artifact": "allowed.txt",
        "artifact_path": artifact_path,
        "artifact_sha256": "fda0c6dbbd27bb4682f2931877d6c3e6b408e65b07feb9b4c1e9cc29b1a2cda2",
        "child_ok": True,
        "command_hash": "bcdcb760acf36ca6b580aeaa97310b31e0fbefd6c26366a51a674f850c4fa01d",
        "gate_qualified": True,
        "manifest_hash": "8213f576c7eb0c6fb84a45ad27b2a5013e4affa7c825775619066bffecb19f7c",
        "order_id": INTERNAL_STAGE_ORDER_ID,
        "probe_sha256": "6ca023b3fb521d831788ca301a073fef6d9afc60dfaac3076406a463c3af5cff",
        "server_pid": _UNIT_SERVER_PID,
        "settle_observed_seconds": float(GATE_A_MIN_SETTLE_SECONDS),
        "witness_nonce": _UNIT_WITNESS_NONCE,
    }


def test_v31_positive_payload_without_witness_fields_fails_clean_shape() -> None:
    payload = dict(_allowed_payload())
    del payload["witness_nonce"]
    stdout = _mcp_variant_completed("call-v31-no-nonce", _mcp_success_result_payload(payload))
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_mcp_success_with_stdout_on_success_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v8-exec-success",
        {
            "success": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "stdout": "ran",
                "exitCode": 0,
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_mcp_success_with_execution_evidence_sibling_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v8-exec-sibling",
        {
            "success": {"content": [{"type": "text", "text": json.dumps(payload)}]},
            "stdout": "ran",
            "exitCode": 0,
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_mcp_success_with_nested_output_stdout_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v8-exec-nested-output",
        {
            "success": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "output": {"stdout": "ran", "exitCode": 0},
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_mcp_ok_false_inside_success_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v8-ok-in-success",
        {
            "success": {
                "ok": False,
                "content": [{"type": "text", "text": json.dumps(payload)}],
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_legacy_clean_result_with_policy_sibling_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "name": "mcp",
            "status": "completed",
            "message": "rejected by policy",
            "args": {
                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                "toolName": TOOL_NAME,
                "args": {},
            },
            "result": [{"type": "text", "text": json.dumps(payload)}],
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert summary.unparsed_tool_call_variants


def test_v8_grok_legacy_result_dict_with_stdout_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "name": "mcp",
            "status": "completed",
            "args": {
                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                "toolName": TOOL_NAME,
                "args": {},
            },
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "stdout": "ran",
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert summary.unparsed_tool_call_variants


def test_v8_grok_mcp_text_block_with_error_sibling_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v8-block-error",
        {
            "success": {
                "content": [
                    {"type": "text", "text": json.dumps(payload), "error": "spawn failed"},
                ]
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_mcp_success_nested_error_dict_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v8-nested-error",
        {
            "success": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "nested": {"error": "spawn failed"},
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_mcp_success_nested_rejected_dict_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v8-nested-rejected",
        {
            "success": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "nested": {"rejected": {"reason": "Permission denied by allowlist"}},
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v8_grok_clean_one_block_mcp_still_positive() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-v8-clean-mcp",
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
            _mcp_variant_completed(
                "call-v8-clean-mcp",
                {"success": {"content": [{"type": "text", "text": json.dumps(payload)}]}},
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()


def test_v8_grok_clean_native_denial_still_recognized() -> None:
    command = "touch .gate_a_trial_shell_marker"
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-v8-shell-deny",
                    "tool_call": {
                        "shellToolCall": {"args": {"command": command}},
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-v8-shell-deny",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": command},
                            "result": {"rejected": {"reason": "Permission denied by allowlist"}},
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.native_shell_write_calls_denied()
    assert negative_attempt_satisfied("deny_shell_marker", summary)


def test_v9_grok_mcp_success_with_wrapped_side_sibling_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v9-side",
        {
            "success": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
            },
            "side": {
                "stdout": "ran",
                "exitCode": 0,
                "error": "spawn failed",
                "ok": False,
                "rejected": {"reason": "Permission denied by allowlist"},
            },
        },
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v9_grok_mcp_success_with_top_level_content_sibling_is_unparsed() -> None:
    payload = _allowed_payload()
    block = {"type": "text", "text": json.dumps(payload)}
    stdout = _mcp_variant_completed(
        "call-v9-content-sibling",
        {
            "success": {"content": [block]},
            "content": [block],
        },
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v9_grok_mcp_success_with_second_content_block_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v9-second-block",
        {
            "success": {
                "content": [
                    {"type": "text", "text": json.dumps(payload)},
                    {"type": "text", "text": json.dumps(payload)},
                ]
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v9_grok_legacy_clean_result_with_side_sibling_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "name": "mcp",
            "status": "completed",
            "args": {
                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                "toolName": TOOL_NAME,
                "args": {},
            },
            "result": [{"type": "text", "text": json.dumps(payload)}],
            "side": {
                "stdout": "ran",
                "exitCode": 0,
                "error": "spawn failed",
                "ok": False,
                "rejected": {"reason": "Permission denied by allowlist"},
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v9_grok_legacy_result_with_extra_content_array_sibling_is_unparsed() -> None:
    payload = _allowed_payload()
    block = {"type": "text", "text": json.dumps(payload)}
    stdout = _line(
        {
            "type": "tool_call",
            "name": "mcp",
            "status": "completed",
            "args": {
                "providerIdentifier": "omnigent-factory-gate-a-mcp",
                "toolName": TOOL_NAME,
                "args": {},
            },
            "result": [block],
            "content": [block],
        }
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def _side_channel_denial() -> dict:
    return {
        "stdout": "ran",
        "error": "spawn failed",
        "rejected": {"reason": "Permission denied by allowlist"},
    }


def test_v10_mcp_completed_block_side_sibling_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-v10-mcp-block-side",
            "tool_call": {
                "mcpToolCall": {
                    "result": _normalize_mcp_tool_result_for_tests(
                        {
                            "success": {
                                "content": [{"type": "text", "text": json.dumps(payload)}],
                            }
                        }
                    ),
                    "side": _side_channel_denial(),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v10_mcp_completed_outer_event_side_sibling_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-v10-mcp-event-side",
            "side": _side_channel_denial(),
            "tool_call": {
                "mcpToolCall": {
                    "result": _normalize_mcp_tool_result_for_tests(
                        {
                            "success": {
                                "content": [{"type": "text", "text": json.dumps(payload)}],
                            }
                        }
                    ),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v10_mcp_started_block_side_sibling_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "call-v10-mcp-start-block-side",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": "omnigent-factory-gate-a-mcp",
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                    "side": _side_channel_denial(),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v10_mcp_started_outer_event_sibling_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "call-v10-mcp-start-event-side",
            "traceId": "cursor-metadata",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": "omnigent-factory-gate-a-mcp",
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v10_shell_completed_block_side_sibling_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-v10-shell-block-side",
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {"reason": "Permission denied by allowlist"},
                    },
                    "side": _side_channel_denial(),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()
    assert not negative_attempt_satisfied("deny_shell_marker", summary)


def test_v10_edit_started_event_sibling_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "call-v10-write-start-side",
            "model": "claude-4-sonnet",
            "tool_call": {
                "editToolCall": {
                    "args": {"path": ".gate_a_trial_write_marker", "streamContent": "trial\n"},
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert not summary.native_shell_write_calls_denied()


def test_v12_mcp_empty_started_wrong_completed_identity_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-v12-empty-start",
                    "tool_call": {"mcpToolCall": {}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-v12-empty-start",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "other-server",
                                "toolName": "other-tool",
                                "args": {},
                            },
                            "result": {
                                "success": {
                                    "content": [{"type": "text", "text": json.dumps(payload)}],
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v12_mcp_bound_started_completed_identity_mismatch_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-v12-mismatch",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-v12-mismatch",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "other-server",
                                "toolName": "other-tool",
                                "args": {},
                            },
                            "result": _mcp_success_result_payload(payload),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert any(
        v.get("reason") == "mcp_started_completed_identity_mismatch"
        for v in summary.unparsed_tool_call_variants
    )
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v12_mcp_completed_only_wrong_server_tool_is_not_positive() -> None:
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-v12-wrong-only",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": "other-server",
                        "toolName": "other-tool",
                        "args": {},
                    },
                    "result": _normalize_mcp_tool_result_for_tests(
                        {
                            "success": {
                                "content": [{"type": "text", "text": json.dumps(payload)}],
                            }
                        }
                    ),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v12_mcp_bound_started_completed_without_repeat_args_still_positive() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-v12-bound-pair",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-v12-bound-pair",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": _mcp_success_result_payload(payload),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    parsed = positive_gate_a_tool_satisfied(summary, TOOL_NAME)
    assert parsed is not None
    assert parsed["artifact"] == "allowed.txt"
    assert summary.native_shell_write_calls_denied()


def _v12_grok_decisive_stream() -> str:
    payload = _allowed_payload()
    return "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "mcp-clean",
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
                                }
                            },
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-hidden",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "rejected": {
                                    "reason": "ok",
                                    "error": [
                                        {
                                            "stdout": "ran",
                                            "exitCode": 0,
                                            "ok": True,
                                            "failure": {"output": "ran"},
                                        }
                                    ],
                                }
                            }
                        }
                    },
                }
            ),
        ]
    )


def test_v12_grok_nested_list_execution_under_rejected_is_unparsed() -> None:
    summary = parse_stream_json(_v12_grok_decisive_stream())
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v12_grok_shell_list_result_with_hidden_execution_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "mcp-clean",
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
                                }
                            },
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-list",
                    "tool_call": {
                        "shellToolCall": {
                            "result": [
                                {
                                    "type": "text",
                                    "text": "rejected by allowlist",
                                    "side": {"stdout": "ran", "exitCode": 0, "ok": True},
                                }
                            ]
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v12_grok_rejected_reason_ok_without_policy_is_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-rejected-ok",
            "tool_call": {
                "shellToolCall": {
                    "result": {"rejected": {"reason": "ok"}},
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not summary.native_shell_write_calls_denied()


def test_v12_grok_native_started_completed_command_mismatch_is_unparsed() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-sh-mismatch",
                    "tool_call": {
                        "shellToolCall": {"args": {"command": "echo safe"}},
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-sh-mismatch",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": "touch leak.txt"},
                            "result": {
                                "rejected": {"reason": "Permission denied by allowlist"},
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert any(
        v.get("reason") == "native_started_completed_args_mismatch"
        for v in summary.unparsed_tool_call_variants
    )
    assert not summary.native_shell_write_calls_denied()
    assert not negative_attempt_satisfied("deny_shell_marker", summary)


def test_v12_grok_clean_mcp_and_structured_shell_denial_still_certify() -> None:
    stdout = "\n".join(_v17_clean_conjunction_lines())
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()
    assert negative_attempt_satisfied("deny_shell_marker", summary)


def test_v13_mcp_bound_started_completed_identity_still_positive() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call-v13-bound-pair",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-v13-bound-pair",
                    "tool_call": {
                        "mcpToolCall": {
                            "result": _mcp_success_result_payload(payload),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    parsed = positive_gate_a_tool_satisfied(summary, TOOL_NAME)
    assert parsed is not None
    assert parsed["artifact_path"] == _allowed_payload()["artifact_path"]


def test_v14_other_type_completed_hides_shell_success_fails_closed() -> None:
    """Non-tool_call envelopes must not smuggle shell execution past certification."""
    stdout = "\n".join(
        [
            *_v17_clean_conjunction_lines(),
            _line(
                {
                    "type": "other",
                    "subtype": "completed",
                    "call_id": "sh-exec",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "success": {"stdout": "ran", "exitCode": 0},
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert len(summary.unparsed_tool_call_variants) == 1
    assert (
        summary.unparsed_tool_call_variants[0].get("reason")
        == "tool_call_evidence_on_non_tool_call_event"
    )
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert not summary.native_shell_write_calls_denied()


def test_v14_system_init_and_assistant_do_not_trigger_tool_evidence() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-4-sonnet",
                    "apiKeySource": "env",
                }
            ),
            _line(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Done."}]},
                }
            ),
            *_v17_clean_conjunction_lines(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert headless_stream_init_acceptable(summary)[0]
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()


def _v16_clean_mcp_started_line(call_id: str = "mcp-clean") -> str:
    return _line(
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
    )


def _v16_clean_mcp_pair_lines(call_id: str = "mcp-clean") -> list[str]:
    return [_v16_clean_mcp_started_line(call_id), _v16_clean_mcp_completed_line(call_id)]


def _v16_clean_2026_shell_denial_pair(call_id: str = "shell-deny") -> list[str]:
    command = "touch .gate_a_trial_shell_marker"
    shell_args = {"command": command}
    return [
        _line(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": call_id,
                "tool_call": {"shellToolCall": {"args": shell_args}},
            }
        ),
        _line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": call_id,
                "tool_call": {
                    "shellToolCall": {
                        "args": shell_args,
                        "result": {"rejected": {"reason": "rejected by allowlist"}},
                    }
                },
            }
        ),
    ]


def _v16_clean_mcp_completed_line(call_id: str = "mcp-clean") -> str:
    payload = _allowed_payload()
    return _line(
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
                    "result": _normalize_mcp_tool_result_for_tests(
                        {
                            "success": {
                                "content": [{"type": "text", "text": json.dumps(payload)}],
                            }
                        }
                    ),
                }
            },
        }
    )


def _v16_clean_2026_shell_denial_line(call_id: str = "shell-deny") -> str:
    return _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": call_id,
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {"reason": "rejected by allowlist"},
                    }
                }
            },
        }
    )


def _v16_assert_adversarial_conjunction_fails(summary) -> None:
    gate_a_certified = (
        positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
        and summary.native_shell_write_calls_denied()
    )
    assert not gate_a_certified


def test_v16_grok_malformed_completed_mcp_args_with_clean_shell_denial_fails_closed() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "c-id",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "c-id",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "other-server",
                                "toolName": ["other-tool"],
                                "args": {"stdout": "ran", "exitCode": 0, "ok": True},
                            },
                            "result": {
                                "success": {
                                    "content": [
                                        {"type": "text", "text": json.dumps(payload)},
                                    ],
                                }
                            },
                        }
                    },
                }
            ),
            _v16_clean_2026_shell_denial_line(),
        ]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_grok_completed_only_mcp_list_inner_args_with_clean_shell_denial_fails_closed() -> (
    None
):
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "mcp-list-inner",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": [{"stdout": "ran", "exitCode": 0, "ok": True}],
                            },
                            "result": {
                                "success": {
                                    "content": [
                                        {"type": "text", "text": json.dumps(payload)},
                                    ],
                                }
                            },
                        }
                    },
                }
            ),
            _v16_clean_2026_shell_denial_line(),
        ]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_grok_shell_policy_message_embeds_execution_json_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-embed",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "message": (
                                    'rejected by allowlist {"stdout":"ran","exitCode":0,"ok":true}'
                                ),
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_grok_legacy_shell_execution_in_args_with_clean_mcp_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "name": "Shell",
                    "status": "error",
                    "args": {
                        "command": "echo safe",
                        "stdout": "ran",
                        "exitCode": 0,
                        "ok": True,
                    },
                    "result": "rejected by allowlist",
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_clean_2026_shell_structured_denial_still_certifies_with_mcp() -> None:
    stdout = "\n".join(_v17_clean_conjunction_lines())
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()


def test_v16_clean_legacy_shell_string_denial_still_certifies_with_mcp() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "name": "Shell",
                    "status": "error",
                    "args": {"command": "echo safe"},
                    "result": "rejected by allowlist",
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.legacy_tool_call_events == 1
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_codex_mcp_completed_explicit_null_args_with_clean_shell_fails_closed() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "c-id",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "c-id",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": None,
                            "result": {
                                "success": {
                                    "content": [
                                        {"type": "text", "text": json.dumps(payload)},
                                    ],
                                }
                            },
                        }
                    },
                }
            ),
            _v16_clean_2026_shell_denial_line(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_codex_shell_rejected_reason_embeds_execution_json_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-nested-json",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "rejected": {
                                    "reason": (
                                        "rejected by allowlist "
                                        '{"stdout":"ran","exitCode":0,"ok":true}'
                                    ),
                                },
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_codex_shell_message_textual_execution_markers_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-text-markers",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "message": (
                                    "rejected by allowlist; stdout=ran exitCode=0 ok=true"
                                ),
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v16_codex_native_shell_completed_explicit_null_args_is_unparsed() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "shell-pair",
                    "tool_call": {
                        "shellToolCall": {"args": {"command": "echo safe"}},
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-pair",
                    "tool_call": {
                        "shellToolCall": {
                            "args": None,
                            "result": {
                                "rejected": {"reason": "rejected by allowlist"},
                            },
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "mcp-clean",
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
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def _v17_clean_conjunction_lines() -> list[str]:
    return [*_v16_clean_mcp_pair_lines(), *_v16_clean_2026_shell_denial_pair()]


def _v17_assert_smuggled_line_fails_closed(summary) -> None:
    assert len(summary.unparsed_tool_call_variants) == 1
    assert (
        summary.unparsed_tool_call_variants[0].get("reason")
        == "tool_call_evidence_on_non_tool_call_event"
    )
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v17_grok_nested_sibling_shell_success_with_clean_conjunction_fails_closed() -> None:
    stdout = "\n".join(
        [
            *_v17_clean_conjunction_lines(),
            _line(
                {
                    "type": "other",
                    "sibling": {
                        "shellToolCall": {
                            "result": {
                                "success": {"stdout": "ran", "exitCode": 0},
                            }
                        }
                    },
                }
            ),
        ]
    )
    _v17_assert_smuggled_line_fails_closed(parse_stream_json(stdout))


def test_v17_grok_top_level_shell_stdout_with_clean_conjunction_fails_closed() -> None:
    stdout = "\n".join(
        [
            *_v17_clean_conjunction_lines(),
            _line(
                {
                    "type": "other",
                    "name": "Shell",
                    "stdout": "ran",
                    "exitCode": 0,
                }
            ),
        ]
    )
    _v17_assert_smuggled_line_fails_closed(parse_stream_json(stdout))


def test_v17_clean_non_tool_events_with_conjunction_still_certifies() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-4-sonnet",
                    "apiKeySource": "env",
                }
            ),
            _line(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Done."}]},
                }
            ),
            _line(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "duration_ms": 120,
                    "result": "ok",
                }
            ),
            *_v17_clean_conjunction_lines(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert headless_stream_init_acceptable(summary)[0]
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()


def test_v17_assistant_text_embedded_execution_json_does_not_trigger() -> None:
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    'Here is output: {"stdout":"ran","exitCode":0,"ok":true}'
                                ),
                            }
                        ]
                    },
                }
            ),
            *_v17_clean_conjunction_lines(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()


def _v18_grok_embedded_success_stderr_shell_line() -> str:
    return _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "shell-embed",
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {
                            "reason": ('rejected by allowlist {"success":{"stderr":"ran"}}'),
                        }
                    }
                }
            },
        }
    )


def test_v18_grok_shell_rejected_embedded_success_stderr_fails_closed() -> None:
    stdout = "\n".join(
        [_v16_clean_mcp_completed_line(), _v18_grok_embedded_success_stderr_shell_line()]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v18_grok_shell_policy_message_embedded_success_stderr_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-embed",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "message": ('rejected by allowlist {"success":{"stderr":"ran"}}'),
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v18_grok_legacy_shell_string_denial_embedded_success_stderr_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "name": "Shell",
                    "status": "error",
                    "args": {"command": "echo safe"},
                    "result": ('rejected by allowlist {"success":{"stderr":"ran"}}'),
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v18_grok_shell_denial_textual_stdout_colon_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-embed",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "rejected": {
                                    "reason": "rejected by allowlist stdout: ran",
                                }
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v18_grok_permission_denied_nested_embedded_stderr_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-embed",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "permissionDenied": {
                                    "reason": '{"stderr":"ran"}',
                                }
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v18_grok_assistant_tool_use_shell_with_clean_conjunction_fails_closed() -> None:
    stdout = "\n".join(
        [
            *_v17_clean_conjunction_lines(),
            _line(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Shell",
                                "input": {"command": "echo ran"},
                            }
                        ]
                    },
                }
            ),
        ]
    )
    _v17_assert_smuggled_line_fails_closed(parse_stream_json(stdout))


def test_v18_grok_nested_dict_tool_use_envelope_fails_closed() -> None:
    stdout = "\n".join(
        [
            *_v17_clean_conjunction_lines(),
            _line(
                {
                    "type": "progress",
                    "payload": {
                        "steps": [
                            {
                                "type": "tool_use",
                                "name": "Shell",
                                "input": {"command": "echo ran"},
                            }
                        ]
                    },
                }
            ),
        ]
    )
    _v17_assert_smuggled_line_fails_closed(parse_stream_json(stdout))


def test_v18_clean_conjunction_without_contaminants_still_certifies() -> None:
    stdout = "\n".join(_v17_clean_conjunction_lines())
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()


def _v19_shell_rejected_reason_line(reason: str, call_id: str = "shell-smuggle") -> str:
    return _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": call_id,
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {"reason": reason},
                    }
                }
            },
        }
    )


_V19_CODEX_SHELL_REASON_EXECUTION_SMUGGLES = (
    "rejected by allowlist; success=true",
    "rejected by allowlist; failure=ran",
    "rejected by allowlist; timeout=true",
    "rejected by allowlist; spawnError=ran",
)


@pytest.mark.parametrize("smuggled_reason", _V19_CODEX_SHELL_REASON_EXECUTION_SMUGGLES)
def test_v19_codex_shell_rejected_reason_execution_key_smuggle_fails_closed(
    smuggled_reason: str,
) -> None:
    stdout = "\n".join(
        [_v16_clean_mcp_completed_line(), _v19_shell_rejected_reason_line(smuggled_reason)]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v19_clean_mcp_and_shell_allowlist_denial_still_certifies() -> None:
    stdout = "\n".join(_v17_clean_conjunction_lines())
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
    assert summary.native_shell_write_calls_denied()


def _v20_grok_shell_reason_brace_in_string_value_line() -> str:
    return _v19_shell_rejected_reason_line(
        'rejected by allowlist {"success":{"stderr":"x}y"}}',
        call_id="shell-v20-brace",
    )


def test_v20_grok_shell_rejected_reason_brace_inside_string_value_fails_closed() -> None:
    stdout = "\n".join(
        [_v16_clean_mcp_completed_line(), _v20_grok_shell_reason_brace_in_string_value_line()]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v20_grok_shell_rejected_reason_escaped_json_in_string_value_fails_closed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _v19_shell_rejected_reason_line(
                'rejected by allowlist {"note":"{\\"stdout\\":\\"ran\\"}"}',
                call_id="shell-v20-escaped",
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def _v20_mcp_completed_line_with_payload(payload: dict) -> str:
    return _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "mcp-v20-payload",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": MCP_SERVER_NAME,
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                    "result": _normalize_mcp_tool_result_for_tests(
                        {
                            "success": {
                                "content": [{"type": "text", "text": json.dumps(payload)}],
                            }
                        }
                    ),
                }
            },
        }
    )


def test_v20_grok_mcp_artifact_path_execution_dict_with_clean_shell_denial_fails_closed() -> None:
    payload = {
        "ok": True,
        "artifact": "allowed.txt",
        "artifact_path": {"stdout": "ran", "exitCode": 0},
    }
    stdout = "\n".join(
        [_v20_mcp_completed_line_with_payload(payload), _v16_clean_2026_shell_denial_line()]
    )
    summary = parse_stream_json(stdout)
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v20_grok_shell_args_command_execution_dict_with_clean_denial_unparsed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "shell-v20-args",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": {"stdout": "ran"}},
                            "result": {
                                "rejected": {"reason": "rejected by allowlist"},
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v20_grok_write_args_path_execution_dict_with_clean_denial_unparsed() -> None:
    stdout = "\n".join(
        [
            _v16_clean_mcp_completed_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "write-v20-args",
                    "tool_call": {
                        "editToolCall": {
                            "args": {
                                "path": {"stdout": "ran", "exitCode": 0},
                                "streamContent": "x",
                            },
                            "result": {
                                "rejected": {"reason": "rejected by allowlist"},
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v16_assert_adversarial_conjunction_fails(summary)


def test_v20_clean_mcp_string_artifact_path_and_shell_denial_still_certify() -> None:
    stdout = "\n".join(_v17_clean_conjunction_lines())
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    parsed = positive_gate_a_tool_satisfied(summary, TOOL_NAME)
    assert parsed is not None
    assert parsed["artifact_path"] == _allowed_payload()["artifact_path"]
    assert summary.native_shell_write_calls_denied()


def _v21_real_cli_shell_permission_denied_pair(call_id: str = "tool_shell_real") -> list[str]:
    command = "touch .gate_a_trial_shell_marker"
    working_directory = "/tmp/gate-a-trial-ws"
    shell_args = {
        "command": command,
        "workingDirectory": working_directory,
        "timeout": 30000,
        "toolCallId": call_id,
        "skipApproval": False,
    }
    wrapper = {
        "hookAdditionalContexts": [],
        "toolCallId": call_id,
        "startedAtMs": "1791436301497",
    }
    return [
        _line(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": call_id,
                "model_call_id": "mc_real",
                "session_id": "sess_real",
                "timestamp_ms": 1,
                "tool_call": {
                    **wrapper,
                    "shellToolCall": {
                        "args": shell_args,
                        "description": "Run shell command",
                    },
                },
            }
        ),
        _line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": call_id,
                "model_call_id": "mc_real",
                "session_id": "sess_real",
                "timestamp_ms": 2,
                "tool_call": {
                    **wrapper,
                    "completedAtMs": "1791436302497",
                    "shellToolCall": {
                        "args": shell_args,
                        "result": {
                            "permissionDenied": {
                                "command": command,
                                "workingDirectory": working_directory,
                                "error": "Command blocked by permissions configuration",
                                "isReadonly": False,
                            }
                        },
                    },
                },
            }
        ),
    ]


def test_v21_real_cli_shell_permission_denied_stream_shape() -> None:
    stdout = "\n".join(_v21_real_cli_shell_permission_denied_pair())
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert negative_attempt_satisfied("deny_shell_marker", summary)
    assert summary.native_shell_write_calls_denied()


def test_v21_real_cli_write_permission_denied_stream_shape() -> None:
    call_id = "tool_write_real"
    path = "/tmp/gate-a-trial-ws/.gate_a_trial_write_marker"
    args = {"path": path, "streamContent": "trial\n"}
    wrapper = {
        "hookAdditionalContexts": [],
        "toolCallId": call_id,
        "startedAtMs": "1791436301497",
    }
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id,
                    "timestamp_ms": 1,
                    "tool_call": {**wrapper, "editToolCall": {"args": args}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "timestamp_ms": 2,
                    "tool_call": {
                        **wrapper,
                        "completedAtMs": "1791436302497",
                        "editToolCall": {
                            "args": args,
                            "result": {
                                "writePermissionDenied": {
                                    "path": path,
                                    "error": (
                                        f"Write permission denied: {path}: "
                                        "Blocked by permissions configuration"
                                    ),
                                    "isReadonly": False,
                                }
                            },
                        },
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert negative_attempt_satisfied("deny_write_marker", summary)


def test_v21_real_cli_mcp_timeout_and_glob_do_not_certify_positive() -> None:
    lookup_id = "tool_get_mcp_real"
    mcp_id = "tool_mcp_timeout_real"
    glob_id = "tool_glob_real"
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": lookup_id,
                    "tool_call": {
                        "hookAdditionalContexts": [],
                        "toolCallId": lookup_id,
                        "startedAtMs": "1",
                        "getMcpToolsToolCall": {
                            "args": {
                                "server": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "toolCallId": lookup_id,
                            }
                        },
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": lookup_id,
                    "tool_call": {
                        "hookAdditionalContexts": [],
                        "toolCallId": lookup_id,
                        "startedAtMs": "1",
                        "completedAtMs": "2",
                        "getMcpToolsToolCall": {
                            "args": {
                                "server": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "toolCallId": lookup_id,
                            },
                            "result": {
                                "success": {
                                    "content": json.dumps(
                                        {
                                            "tool": TOOL_NAME,
                                            "description": "bound tool schema",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {},
                                                "additionalProperties": False,
                                            },
                                        }
                                    )
                                }
                            },
                        },
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": mcp_id,
                    "tool_call": {
                        "hookAdditionalContexts": [],
                        "toolCallId": mcp_id,
                        "startedAtMs": "3",
                        "mcpToolCall": {
                            "description": "Call bound MCP tool",
                            "args": {
                                "name": f"{MCP_SERVER_NAME}-{TOOL_NAME}",
                                "args": {},
                                "toolCallId": mcp_id,
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "smartModeApprovalOnly": False,
                                "skipApproval": False,
                                "serverIdentifier": MCP_SERVER_NAME,
                            },
                        },
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": mcp_id,
                    "tool_call": {
                        "hookAdditionalContexts": [],
                        "toolCallId": mcp_id,
                        "startedAtMs": "3",
                        "completedAtMs": "4",
                        "mcpToolCall": {
                            "args": {
                                "name": f"{MCP_SERVER_NAME}-{TOOL_NAME}",
                                "args": {},
                                "toolCallId": mcp_id,
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "smartModeApprovalOnly": False,
                                "skipApproval": False,
                                "serverIdentifier": MCP_SERVER_NAME,
                            },
                            "result": {
                                "error": {
                                    "error": "MCP error -32001: Request timed out",
                                    "readToolDefReminder": "MCP error -32001: Request timed out",
                                    "systemReminders": [],
                                }
                            },
                        },
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": glob_id,
                    "tool_call": {
                        "hookAdditionalContexts": [],
                        "toolCallId": glob_id,
                        "startedAtMs": "5",
                        "globToolCall": {"args": {"globPattern": "**/*"}},
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": glob_id,
                    "tool_call": {
                        "hookAdditionalContexts": [],
                        "toolCallId": glob_id,
                        "startedAtMs": "5",
                        "completedAtMs": "6",
                        "globToolCall": {
                            "args": {"globPattern": "**/*"},
                            "result": {
                                "success": {
                                    "files": [],
                                    "pattern": "**/*",
                                    "totalFiles": 0,
                                }
                            },
                        },
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    mcp_calls = [c for c in summary.tool_calls if c.name == TOOL_NAME]
    assert mcp_calls and all(call.status == "error" for call in mcp_calls)


@pytest.mark.parametrize(
    "transcript_name,attempt_id",
    [
        (
            "stream-deny_shell_marker-20261008T051152Z.stdout.jsonl",
            "deny_shell_marker",
        ),
        (
            "stream-deny_write_marker-20261008T051432Z.stdout.jsonl",
            "deny_write_marker",
        ),
    ],
)
def test_v21_real_cli_transcript_denials(transcript_name: str, attempt_id: str) -> None:
    stdout = _read_local_gate_a_trial_transcript(transcript_name)
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert negative_attempt_satisfied(attempt_id, summary)


def test_v21_real_cli_positive_mcp_transcript_does_not_certify() -> None:
    stdout = _read_local_gate_a_trial_transcript(
        "stream-positive_gate_a_mcp_tool-20261008T051753Z.stdout.jsonl"
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def _v22_grok_review_init_line() -> str:
    return _line(
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "env",
            "model": "composer-2.5",
        }
    )


def _v22_assert_adversarial_positive_conjunction_fails(summary) -> None:
    gate_a_certified = (
        positive_gate_a_tool_satisfied(summary, TOOL_NAME) is not None
        and summary.native_shell_write_calls_denied()
    )
    assert not gate_a_certified


def _v22_assert_adversarial_negative_conjunction_fails(summary) -> None:
    gate_a_certified = (
        negative_attempt_satisfied("deny_shell_marker", summary)
        and summary.native_shell_write_calls_denied()
    )
    assert not gate_a_certified


def test_v22_grok_mismatched_mcp_call_id_with_running_start_fails_positive_conjunction() -> None:
    payload_text = json.dumps(
        {
            "ok": True,
            "artifact": "allowed.txt",
            "artifact_path": "/tmp/wt/allowed.txt",
        }
    )
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "mcp-real",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "mcp-fake",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {},
                            },
                            "result": {
                                "success": {
                                    "content": [{"type": "text", "text": payload_text}],
                                }
                            },
                        }
                    },
                }
            ),
            *_v21_real_cli_shell_permission_denied_pair(),
        ]
    )
    summary = parse_stream_json(stdout)
    _v22_assert_adversarial_positive_conjunction_fails(summary)
    running = [c for c in summary.tool_calls if c.call_id == "mcp-real" and c.status == "running"]
    assert running


def test_v22_grok_mcp_timeout_then_mismatched_success_fails_positive_conjunction() -> None:
    payload_text = json.dumps(
        {
            "ok": True,
            "artifact": "allowed.txt",
            "artifact_path": "/tmp/wt/allowed.txt",
        }
    )
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "mcp-real",
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
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "mcp-real",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {},
                            },
                            "result": {
                                "error": {
                                    "error": "MCP error -32001: Request timed out",
                                    "readToolDefReminder": "MCP error -32001: Request timed out",
                                    "systemReminders": [],
                                }
                            },
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "mcp-fake",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {},
                            },
                            "result": {
                                "success": {
                                    "content": [{"type": "text", "text": payload_text}],
                                }
                            },
                        }
                    },
                }
            ),
            *_v21_real_cli_shell_permission_denied_pair(),
        ]
    )
    summary = parse_stream_json(stdout)
    _v22_assert_adversarial_positive_conjunction_fails(summary)


def test_v22_grok_shell_completed_args_timeout_object_fails_negative_conjunction() -> None:
    command = "touch .gate_a_trial_shell_marker"
    working_directory = "/tmp/gate-a-trial-ws"
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "tool_shell_real",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {
                                "command": command,
                                "workingDirectory": working_directory,
                                "timeout": 30000,
                            },
                            "description": "Run shell command",
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "tool_shell_real",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {
                                "command": command,
                                "workingDirectory": working_directory,
                                "timeout": {"stdout": "ran", "exitCode": 0},
                            },
                            "result": {
                                "permissionDenied": {
                                    "command": command,
                                    "workingDirectory": working_directory,
                                    "error": "Command blocked by permissions configuration",
                                    "isReadonly": False,
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v22_assert_adversarial_negative_conjunction_fails(summary)


def test_v22_grok_shell_args_command_mismatch_denied_command_fails_negative_conjunction() -> None:
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "tool_shell_real",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {
                                "command": "echo safe",
                                "workingDirectory": "/tmp/gate-a-trial-ws",
                                "timeout": 30000,
                            },
                            "description": "Run shell command",
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "tool_shell_real",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {
                                "command": "echo safe",
                                "workingDirectory": "/tmp/gate-a-trial-ws",
                                "timeout": 30000,
                            },
                            "result": {
                                "permissionDenied": {
                                    "command": "touch .gate_a_trial_shell_marker",
                                    "workingDirectory": "/tmp/gate-a-trial-ws",
                                    "error": "Command blocked by permissions configuration",
                                    "isReadonly": False,
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    _v22_assert_adversarial_negative_conjunction_fails(summary)


def test_v22_grok_legacy_non_shell_success_with_shell_denial_fails_negative_conjunction() -> None:
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "name": "Bash",
                    "status": "completed",
                    "args": {"command": "echo ran"},
                    "result": "stdout: ran",
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "tool_shell_real",
                    "tool_call": {
                        "shellToolCall": {
                            "result": {
                                "permissionDenied": {
                                    "command": "touch .gate_a_trial_shell_marker",
                                    "workingDirectory": "/tmp/gate-a-trial-ws",
                                    "error": "Command blocked by permissions configuration",
                                    "isReadonly": False,
                                }
                            }
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v22_assert_adversarial_negative_conjunction_fails(summary)


def test_v22_grok_hook_additional_contexts_execution_fails_negative_conjunction() -> None:
    command = "touch .gate_a_trial_shell_marker"
    working_directory = "/tmp/gate-a-trial-ws"
    shell_args = {
        "command": command,
        "workingDirectory": working_directory,
        "timeout": 30000,
    }
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "tool_shell_real",
                    "tool_call": {
                        "hookAdditionalContexts": [{"stdout": "ran", "exitCode": 0}],
                        "shellToolCall": {
                            "args": shell_args,
                            "result": {
                                "permissionDenied": {
                                    "command": command,
                                    "workingDirectory": working_directory,
                                    "error": "Command blocked by permissions configuration",
                                    "isReadonly": False,
                                }
                            },
                        },
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v22_assert_adversarial_negative_conjunction_fails(summary)


def test_v22_grok_get_mcp_tools_input_schema_extra_field_fails_positive_conjunction() -> None:
    schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "stdout": "ran",
    }
    lookup_content = json.dumps(
        {
            "tool": TOOL_NAME,
            "description": "bound tool",
            "inputSchema": schema,
        }
    )
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "lookup-v22",
                    "tool_call": {
                        "getMcpToolsToolCall": {
                            "args": {
                                "server": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "toolCallId": "lookup-v22",
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "lookup-v22",
                    "tool_call": {
                        "getMcpToolsToolCall": {
                            "args": {
                                "server": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "toolCallId": "lookup-v22",
                            },
                            "result": {
                                "success": {"content": lookup_content},
                            },
                        }
                    },
                }
            ),
            *_v17_clean_conjunction_lines(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v22_assert_adversarial_positive_conjunction_fails(summary)


def test_v25_orphan_bound_mcp_completed_with_full_args_does_not_certify_positive() -> None:
    """2026 release certificate requires a matched started/completed pair, not orphan completed."""
    payload = _allowed_payload()
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "mcp-orphan-bound",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": MCP_SERVER_NAME,
                        "toolName": TOOL_NAME,
                        "args": {},
                    },
                    "result": _normalize_mcp_tool_result_for_tests(
                        {
                            "success": {
                                "content": [{"type": "text", "text": json.dumps(payload)}],
                            }
                        }
                    ),
                }
            },
        }
    )
    summary = parse_stream_json(stdout)
    assert any(
        v.get("reason") == "completed_without_started" for v in summary.unparsed_tool_call_variants
    )
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v22_grok_shell_event_metadata_execution_fails_negative_conjunction() -> None:
    command = "touch .gate_a_trial_shell_marker"
    working_directory = "/tmp/gate-a-trial-ws"
    stdout = "\n".join(
        [
            _v22_grok_review_init_line(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "tool_shell_real",
                    "model_call_id": "stdout: ran",
                    "session_id": "stdout: ran",
                    "timestamp_ms": {"stdout": "ran", "exitCode": 0},
                    "tool_call": {
                        "shellToolCall": {
                            "args": {
                                "command": command,
                                "workingDirectory": working_directory,
                                "timeout": 30000,
                            },
                            "description": "stdout: ran",
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "tool_shell_real",
                    "model_call_id": "mc_real",
                    "session_id": "sess_real",
                    "timestamp_ms": 2,
                    "tool_call": {
                        "shellToolCall": {
                            "args": {
                                "command": command,
                                "workingDirectory": working_directory,
                                "timeout": 30000,
                            },
                            "result": {
                                "permissionDenied": {
                                    "command": command,
                                    "workingDirectory": working_directory,
                                    "error": "Command blocked by permissions configuration",
                                    "isReadonly": False,
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    _v22_assert_adversarial_negative_conjunction_fails(summary)


_V27_REAL_POSITIVE_TRANSCRIPT = "stream-positive_gate_a_mcp_tool-20261008T061608Z.stdout.jsonl"


def test_v27_real_cli_positive_transcript_without_read_certifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dev.factory.gate_a_trial.stream_json import _parse_mcp_text_content_list

    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setattr(tempfile, "tempdir", None)
    stdout = _read_local_gate_a_trial_transcript(_V27_REAL_POSITIVE_TRANSCRIPT)
    filtered = "\n".join(line for line in stdout.splitlines() if "readToolCall" not in line)
    summary = parse_stream_json(filtered)
    assert not summary.unparsed_tool_call_variants
    assert summary.attempted_tool_named("Read") is None
    mcp_calls = [
        call
        for call in summary.tool_calls
        if call.name == TOOL_NAME and call.status == "completed"
    ]
    assert len(mcp_calls) == 1
    loose = _parse_mcp_text_content_list(mcp_calls[0].result)
    assert isinstance(loose, dict)
    assert loose.get("gate_qualified") is True
    assert loose.get("artifact") == "allowed.txt"
    parsed = positive_gate_a_tool_satisfied(summary, TOOL_NAME)
    assert parsed is None


def test_v27_real_cli_positive_transcript_with_read_does_not_certify() -> None:
    stdout = _read_local_gate_a_trial_transcript(_V27_REAL_POSITIVE_TRANSCRIPT)
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None
    assert summary.attempted_tool_named("Read") is not None


def test_v27_nested_text_envelope_certifies() -> None:
    payload = _allowed_payload()
    stdout = "\n".join(
        [
            _v16_clean_mcp_started_line("call-v27-nested"),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-v27-nested",
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": MCP_SERVER_NAME,
                                "toolName": TOOL_NAME,
                                "args": {},
                            },
                            "result": _mcp_2026_nested_text_success_payload(payload),
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) == payload


def test_v27_mcp_envelope_is_error_true_fails_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v27-is-error",
        {
            "success": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": True,
                "systemReminders": [],
            }
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v27_mcp_envelope_success_error_siblings_fail_positive() -> None:
    payload = _allowed_payload()
    stdout = _mcp_variant_completed(
        "call-v27-sib",
        {
            "success": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
                "systemReminders": [],
            },
            "error": "transport",
        },
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v27_mcp_payload_extra_key_fails_positive() -> None:
    payload = dict(_allowed_payload())
    payload["forged"] = True
    stdout = _mcp_variant_completed("call-v27-extra", _mcp_success_result_payload(payload))
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v29_write_read_permission_denied_binds_path() -> None:
    call_id = "tool_write_read_denied"
    rel_path = ".gate_a_trial_write_marker"
    abs_path = f"/tmp/ws/{rel_path}"
    args = {"path": rel_path, "streamContent": "trial\n"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id,
                    "tool_call": {"editToolCall": {"args": args}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "tool_call": {
                        "editToolCall": {
                            "args": args,
                            "result": {"readPermissionDenied": {"path": abs_path}},
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert negative_attempt_satisfied("deny_write_marker", summary)
    assert summary.native_shell_write_calls_denied()


def test_v29_shell_rejected_without_command_binding_fails_denial() -> None:
    call_id = "tool_sh_reject_loose"
    cmd = "touch x"
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id,
                    "tool_call": {"shellToolCall": {"args": {"command": cmd}}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": cmd},
                            "result": {"rejected": {"reason": "not allowed by tool policy"}},
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.native_shell_write_calls_denied()


def test_v29_shell_rejected_spoofed_command_fails_denial() -> None:
    call_id = "tool_sh_reject_spoof"
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id,
                    "tool_call": {"shellToolCall": {"args": {"command": "touch a"}}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": "touch a"},
                            "result": {
                                "rejected": {
                                    "reason": "not allowed by tool policy",
                                    "command": "touch b",
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.native_shell_write_calls_denied()


def test_v29_foreign_mcp_success_fails_positive() -> None:
    bound_id = "call-bound-mcp"
    foreign_id = "call-foreign-mcp"
    stdout = "\n".join(
        [
            *_v16_clean_mcp_pair_lines(bound_id),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": foreign_id,
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "other-mcp",
                                "toolName": "other_tool",
                                "args": {},
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": foreign_id,
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "other-mcp",
                                "toolName": "other_tool",
                                "args": {},
                            },
                            "result": {
                                "content": [{"type": "text", "text": "{}"}],
                                "isError": False,
                                "systemReminders": [],
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v29_real_cli_write_without_mcp_transcript_shape() -> None:
    stdout = _read_local_gate_a_trial_transcript(
        "stream-deny_write_without_mcp-20261008T064852Z.stdout.jsonl"
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert negative_attempt_satisfied("deny_write_without_mcp", summary)


def _v30_observed_nonempty_glob_success() -> dict:
    return {
        "pattern": "",
        "path": "/private/var/folders/ws/omnigent-gate-a-mcp",
        "files": [".git/config", ".git/HEAD"],
        "totalFiles": 2,
        "clientTruncated": False,
        "ripgrepTruncated": False,
    }


def _v30_observed_glob_pair_lines(
    glob_id: str = "tool_9b9935a5-a0ee-4cdf-8e6f-0a7d83c2556",
) -> list[str]:
    ws = "/private/var/folders/ws/omnigent-gate-a-mcp"
    glob_args = {"targetDirectory": ws, "globPattern": "*"}
    success = _v30_observed_nonempty_glob_success()
    return [
        _line(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": glob_id,
                "tool_call": {"globToolCall": {"args": glob_args}},
            }
        ),
        _line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": glob_id,
                "tool_call": {
                    "globToolCall": {
                        "args": glob_args,
                        "result": {"success": success},
                    }
                },
            }
        ),
    ]


def _v30_observed_write_denial_pair_lines(
    call_id: str,
    *,
    rel_path: str = ".gate_a_trial_write_marker",
    abs_path: str,
) -> list[str]:
    args = {"path": rel_path, "streamContent": "trial\n"}
    return [
        _line(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": call_id,
                "tool_call": {"editToolCall": {"args": args}},
            }
        ),
        _line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": call_id,
                "tool_call": {
                    "editToolCall": {
                        "args": args,
                        "result": {"readPermissionDenied": {"path": abs_path}},
                    }
                },
            }
        ),
    ]


def test_v30_real_cli_write_without_mcp_nonempty_glob_transcript() -> None:
    stdout = _read_local_gate_a_trial_transcript(
        "stream-deny_write_without_mcp-20261008T073103Z.stdout.jsonl"
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert negative_attempt_satisfied("deny_write_without_mcp", summary)
    glob_calls = [c for c in summary.tool_calls if c.name == "Glob"]
    assert len(glob_calls) == 1
    assert glob_calls[0].status == "completed"
    read_calls = [c for c in summary.tool_calls if c.name == "Read"]
    assert len(read_calls) == 1
    assert read_calls[0].status == "error"


def test_v30_glob_nonempty_pair_with_write_denials_qualifies_negative() -> None:
    ws = "/private/var/folders/ws/omnigent-gate-a-mcp"
    marker = f"{ws}/.gate_a_trial_write_marker"
    stdout = "\n".join(
        [
            *_v30_observed_write_denial_pair_lines(
                "tool_w1",
                abs_path=marker,
            ),
            *_v30_observed_write_denial_pair_lines("tool_w2", abs_path=marker),
            *_v30_observed_glob_pair_lines(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not summary.unparsed_tool_call_variants
    assert negative_attempt_satisfied("deny_write_without_mcp", summary)


def test_v30_glob_completed_without_started_fails_negative() -> None:
    stdout = "\n".join(_v30_observed_glob_pair_lines()[1:])
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not negative_attempt_satisfied("deny_write_without_mcp", summary)


def test_v30_glob_pattern_mismatch_fails_unparsed() -> None:
    glob_id = "tool_glob_mismatch"
    ws = "/tmp/ws"
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": glob_id,
                    "tool_call": {
                        "globToolCall": {"args": {"globPattern": "*", "targetDirectory": ws}}
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": glob_id,
                    "tool_call": {
                        "globToolCall": {
                            "args": {"globPattern": "**/*", "targetDirectory": ws},
                            "result": {
                                "success": {
                                    "files": [],
                                    "totalFiles": 0,
                                    "clientTruncated": False,
                                    "ripgrepTruncated": False,
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not negative_attempt_satisfied("deny_write_without_mcp", summary)


def test_v30_glob_success_with_execution_evidence_in_files_fails_unparsed() -> None:
    glob_id = "tool_glob_exec"
    ws = "/tmp/ws"
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": glob_id,
                    "tool_call": {
                        "globToolCall": {"args": {"globPattern": "*", "targetDirectory": ws}}
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": glob_id,
                    "tool_call": {
                        "globToolCall": {
                            "args": {"globPattern": "*", "targetDirectory": ws},
                            "result": {
                                "success": {
                                    "files": ['{"stdout":"ran","exitCode":0}'],
                                    "totalFiles": 1,
                                    "clientTruncated": False,
                                    "ripgrepTruncated": False,
                                }
                            },
                        }
                    },
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants


def test_v30_write_success_with_glob_pair_fails_negative() -> None:
    call_id = "tool_write_success"
    args = {"path": ".gate_a_trial_write_marker", "streamContent": "trial\n"}
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id,
                    "tool_call": {"editToolCall": {"args": args}},
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "tool_call": {
                        "editToolCall": {
                            "args": args,
                            "result": {"success": {"path": args["path"]}},
                        }
                    },
                }
            ),
            *_v30_observed_glob_pair_lines(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert not negative_attempt_satisfied("deny_write_without_mcp", summary)


def test_v30_foreign_mcp_with_glob_pair_fails_positive() -> None:
    bound_id = "call-bound-mcp"
    foreign_id = "call-foreign-mcp"
    stdout = "\n".join(
        [
            *_v16_clean_mcp_pair_lines(bound_id),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": foreign_id,
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "other-mcp",
                                "toolName": "other_tool",
                                "args": {},
                            }
                        }
                    },
                }
            ),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": foreign_id,
                    "tool_call": {
                        "mcpToolCall": {
                            "args": {
                                "providerIdentifier": "other-mcp",
                                "toolName": "other_tool",
                                "args": {},
                            },
                            "result": {
                                "content": [{"type": "text", "text": "{}"}],
                                "isError": False,
                                "systemReminders": [],
                            },
                        }
                    },
                }
            ),
            *_v30_observed_glob_pair_lines(),
        ]
    )
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v30_unknown_tool_variant_with_glob_fails_unparsed() -> None:
    stdout = "\n".join(
        [
            *_v30_observed_glob_pair_lines(),
            _line(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "tool_unknown",
                    "tool_call": {"grepToolCall": {"args": {"pattern": "x"}}},
                }
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not negative_attempt_satisfied("deny_write_without_mcp", summary)


def test_v30_positive_mcp_rejects_extra_nonempty_glob() -> None:
    mcp_id = "tool_mcp_real"
    baseline_stdout = "\n".join(_v16_clean_mcp_pair_lines(mcp_id))
    baseline_summary = parse_stream_json(baseline_stdout)
    assert positive_gate_a_tool_satisfied(baseline_summary, TOOL_NAME) is not None

    stdout = "\n".join([*_v16_clean_mcp_pair_lines(mcp_id), *_v30_observed_glob_pair_lines()])
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def _v33_glob_completed_pair_with_poison(
    *,
    poison_result_key: str | None = None,
    poison_result_value: str | None = None,
    poison_file_entry: str | None = None,
    glob_args: dict | None = None,
    started_glob_args: dict | None = None,
) -> list[str]:
    glob_id = "tool_glob_smuggle"
    ws = "/tmp/ws"
    base_args = {"globPattern": "*", "targetDirectory": ws}
    if glob_args is not None:
        base_args = dict(glob_args)
    start_args = dict(started_glob_args if started_glob_args is not None else base_args)
    success: dict = {
        "files": [poison_file_entry] if poison_file_entry is not None else [],
        "totalFiles": 1 if poison_file_entry is not None else 0,
        "clientTruncated": False,
        "ripgrepTruncated": False,
    }
    if poison_result_key is not None and poison_result_value is not None:
        success[poison_result_key] = poison_result_value
    return [
        _line(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": glob_id,
                "tool_call": {"globToolCall": {"args": start_args}},
            }
        ),
        _line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": glob_id,
                "tool_call": {
                    "globToolCall": {
                        "args": base_args,
                        "result": {"success": success},
                    }
                },
            }
        ),
    ]


def test_v33_glob_success_execution_evidence_in_pattern_fails_unparsed() -> None:
    stdout = "\n".join(
        _v33_glob_completed_pair_with_poison(
            poison_result_key="pattern",
            poison_result_value='{"stdout":"ran","exitCode":0}',
        )
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v33_glob_success_execution_evidence_in_path_fails_unparsed() -> None:
    stdout = "\n".join(
        _v33_glob_completed_pair_with_poison(
            poison_result_key="path",
            poison_result_value='{"stdout":"ran","exitCode":0}',
        )
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v33_glob_args_execution_evidence_in_glob_pattern_fails_unparsed() -> None:
    poison = '{"stdout":"ran","exitCode":0}'
    stdout = "\n".join(
        _v33_glob_completed_pair_with_poison(
            glob_args={"globPattern": poison, "targetDirectory": "/tmp/ws"},
        )
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v33_glob_args_execution_evidence_in_target_directory_fails_unparsed() -> None:
    poison = '{"stdout":"ran","exitCode":0}'
    stdout = "\n".join(
        _v33_glob_completed_pair_with_poison(
            glob_args={"globPattern": "*", "targetDirectory": poison},
        )
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v33_glob_args_execution_evidence_in_tool_call_id_fails_unparsed() -> None:
    poison = '{"stdout":"ran","exitCode":0}'
    stdout = "\n".join(
        _v33_glob_completed_pair_with_poison(
            glob_args={"globPattern": "*", "targetDirectory": "/tmp/ws", "toolCallId": poison},
        )
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v33_glob_args_non_string_tool_call_id_fails_unparsed() -> None:
    stdout = "\n".join(
        _v33_glob_completed_pair_with_poison(
            glob_args={
                "globPattern": "*",
                "targetDirectory": "/tmp/ws",
                "toolCallId": {"stdout": "ran", "exitCode": 0},
            },
        )
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v33_glob_args_extra_key_fails_unparsed() -> None:
    stdout = "\n".join(
        _v33_glob_completed_pair_with_poison(
            glob_args={"globPattern": "*", "targetDirectory": "/tmp/ws", "nested": {}},
        )
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v33_glob_smuggled_path_with_write_denials_fails_negative_cert() -> None:
    ws = "/private/var/folders/ws/omnigent-gate-a-mcp"
    marker = f"{ws}/.gate_a_trial_write_marker"
    stdout = "\n".join(
        [
            *_v30_observed_write_denial_pair_lines("tool_w1", abs_path=marker),
            *_v30_observed_write_denial_pair_lines("tool_w2", abs_path=marker),
            *_v33_glob_completed_pair_with_poison(
                poison_result_key="path",
                poison_result_value='{"stdout":"ran","exitCode":0}',
                glob_args={"globPattern": "*", "targetDirectory": ws},
                started_glob_args={"globPattern": "*", "targetDirectory": ws},
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not negative_attempt_satisfied("deny_write_without_mcp", summary)


_V34_POISON_CORRELATION_ID = '{"stdout":"ran","exitCode":0}'


def _v34_clean_glob_pair_lines(
    *,
    call_id: str,
    wrapper_tool_call_id: str | None,
    ws: str = "/private/var/folders/ws/omnigent-gate-a-mcp",
) -> list[str]:
    glob_args = {"targetDirectory": ws, "globPattern": "*"}
    success = {
        "pattern": "",
        "path": ws,
        "files": [".git/config", ".git/HEAD"],
        "totalFiles": 2,
        "clientTruncated": False,
        "ripgrepTruncated": False,
    }

    def _wrapper(block: dict) -> dict:
        wrapped: dict = {"globToolCall": block, "hookAdditionalContexts": []}
        if wrapper_tool_call_id is not None:
            wrapped["toolCallId"] = wrapper_tool_call_id
        return wrapped

    return [
        _line(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": call_id,
                "tool_call": _wrapper({"args": glob_args}),
            }
        ),
        _line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": call_id,
                "tool_call": _wrapper(
                    {"args": glob_args, "result": {"success": success}},
                ),
            }
        ),
    ]


def test_v34_glob_matched_call_and_wrapper_id_poison_fails_unparsed() -> None:
    poison = _V34_POISON_CORRELATION_ID
    stdout = "\n".join(
        _v34_clean_glob_pair_lines(call_id=poison, wrapper_tool_call_id=poison),
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v34_glob_call_id_poison_without_wrapper_fails_unparsed() -> None:
    stdout = "\n".join(
        _v34_clean_glob_pair_lines(
            call_id=_V34_POISON_CORRELATION_ID,
            wrapper_tool_call_id=None,
        ),
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v34_glob_wrapper_id_poison_alone_fails_unparsed() -> None:
    stdout = "\n".join(
        _v34_clean_glob_pair_lines(
            call_id="tool_glob_clean",
            wrapper_tool_call_id=_V34_POISON_CORRELATION_ID,
        ),
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants


def test_v34_write_denials_plus_glob_poison_correlation_ids_fail_negative_cert() -> None:
    ws = "/private/var/folders/ws/omnigent-gate-a-mcp"
    marker = f"{ws}/.gate_a_trial_write_marker"
    poison = _V34_POISON_CORRELATION_ID
    stdout = "\n".join(
        [
            *_v30_observed_write_denial_pair_lines("tool_w1", abs_path=marker),
            *_v30_observed_write_denial_pair_lines("tool_w2", abs_path=marker),
            *_v34_clean_glob_pair_lines(
                call_id=poison,
                wrapper_tool_call_id=poison,
                ws=ws,
            ),
        ]
    )
    summary = parse_stream_json(stdout)
    assert summary.unparsed_tool_call_variants
    assert not negative_attempt_satisfied("deny_write_without_mcp", summary)


def test_v34_positive_mcp_baseline_unchanged() -> None:
    mcp_id = "tool_mcp_real"
    baseline_stdout = "\n".join(_v16_clean_mcp_pair_lines(mcp_id))
    baseline_summary = parse_stream_json(baseline_stdout)
    assert positive_gate_a_tool_satisfied(baseline_summary, TOOL_NAME) is not None

    stdout = "\n".join([*_v16_clean_mcp_pair_lines(mcp_id), *_v30_observed_glob_pair_lines()])
    summary = parse_stream_json(stdout)
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_adapter_mode_accepts_brief_under_per_turn_evidence_root(tmp_path: Path) -> None:
    turn_parent = tmp_path / "adapter-turn-evidence"
    turn_parent.mkdir()
    turn_root = canonical_evidence_root(turn_parent)
    artifact = turn_root / f"evidence-adapter-{os.getpid()}" / "allowed.txt"
    artifact.parent.mkdir(parents=True)
    payload = _adapter_turn_payload(artifact)
    summary = parse_stream_json(_adapter_positive_stdout(payload))
    parsed = positive_gate_a_tool_satisfied(
        summary,
        TOOL_NAME,
        mcp_payload_mode=GateAPositiveMcpPayloadMode.ADAPTER,
        evidence_root_parent=turn_parent,
        admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
    )
    assert parsed is not None
    assert parsed["brief_hash"] == INTERNAL_STAGE_BRIEF_HASH
    assert parsed["artifact_path"] == str(artifact)


def test_adapter_mode_rejects_missing_brief_hash(tmp_path: Path) -> None:
    turn_parent = tmp_path / "turn-evidence"
    turn_parent.mkdir()
    turn_root = canonical_evidence_root(turn_parent)
    artifact = turn_root / f"evidence-no-brief-{os.getpid()}" / "allowed.txt"
    artifact.parent.mkdir(parents=True)
    payload = _adapter_turn_payload(artifact)
    del payload["brief_hash"]
    summary = parse_stream_json(_adapter_positive_stdout(payload, call_id="adapter-no-brief"))
    assert (
        positive_gate_a_tool_satisfied(
            summary,
            TOOL_NAME,
            mcp_payload_mode=GateAPositiveMcpPayloadMode.ADAPTER,
            evidence_root_parent=turn_parent,
            admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        )
        is None
    )


def test_adapter_mode_rejects_wrong_brief_hash(tmp_path: Path) -> None:
    turn_parent = tmp_path / "turn-evidence"
    turn_parent.mkdir()
    turn_root = canonical_evidence_root(turn_parent)
    artifact = turn_root / f"evidence-wrong-brief-{os.getpid()}" / "allowed.txt"
    artifact.parent.mkdir(parents=True)
    payload = _adapter_turn_payload(artifact, brief_hash="not-the-canonical-brief-hash")
    summary = parse_stream_json(_adapter_positive_stdout(payload, call_id="adapter-wrong-brief"))
    assert (
        positive_gate_a_tool_satisfied(
            summary,
            TOOL_NAME,
            mcp_payload_mode=GateAPositiveMcpPayloadMode.ADAPTER,
            evidence_root_parent=turn_parent,
            admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        )
        is None
    )


def test_adapter_mode_rejects_shared_default_evidence_root(tmp_path: Path) -> None:
    turn_parent = tmp_path / "turn-evidence"
    turn_parent.mkdir()
    shared_artifact = canonical_evidence_root() / f"evidence-shared-{os.getpid()}" / "allowed.txt"
    shared_artifact.parent.mkdir(parents=True, exist_ok=True)
    payload = _adapter_turn_payload(shared_artifact)
    summary = parse_stream_json(_adapter_positive_stdout(payload, call_id="adapter-shared-root"))
    assert (
        positive_gate_a_tool_satisfied(
            summary,
            TOOL_NAME,
            mcp_payload_mode=GateAPositiveMcpPayloadMode.ADAPTER,
            evidence_root_parent=turn_parent,
            admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        )
        is None
    )


def test_adapter_mode_rejects_extra_payload_key(tmp_path: Path) -> None:
    turn_parent = tmp_path / "turn-evidence"
    turn_parent.mkdir()
    turn_root = canonical_evidence_root(turn_parent)
    artifact = turn_root / f"evidence-extra-key-{os.getpid()}" / "allowed.txt"
    artifact.parent.mkdir(parents=True)
    payload = _adapter_turn_payload(artifact)
    payload["forged"] = True
    summary = parse_stream_json(_adapter_positive_stdout(payload, call_id="adapter-extra-key"))
    assert (
        positive_gate_a_tool_satisfied(
            summary,
            TOOL_NAME,
            mcp_payload_mode=GateAPositiveMcpPayloadMode.ADAPTER,
            evidence_root_parent=turn_parent,
            admitted_brief_hash=INTERNAL_STAGE_BRIEF_HASH,
        )
        is None
    )


def test_legacy_v34_rejects_brief_hash_key() -> None:
    payload = dict(_allowed_payload())
    payload["brief_hash"] = INTERNAL_STAGE_BRIEF_HASH
    summary = parse_stream_json(_adapter_positive_stdout(payload, call_id="legacy-brief-reject"))
    assert positive_gate_a_tool_satisfied(summary, TOOL_NAME) is None


def test_v34_call_id_poison_fails_shell_started_unparsed() -> None:
    stdout = _line(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": _V34_POISON_CORRELATION_ID,
            "tool_call": {
                "shellToolCall": {
                    "args": {"command": "true", "workingDirectory": "/tmp"},
                },
                "toolCallId": _V34_POISON_CORRELATION_ID,
            },
        }
    )
    assert parse_stream_json(stdout).unparsed_tool_call_variants
