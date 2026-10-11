"""Parse Cursor agent ``--output-format stream-json`` stdout.

Gate A release certification predicates (`positive_gate_a_tool_satisfied`,
`negative_attempt_satisfied`, `native_shell_write_calls_denied`) accept only the
installed 2026 CLI shape: matched ``started``/``completed`` pairs per ``call_id``,
no legacy ``tool_call`` rows, and no unparsed variants. Legacy events may still be
parsed for display but never satisfy release predicates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

_ALLOWLIST_POLICY_IN_TEXT_RE = re.compile(
    r"(allowlist|not allowed|rejected by policy)",
    re.IGNORECASE,
)
_SCHEMA_ARGUMENT_REJECTION_RE = re.compile(
    r"(field required|additional properties|extra inputs are not permitted|"
    r"unexpected property|validation error|unexpected argument|forbidden argument)",
    re.IGNORECASE,
)
_RECOGNIZED_TOOL_CALL_VARIANT_KEYS = frozenset(
    {
        "mcpToolCall",
        "shellToolCall",
        "editToolCall",
        "readToolCall",
        "getMcpToolsToolCall",
        "globToolCall",
    },
)
_DENIAL_RESULT_KEYS = frozenset(
    {"rejected", "permissionDenied", "writePermissionDenied", "readPermissionDenied"}
)
_EXECUTION_RESULT_KEYS = frozenset({"success", "failure", "timeout", "spawnError"})
_EXECUTION_EVIDENCE_KEYS = _EXECUTION_RESULT_KEYS | frozenset(
    {"stdout", "stderr", "exitCode", "exit_code", "bytesWritten", "output"}
)
_EXECUTION_EVIDENCE_SURFACE_KEYS = frozenset(
    {"stdout", "stderr", "exitCode", "exit_code", "bytesWritten", "output"}
)
_TEXTUAL_EXECUTION_EVIDENCE_KEY_ALTS = "|".join(
    rf"\b{re.escape(key)}\s*[:=]"
    for key in sorted(_EXECUTION_EVIDENCE_SURFACE_KEYS | _EXECUTION_RESULT_KEYS)
)
_TEXTUAL_EXECUTION_EVIDENCE_RE = re.compile(
    rf"(?:{_TEXTUAL_EXECUTION_EVIDENCE_KEY_ALTS}|\bok\s*[:=]\s*true)",
    re.IGNORECASE,
)
_MCP_TEXT_BLOCK_ALLOWED_KEYS = frozenset({"type", "text"})
_POLICY_TEXT_KEYS = frozenset({"message", "detail", "agent_message", "reason"})
_LEGACY_EVENT_AMBIGUOUS_KEYS = (
    _DENIAL_RESULT_KEYS
    | _EXECUTION_RESULT_KEYS
    | frozenset({"error", "errorMessage"})
    | _POLICY_TEXT_KEYS
    | _EXECUTION_EVIDENCE_SURFACE_KEYS
)
_LEGACY_TOOL_CALL_EVENT_KEYS = frozenset(
    {"type", "name", "status", "args", "result", "call_id"},
)
_STREAM_2026_TOOL_CALL_EVENT_KEYS = frozenset(
    {
        "type",
        "subtype",
        "call_id",
        "tool_call",
        "model_call_id",
        "session_id",
        "timestamp_ms",
    },
)
_STREAM_2026_WRAPPER_METADATA_KEYS = frozenset(
    {"hookAdditionalContexts", "toolCallId", "startedAtMs", "completedAtMs"},
)
_STREAM_2026_BLOCK_STARTED_KEYS = frozenset({"args"})
_STREAM_2026_BLOCK_COMPLETED_KEYS = frozenset({"args", "result"})
_MCP_TOOL_CALL_ARGS_KEYS = frozenset(
    {
        "providerIdentifier",
        "toolName",
        "args",
        "name",
        "serverIdentifier",
        "toolCallId",
        "skipApproval",
        "smartModeApprovalOnly",
    },
)
_GET_MCP_TOOLS_ARGS_KEYS = frozenset({"server", "toolName", "toolCallId"})
_SHELL_TOOL_CALL_ARGS_KEYS = frozenset(
    {
        "adminCommandDenylist",
        "closeStdin",
        "command",
        "conversationId",
        "description",
        "fileOutputThresholdBytes",
        "hardTimeout",
        "hasInputRedirect",
        "hasOutputRedirect",
        "isBackground",
        "parsingResult",
        "requestId",
        "simpleCommands",
        "skipApproval",
        "timeout",
        "timeoutBehavior",
        "toolCallId",
        "workingDirectory",
    },
)
_EDIT_TOOL_CALL_ARGS_KEYS = frozenset({"path", "streamContent"})
_READ_TOOL_CALL_ARGS_KEYS = frozenset({"path"})
_GLOB_TOOL_CALL_ARGS_KEYS = frozenset({"globPattern", "targetDirectory", "toolCallId"})
_GLOB_SUCCESS_VALUE_KEYS = frozenset(
    {
        "clientTruncated",
        "files",
        "path",
        "pattern",
        "ripgrepTruncated",
        "totalFiles",
    },
)
_SHELL_PERMISSION_DENIED_KEYS = frozenset(
    {"command", "error", "isReadonly", "workingDirectory"},
)
_WRITE_PERMISSION_DENIED_KEYS = frozenset({"path", "error", "isReadonly"})
_READ_PERMISSION_DENIED_KEYS = frozenset({"path"})
_MCP_TRANSPORT_ERROR_KEYS = frozenset({"error", "readToolDefReminder", "systemReminders"})
_NESTED_DENIAL_ALLOWED_KEYS = frozenset({"reason", "path", "error"})
_POSITIVE_MCP_PAYLOAD_KEYS = frozenset(
    {
        "ok",
        "artifact",
        "artifact_path",
        "artifact_sha256",
        "child_ok",
        "command_hash",
        "gate_qualified",
        "manifest_hash",
        "order_id",
        "probe_sha256",
        "server_pid",
        "settle_observed_seconds",
        "witness_nonce",
    }
)
_POSITIVE_MCP_PAYLOAD_KEYS_ADAPTER = _POSITIVE_MCP_PAYLOAD_KEYS | frozenset({"brief_hash"})


class GateAPositiveMcpPayloadMode(str, Enum):
    """Which MCP success JSON shape the stream gate accepts."""

    LEGACY_V34 = "legacy_v34"
    ADAPTER = "adapter"


_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
_MCP_2026_SUCCESS_KEYS = frozenset({"content", "isError", "systemReminders"})


@dataclass
class ToolCallObservation:
    name: str
    status: str
    call_id: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    mcp_server: str | None = None


@dataclass
class StreamJsonSummary:
    init_model: str | None = None
    api_key_source: str | None = None
    permission_mode: str | None = None
    tool_calls: list[ToolCallObservation] = field(default_factory=list)
    unparsed_tool_call_variants: list[dict[str, Any]] = field(default_factory=list)
    # 2026 CLI emits matched started/completed pairs; release predicates ignore legacy rows.
    matched_2026_call_ids: set[str] = field(default_factory=set)
    legacy_tool_call_events: int = 0

    def attempted_tool_named(self, *names: str) -> ToolCallObservation | None:
        wanted = {n.casefold() for n in names}
        for call in self.tool_calls:
            if call.status == "running" or call.status in ("completed", "error"):
                if call.name.casefold() in wanted:
                    return call
        return None

    def last_tool_named(self, *names: str) -> ToolCallObservation | None:
        wanted = {n.casefold() for n in names}
        for call in reversed(self.tool_calls):
            if call.name.casefold() in wanted:
                return call
        return None

    def permission_denied_for_tool(self, *names: str) -> bool:
        call = self.last_tool_named(*names)
        if call is None:
            return False
        if call.status == "error":
            return _result_indicates_permission_denial(call.result)
        if call.status == "completed" and _result_indicates_permission_denial(call.result):
            return True
        return False

    def native_shell_write_calls_denied(self) -> bool:
        """Every matched 2026 Shell/Write pair is a structured allowlist denial bound to args."""
        if not _gate_a_release_certification_ready(self):
            return False
        for call in self.tool_calls:
            if call.name.casefold() not in ("shell", "write"):
                continue
            if not _call_id_matched_2026_pair(self, call.call_id):
                return False
            if call.status == "running":
                return False
            if not _structured_allowlist_denial(call.result):
                return False
            if not _native_shell_write_denial_binds_args(call):
                return False
        return True

    def mcp_tool_payload_rejected(self, tool_name: str) -> bool:
        call = self.last_tool_named(tool_name, "mcp")
        if call is None:
            return False
        if _mcp_result_argument_rejected(call, tool_name):
            return True
        return _mcp_result_schema_validation_failed(call, tool_name)


def _unwrap_legacy_tool_call(event: dict[str, Any]) -> ToolCallObservation:
    name = str(event.get("name") or "tool")
    raw_args = event.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    mcp_server: str | None = None
    if "toolName" in args:
        inner_name = str(args.get("toolName") or name)
        provider = args.get("providerIdentifier")
        if isinstance(provider, str) and provider:
            mcp_server = provider
        inner = args.get("args")
        args = inner if isinstance(inner, dict) else {}
        name = inner_name
    return ToolCallObservation(
        name=name,
        status=str(event.get("status") or ""),
        call_id=event.get("call_id") if isinstance(event.get("call_id"), str) else None,
        args=args,
        result=event.get("result"),
        mcp_server=mcp_server,
    )


def _mcp_from_payload(payload: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]] | None:
    tool = payload.get("toolName")
    if not isinstance(tool, str) or not tool:
        return None
    provider = payload.get("providerIdentifier")
    if not isinstance(provider, str) or not provider:
        return None
    mcp_server = provider
    if "args" in payload and not isinstance(payload.get("args"), dict):
        return None
    inner = payload.get("args")
    args = inner if isinstance(inner, dict) else {}
    return tool, mcp_server, args


def _mcp_args_block_state(
    args_block: Any,
) -> tuple[str, str, dict[str, Any]] | None | Literal["malformed"]:
    """Malformed when present but not a valid triple; never pass JSON null here."""
    if args_block is None:
        return "malformed"
    if not isinstance(args_block, dict):
        return "malformed"
    provider = args_block.get("providerIdentifier")
    tool = args_block.get("toolName")
    if not isinstance(provider, str) or not provider:
        return "malformed"
    if not isinstance(tool, str) or not tool:
        return "malformed"
    if "args" in args_block and not isinstance(args_block.get("args"), dict):
        return "malformed"
    inner = args_block.get("args")
    inner_args = inner if isinstance(inner, dict) else {}
    return tool, provider, inner_args


def _mcp_completed_args_state_from_block(
    mcp_block: Any,
) -> tuple[str, str, dict[str, Any]] | None | Literal["malformed"]:
    """None only when the completed mcpToolCall omits args; JSON null is malformed."""
    if not isinstance(mcp_block, dict):
        return "malformed"
    if "args" not in mcp_block:
        return None
    args_block = mcp_block.get("args")
    if args_block is None:
        return "malformed"
    return _mcp_args_block_state(args_block)


def _mcp_identity_from_args_block(
    args_block: Any,
) -> tuple[str, str, dict[str, Any]] | None:
    state = _mcp_args_block_state(args_block)
    if state is None or state == "malformed":
        return None
    return state


def _mcp_identity_from_variant(variant: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    if "mcpToolCall" not in variant:
        return None
    block = variant.get("mcpToolCall")
    if not isinstance(block, dict):
        return None
    return _mcp_identity_from_args_block(block.get("args"))


def _mcp_identity_from_observation(
    call: ToolCallObservation,
) -> tuple[str, str, dict[str, Any]] | None:
    if not call.mcp_server:
        return None
    if not call.name or call.name.casefold() == "mcp":
        return None
    return call.name, call.mcp_server, call.args


def _mcp_stream_identities_agree(
    left: tuple[str, str, dict[str, Any]],
    right: tuple[str, str, dict[str, Any]],
) -> bool:
    return left[0] == right[0] and left[1] == right[1] and left[2] == right[2]


def _bound_gate_a_mcp_identity(
    tool_name: str,
    mcp_server: str,
    inner_args: dict[str, Any],
) -> bool:
    from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME

    return tool_name == TOOL_NAME and mcp_server == MCP_SERVER_NAME and inner_args == {}


def _present_arm(result: dict[str, Any], key: str) -> bool:
    return result.get(key) is not None


def _result_has_conflicting_denial_and_execution(result: dict[str, Any]) -> bool:
    execution = [key for key in _EXECUTION_RESULT_KEYS if _present_arm(result, key)]
    if len(execution) > 1:
        return True
    denial = any(_present_arm(result, key) for key in _DENIAL_RESULT_KEYS)
    return denial and bool(execution)


def _value_has_execution_evidence(val: Any) -> bool:
    if isinstance(val, dict):
        return _dict_has_execution_evidence(val)
    if isinstance(val, list):
        return any(_value_has_execution_evidence(item) for item in val)
    if isinstance(val, str):
        return _string_has_structural_execution_evidence(val)
    return False


def _dict_has_execution_evidence(result: dict[str, Any]) -> bool:
    if result.get("ok") is True:
        return True
    if any(_present_arm(result, key) for key in _EXECUTION_EVIDENCE_KEYS):
        return True
    return any(_value_has_execution_evidence(val) for val in result.values())


def _dict_has_any_key(result: dict[str, Any], keys: frozenset[str]) -> bool:
    return any(_present_arm(result, key) for key in keys)


def _mcp_text_block_exact(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    if set(block.keys()) == _MCP_TEXT_BLOCK_ALLOWED_KEYS:
        if block.get("type") != "text":
            return False
        return isinstance(block.get("text"), str)
    if set(block.keys()) == frozenset({"text"}):
        nested = block.get("text")
        if not isinstance(nested, dict) or set(nested.keys()) != frozenset({"text"}):
            return False
        return isinstance(nested.get("text"), str)
    return False


def _mcp_text_from_block(block: dict[str, Any]) -> str | None:
    if block.get("type") == "text":
        text = block.get("text")
        return text if isinstance(text, str) else None
    nested = block.get("text")
    if isinstance(nested, dict):
        inner = nested.get("text")
        return inner if isinstance(inner, str) else None
    return None


def _mcp_text_block_ambiguous(block: dict[str, Any]) -> bool:
    return not _mcp_text_block_exact(block)


def _mcp_content_list_exact(content: Any) -> bool:
    if not isinstance(content, list) or len(content) != 1:
        return False
    return _mcp_text_block_exact(content[0])


def _mcp_2026_success_envelope_exact(success: Any) -> bool:
    if not isinstance(success, dict) or set(success.keys()) != _MCP_2026_SUCCESS_KEYS:
        return False
    if success.get("isError") is not False:
        return False
    if success.get("systemReminders") != []:
        return False
    return _mcp_content_list_exact(success.get("content"))


def _mcp_result_exact_positive(result: dict[str, Any]) -> bool:
    if set(result.keys()) != frozenset({"success"}):
        return False
    return _mcp_2026_success_envelope_exact(result.get("success"))


def _list_free_of_execution_evidence(items: Any) -> bool:
    if not isinstance(items, list):
        return False
    return not any(_value_has_execution_evidence(item) for item in items)


def _mcp_transport_error_nested_allowed(nested: dict[str, Any]) -> bool:
    if not nested.keys() <= _MCP_TRANSPORT_ERROR_KEYS:
        return False
    message = nested.get("error")
    if not isinstance(message, str) or not message.strip():
        return False
    if _string_has_structural_execution_evidence(message):
        return False
    reminder = nested.get("readToolDefReminder")
    if reminder is not None and not isinstance(reminder, str):
        return False
    if isinstance(reminder, str) and _string_has_structural_execution_evidence(reminder):
        return False
    reminders = nested.get("systemReminders")
    if reminders is not None and not _list_free_of_execution_evidence(reminders):
        return False
    return True


def _mcp_result_exact_transport_error(result: dict[str, Any]) -> bool:
    if len(result) != 1 or "error" not in result:
        return False
    nested = result.get("error")
    if isinstance(nested, str):
        return bool(nested.strip()) and not _string_has_structural_execution_evidence(nested)
    if isinstance(nested, dict):
        return _mcp_transport_error_nested_allowed(nested)
    return False


def _get_mcp_tools_args_bound_lookup(args: dict[str, Any]) -> bool:
    from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME, TOOL_NAME

    if not args.keys() <= _GET_MCP_TOOLS_ARGS_KEYS:
        return False
    server = args.get("server")
    tool = args.get("toolName")
    tool_call_id = args.get("toolCallId")
    if server != MCP_SERVER_NAME or tool != TOOL_NAME:
        return False
    return isinstance(tool_call_id, str) and bool(tool_call_id)


def _get_mcp_tools_schema_lookup_result_allowed(result: Any) -> bool:
    from dev.factory.gate_a_mcp.constants import TOOL_NAME

    if not isinstance(result, dict) or set(result.keys()) != frozenset({"success"}):
        return False
    success = result.get("success")
    if not isinstance(success, dict) or set(success.keys()) != frozenset({"content"}):
        return False
    content = success.get("content")
    if not isinstance(content, str) or not content.strip():
        return False
    try:
        parsed = json.loads(content)
    except ValueError:
        return False
    if not isinstance(parsed, dict) or set(parsed.keys()) != frozenset(
        {"tool", "description", "inputSchema"}
    ):
        return False
    if parsed.get("tool") != TOOL_NAME:
        return False
    description = parsed.get("description")
    if not isinstance(description, str) or not description.strip():
        return False
    schema = parsed.get("inputSchema")
    if not isinstance(schema, dict):
        return False
    if schema.get("type") != "object":
        return False
    if schema.get("properties") != {}:
        return False
    if schema.get("additionalProperties") is not False:
        return False
    if set(schema.keys()) != frozenset({"type", "properties", "additionalProperties"}):
        return False
    return True


def _mcp_result_exact_structured_denial(result: dict[str, Any]) -> bool:
    if len(result) != 1:
        return False
    for key in _DENIAL_RESULT_KEYS:
        if key not in result:
            continue
        nested = result.get(key)
        if isinstance(nested, dict) and _nested_denial_dict_allowed(nested):
            return True
        return False
    return False


def _legacy_result_exact_observation(result: Any) -> bool:
    if isinstance(result, str):
        return _allowlist_policy_text_acceptable(result)
    if isinstance(result, list):
        return _mcp_content_list_exact(result)
    if isinstance(result, dict):
        if set(result.keys()) != frozenset({"content"}):
            return False
        return _mcp_content_list_exact(result.get("content"))
    return False


def _mcp_result_dict_ambiguous(result: dict[str, Any]) -> bool:
    if _mcp_result_exact_positive(result):
        return False
    if _mcp_result_exact_structured_denial(result):
        return False
    if _mcp_result_exact_transport_error(result):
        return False
    return True


def _native_result_dict_ambiguous(result: dict[str, Any]) -> bool:
    if _result_has_conflicting_denial_and_execution(result):
        return True
    if any(
        _present_arm(result, key) for key in _DENIAL_RESULT_KEYS
    ) and _dict_has_execution_evidence(result):
        return True
    if _present_arm(result, "success") and _dict_has_any_key(result, _POLICY_TEXT_KEYS):
        return True
    if _present_arm(result, "success") and result.get("ok") is False:
        return True
    if _dict_has_execution_evidence(result) and any(
        _present_arm(result, key) for key in _DENIAL_RESULT_KEYS
    ):
        return True
    denial_keys = [key for key in _DENIAL_RESULT_KEYS if _present_arm(result, key)]
    if denial_keys and len(result) != 1:
        return True
    return False


def _nested_denial_dict_allowed(nested: dict[str, Any]) -> bool:
    if _dict_has_execution_evidence(nested):
        return False
    if not nested.keys() <= _NESTED_DENIAL_ALLOWED_KEYS:
        return False
    if not all(isinstance(val, str) for val in nested.values()):
        return False
    if any(_string_has_structural_execution_evidence(val) for val in nested.values()):
        return False
    return any(isinstance(nested.get(key), str) for key in _NESTED_DENIAL_ALLOWED_KEYS)


def _text_contains_structural_json_material(text: str) -> bool:
    """Policy denials and positive payload strings must not embed JSON-like structure."""
    return any(ch in text for ch in "{}[]")


def _embedded_json_in_text_has_execution_evidence(text: str) -> bool:
    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    fragment = text[start : index + 1]
                    try:
                        parsed = json.loads(fragment)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict) and _dict_has_execution_evidence(parsed):
                        return True
                    break
        start = text.find("{", start + 1)
    return False


def _string_has_structural_execution_evidence(text: str) -> bool:
    if _text_contains_structural_json_material(text):
        return True
    if _TEXTUAL_EXECUTION_EVIDENCE_RE.search(text):
        return True
    return _embedded_json_in_text_has_execution_evidence(text)


def _allowlist_policy_text_acceptable(text: str) -> bool:
    if not _ALLOWLIST_POLICY_IN_TEXT_RE.search(text):
        return False
    if _text_contains_structural_json_material(text):
        return False
    return not _string_has_structural_execution_evidence(text)


def _native_2026_policy_only_dict(result: dict[str, Any]) -> bool:
    policy_only = [key for key in _POLICY_TEXT_KEYS if isinstance(result.get(key), str)]
    if len(result) != 1 or len(policy_only) != 1:
        return False
    text = result.get(policy_only[0])
    return isinstance(text, str) and _allowlist_policy_text_acceptable(text)


def _shell_timeout_scalar_allowed(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _native_write_denied_path_matches_args(args_path: str, denied_path: str) -> bool:
    if args_path == denied_path:
        return True
    if denied_path.endswith(f"/{args_path.lstrip('/')}"):
        return Path(denied_path).name == Path(args_path).name
    return Path(denied_path).name == Path(args_path).name and Path(args_path).name


def _native_shell_write_denial_binds_args(call: ToolCallObservation) -> bool:
    if not isinstance(call.result, dict):
        return False
    kind = call.name.casefold()
    if kind == "shell":
        rejected = call.result.get("rejected")
        if isinstance(rejected, dict):
            args_command = call.args.get("command")
            denied_command = rejected.get("command")
            if denied_command is not None:
                if not isinstance(args_command, str) or not isinstance(denied_command, str):
                    return False
                return args_command == denied_command
            reason = rejected.get("reason")
            if not isinstance(reason, str) or not _ALLOWLIST_POLICY_IN_TEXT_RE.search(reason):
                return False
            return isinstance(args_command, str) and bool(args_command)
        nested = call.result.get("permissionDenied")
        if not isinstance(nested, dict):
            return False
        args_command = call.args.get("command")
        denied_command = nested.get("command")
        if not isinstance(args_command, str) or not isinstance(denied_command, str):
            return False
        return args_command == denied_command
    if kind == "write":
        nested = call.result.get("writePermissionDenied")
        if isinstance(nested, dict):
            args_path = call.args.get("path")
            denied_path = nested.get("path")
            if not isinstance(args_path, str) or not isinstance(denied_path, str):
                return False
            return _native_write_denied_path_matches_args(args_path, denied_path)
        read_denied = call.result.get("readPermissionDenied")
        if not isinstance(read_denied, dict):
            return False
        args_path = call.args.get("path")
        denied_path = read_denied.get("path")
        if not isinstance(args_path, str) or not isinstance(denied_path, str):
            return False
        return _native_write_denied_path_matches_args(args_path, denied_path)
    return True


def _native_shell_permission_denied_nested(nested: dict[str, Any]) -> bool:
    if not nested.keys() <= _SHELL_PERMISSION_DENIED_KEYS:
        return False
    if nested.get("isReadonly") is not False:
        return False
    command = nested.get("command")
    error = nested.get("error")
    working_directory = nested.get("workingDirectory")
    if not isinstance(command, str) or not command:
        return False
    if error != "Command blocked by permissions configuration":
        return False
    if not isinstance(working_directory, str) or not working_directory:
        return False
    if _string_has_structural_execution_evidence(command):
        return False
    if _string_has_structural_execution_evidence(working_directory):
        return False
    return True


def _read_2026_permission_error_result(result: dict[str, Any]) -> bool:
    if len(result) != 1 or "error" not in result:
        return False
    err = result.get("error")
    if not isinstance(err, dict) or not err.keys() <= frozenset({"errorMessage"}):
        return False
    msg = err.get("errorMessage")
    if not isinstance(msg, str) or "permission" not in msg.casefold():
        return False
    return not _string_has_structural_execution_evidence(msg)


def _native_read_permission_denied_nested(nested: dict[str, Any]) -> bool:
    if not nested.keys() <= _READ_PERMISSION_DENIED_KEYS:
        return False
    path = nested.get("path")
    if not isinstance(path, str) or not path:
        return False
    return not _string_has_structural_execution_evidence(path)


def _native_write_permission_denied_nested(nested: dict[str, Any]) -> bool:
    if not nested.keys() <= _WRITE_PERMISSION_DENIED_KEYS:
        return False
    if nested.get("isReadonly") is not False:
        return False
    path = nested.get("path")
    error = nested.get("error")
    if not isinstance(path, str) or not path:
        return False
    if not isinstance(error, str) or "Blocked by permissions configuration" not in error:
        return False
    if _string_has_structural_execution_evidence(path):
        return False
    if _string_has_structural_execution_evidence(error):
        return False
    return True


def _native_structured_denial_nested_allowed(
    nested: dict[str, Any],
    denial_key: str,
) -> bool:
    if denial_key == "permissionDenied":
        return _native_shell_permission_denied_nested(nested)
    if denial_key == "writePermissionDenied":
        return _native_write_permission_denied_nested(nested)
    if denial_key == "readPermissionDenied":
        return _native_read_permission_denied_nested(nested)
    if _dict_has_execution_evidence(nested):
        return False
    if not nested.keys() <= _NESTED_DENIAL_ALLOWED_KEYS:
        return False
    if not all(isinstance(val, str) for val in nested.values()):
        return False
    if any(_string_has_structural_execution_evidence(val) for val in nested.values()):
        return False
    if denial_key == "rejected":
        reason = nested.get("reason")
        if not isinstance(reason, str) or not _ALLOWLIST_POLICY_IN_TEXT_RE.search(reason):
            return False
    return True


def _native_2026_result_exact_structured_denial(result: dict[str, Any]) -> bool:
    if len(result) != 1:
        return False
    for key in _DENIAL_RESULT_KEYS:
        if key not in result:
            continue
        nested = result.get(key)
        if not isinstance(nested, dict):
            return False
        return _native_structured_denial_nested_allowed(nested, key)
    return False


def _glob_2026_completed_result_is_ambiguous(result: Any) -> bool:
    if not isinstance(result, dict) or len(result) != 1 or "success" not in result:
        return True
    success = result.get("success")
    if not isinstance(success, dict) or not success.keys() <= _GLOB_SUCCESS_VALUE_KEYS:
        return True
    files = success.get("files")
    if not isinstance(files, list):
        return True
    if not all(isinstance(entry, str) for entry in files):
        return True
    if any(_string_has_structural_execution_evidence(entry) for entry in files):
        return True
    total_files = success.get("totalFiles")
    if isinstance(total_files, bool) or not isinstance(total_files, int) or total_files < 0:
        return True
    for key in ("clientTruncated", "ripgrepTruncated"):
        val = success.get(key)
        if val is not None and not isinstance(val, bool):
            return True
    for key in ("pattern", "path"):
        val = success.get(key)
        if val is not None and not isinstance(val, str):
            return True
        if isinstance(val, str) and _string_has_structural_execution_evidence(val):
            return True
    return False


def _native_2026_completed_result_is_ambiguous(result: Any) -> bool:
    if isinstance(result, str):
        return not _allowlist_policy_text_acceptable(result)
    if not isinstance(result, dict):
        return True
    if _read_2026_permission_error_result(result):
        return False
    if _native_2026_result_exact_structured_denial(result):
        return False
    if _native_2026_policy_only_dict(result):
        return False
    return True


def _native_args_from_variant(
    variant: dict[str, Any], kind: str
) -> dict[str, Any] | None | Literal["malformed"]:
    if kind == "shell":
        key = "shellToolCall"
    elif kind == "read":
        key = "readToolCall"
    elif kind == "glob":
        key = "globToolCall"
    else:
        key = "editToolCall"
    block = variant.get(key)
    if not isinstance(block, dict):
        return None
    if "args" not in block:
        return None
    raw_args = block.get("args")
    if raw_args is None:
        return "malformed"
    if not isinstance(raw_args, dict):
        return "malformed"
    return raw_args


def _native_stream_args_agree(
    started_args: dict[str, Any],
    completed_args: dict[str, Any],
    kind: str,
) -> bool:
    if kind == "shell":
        return (
            started_args.get("command") == completed_args.get("command")
            and started_args.get("workingDirectory") == completed_args.get("workingDirectory")
            and started_args.get("timeout") == completed_args.get("timeout")
        )
    if kind == "read":
        return started_args.get("path") == completed_args.get("path")
    if kind == "glob":
        return started_args.get("globPattern") == completed_args.get(
            "globPattern"
        ) and started_args.get("targetDirectory") == completed_args.get("targetDirectory")
    return started_args.get("path") == completed_args.get("path") and started_args.get(
        "streamContent"
    ) == completed_args.get("streamContent")


def _clean_positive_mcp_payload(
    obj: Any,
    *,
    mode: GateAPositiveMcpPayloadMode = GateAPositiveMcpPayloadMode.LEGACY_V34,
    evidence_root_parent: Path | None = None,
    admitted_brief_hash: str | None = None,
) -> dict[str, Any] | None:
    from dev.factory.gate_a_mcp.checkout import artifact_path_under_canonical_evidence_root
    from dev.factory.gate_a_mcp.constants import BOUND_ARTIFACT_FILENAME
    from dev.factory.order_scoped.binding import INTERNAL_STAGE_ORDER_ID
    from dev.factory.seatbelt_fixture.manifest import GATE_A_MIN_SETTLE_SECONDS

    if not isinstance(obj, dict):
        return None
    if mode == GateAPositiveMcpPayloadMode.ADAPTER:
        if evidence_root_parent is None or not admitted_brief_hash:
            return None
        expected_keys = _POSITIVE_MCP_PAYLOAD_KEYS_ADAPTER
        root_parent = evidence_root_parent
    else:
        expected_keys = _POSITIVE_MCP_PAYLOAD_KEYS
        root_parent = None
    if set(obj.keys()) != expected_keys:
        return None
    if obj.get("ok") is not True:
        return None
    if obj.get("child_ok") is not True:
        return None
    if obj.get("gate_qualified") is not True:
        return None
    if obj.get("artifact") != BOUND_ARTIFACT_FILENAME:
        return None
    if obj.get("order_id") != INTERNAL_STAGE_ORDER_ID:
        return None
    settle = obj.get("settle_observed_seconds")
    if isinstance(settle, bool) or not isinstance(settle, (int, float)):
        return None
    if float(settle) < GATE_A_MIN_SETTLE_SECONDS:
        return None
    if mode == GateAPositiveMcpPayloadMode.ADAPTER:
        brief_hash = obj.get("brief_hash")
        if not isinstance(brief_hash, str) or not brief_hash.strip():
            return None
        if brief_hash != admitted_brief_hash:
            return None
    artifact_path = obj.get("artifact_path")
    if not isinstance(artifact_path, str) or not artifact_path:
        return None
    under, _, _ = artifact_path_under_canonical_evidence_root(
        artifact_path,
        parent=root_parent,
    )
    if not under:
        return None
    for hash_key in ("artifact_sha256", "command_hash", "manifest_hash", "probe_sha256"):
        digest = obj.get(hash_key)
        if not isinstance(digest, str) or not _SHA256_HEX_RE.fullmatch(digest):
            return None
    witness_nonce = obj.get("witness_nonce")
    if not isinstance(witness_nonce, str) or not witness_nonce.strip():
        return None
    server_pid = obj.get("server_pid")
    if isinstance(server_pid, bool) or not isinstance(server_pid, int) or server_pid <= 0:
        return None
    if "error" in obj or "isError" in obj:
        return None
    for key in ("artifact", "artifact_path", "order_id"):
        val = obj.get(key)
        if not isinstance(val, str) or _string_has_structural_execution_evidence(val):
            return None
    return obj


def _non_tool_call_dict_carries_tool_evidence(node: dict[str, Any]) -> bool:
    if node.get("type") == "tool_use":
        return True
    if "tool_call" in node:
        return True
    if _dict_has_any_key(node, _RECOGNIZED_TOOL_CALL_VARIANT_KEYS):
        return True
    call_id = node.get("call_id")
    subtype = node.get("subtype")
    if isinstance(call_id, str) and call_id and subtype in ("started", "completed"):
        return True
    name = node.get("name")
    if isinstance(name, str) and name:
        if "status" in node or "args" in node or "result" in node or "input" in node:
            return True
        if _dict_has_any_key(node, _EXECUTION_EVIDENCE_SURFACE_KEYS):
            return True
    if _dict_has_any_key(node, _EXECUTION_EVIDENCE_KEYS):
        return True
    return any(_non_tool_call_value_carries_tool_evidence(val) for val in node.values())


def _non_tool_call_value_carries_tool_evidence(val: Any) -> bool:
    if isinstance(val, dict):
        return _non_tool_call_dict_carries_tool_evidence(val)
    if isinstance(val, list):
        return any(_non_tool_call_value_carries_tool_evidence(item) for item in val)
    return False


def _non_tool_call_event_carries_tool_evidence(event: dict[str, Any]) -> bool:
    return _non_tool_call_dict_carries_tool_evidence(event)


def _legacy_tool_call_certification_ambiguous(event: dict[str, Any]) -> bool:
    if not event.keys() <= _LEGACY_TOOL_CALL_EVENT_KEYS:
        return True
    if _present_arm(event, "error") or _present_arm(event, "errorMessage"):
        return True
    if event.get("isError") is True:
        return True
    if event.get("ok") is False:
        return True
    if _dict_has_any_key(event, _LEGACY_EVENT_AMBIGUOUS_KEYS):
        return True
    raw_args = event.get("args")
    if isinstance(raw_args, dict):
        if _dict_has_execution_evidence(raw_args):
            return True
        name = event.get("name")
        if isinstance(name, str):
            kind = name.casefold()
            if kind == "shell" and not _native_tool_call_arg_values_well_typed("shell", raw_args):
                return True
            if kind == "write" and not _native_tool_call_arg_values_well_typed("write", raw_args):
                return True
    legacy_result = event.get("result")
    if isinstance(legacy_result, str) and not _allowlist_policy_text_acceptable(legacy_result):
        if _ALLOWLIST_POLICY_IN_TEXT_RE.search(legacy_result):
            return True
    if not _legacy_result_exact_observation(legacy_result):
        return True
    return False


def _stream_2026_tool_call_wrapper_ambiguous(
    variant: dict[str, Any],
    call_id: str | None = None,
) -> bool:
    recognized = [key for key in variant if key in _RECOGNIZED_TOOL_CALL_VARIANT_KEYS]
    if len(recognized) != 1:
        return True
    variant_key = recognized[0]
    extra = set(variant.keys()) - {variant_key}
    if not extra <= _STREAM_2026_WRAPPER_METADATA_KEYS:
        return True
    hook = variant.get("hookAdditionalContexts")
    if hook is not None and not _list_free_of_execution_evidence(hook):
        return True
    tool_call_id = variant.get("toolCallId")
    if tool_call_id is not None:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return True
        if _string_has_structural_execution_evidence(tool_call_id):
            return True
        if isinstance(call_id, str) and call_id and tool_call_id != call_id:
            return True
    for ms_key in ("startedAtMs", "completedAtMs"):
        val = variant.get(ms_key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            continue
        if isinstance(val, str) and val.isdigit():
            continue
        return True
    return False


def _native_tool_call_arg_values_well_typed(kind: str, raw_args: dict[str, Any]) -> bool:
    if kind == "shell":
        if "command" in raw_args and not isinstance(raw_args.get("command"), str):
            return False
        if "timeout" in raw_args and not _shell_timeout_scalar_allowed(raw_args.get("timeout")):
            return False
    elif kind == "write":
        for key in ("path", "streamContent"):
            if key in raw_args and not isinstance(raw_args.get(key), str):
                return False
    elif kind == "read":
        if "path" in raw_args and not isinstance(raw_args.get("path"), str):
            return False
    elif kind == "glob":
        for key in ("globPattern", "targetDirectory", "toolCallId"):
            if key in raw_args and not isinstance(raw_args.get(key), str):
                return False
    return True


def _stream_2026_variant_args_keys_allowed(kind: str) -> frozenset[str]:
    if kind == "mcp":
        return _MCP_TOOL_CALL_ARGS_KEYS
    if kind == "shell":
        return _SHELL_TOOL_CALL_ARGS_KEYS
    if kind == "write":
        return _EDIT_TOOL_CALL_ARGS_KEYS
    if kind == "read":
        return _READ_TOOL_CALL_ARGS_KEYS
    if kind == "get_mcp_tools":
        return _GET_MCP_TOOLS_ARGS_KEYS
    if kind == "glob":
        return _GLOB_TOOL_CALL_ARGS_KEYS
    return frozenset()


def _tool_call_args_carry_execution_evidence(args: dict[str, Any]) -> bool:
    for key, val in args.items():
        if key in _EXECUTION_EVIDENCE_SURFACE_KEYS:
            return True
        if key in frozenset({"success", "failure", "spawnError"}):
            return True
        if key == "timeout":
            if not _shell_timeout_scalar_allowed(val):
                return True
            if isinstance(val, dict) and _dict_has_execution_evidence(val):
                return True
            continue
        if isinstance(val, dict) and _dict_has_execution_evidence(val):
            return True
        if isinstance(val, list):
            if any(_value_has_execution_evidence(item) for item in val):
                return True
        elif isinstance(val, str) and _string_has_structural_execution_evidence(val):
            return True
    return False


def _stream_2026_variant_args_layer_ambiguous(kind: str, raw_args: Any) -> bool:
    if not isinstance(raw_args, dict):
        return True
    if not raw_args.keys() <= _stream_2026_variant_args_keys_allowed(kind):
        return True
    if kind in ("shell", "write", "read", "glob") and not _native_tool_call_arg_values_well_typed(
        kind, raw_args
    ):
        return True
    if kind == "get_mcp_tools" and not _get_mcp_tools_args_bound_lookup(raw_args):
        return True
    if kind in ("shell", "write", "mcp", "glob") and _tool_call_args_carry_execution_evidence(
        raw_args
    ):
        return True
    return False


def _stream_2026_variant_block_layer_ambiguous(
    variant: dict[str, Any],
    subtype: str,
) -> bool:
    kind = _tool_call_variant_kind(variant)
    if kind is None:
        return True
    variant_key = next(key for key in variant if key in _RECOGNIZED_TOOL_CALL_VARIANT_KEYS)
    block = variant.get(variant_key)
    if not isinstance(block, dict):
        return True
    if subtype == "started":
        allowed_started = set(_STREAM_2026_BLOCK_STARTED_KEYS)
        if kind in ("shell", "mcp"):
            allowed_started.add("description")
        if not block.keys() <= allowed_started:
            return True
        if kind in ("shell", "mcp") and "description" in block:
            description = block.get("description")
            if not isinstance(description, str):
                return True
            if _string_has_structural_execution_evidence(description):
                return True
    elif subtype == "completed":
        allowed_completed = set(_STREAM_2026_BLOCK_COMPLETED_KEYS)
        if kind == "mcp":
            allowed_completed.add("description")
        if not block.keys() <= allowed_completed:
            return True
        if kind == "mcp" and "description" in block:
            description = block.get("description")
            if not isinstance(description, str):
                return True
            if _string_has_structural_execution_evidence(description):
                return True
    else:
        return True
    if "args" not in block:
        return False
    raw_args = block.get("args")
    if raw_args is None:
        return True
    return _stream_2026_variant_args_layer_ambiguous(kind, raw_args)


def _stream_2026_tool_call_event_metadata_allowed(key: str, val: Any) -> bool:
    if key == "timestamp_ms":
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return True
        return isinstance(val, str) and val.isdigit()
    if key in ("call_id", "model_call_id", "session_id"):
        return (
            isinstance(val, str)
            and bool(val)
            and not _string_has_structural_execution_evidence(val)
        )
    return True


def _stream_2026_tool_call_event_layer_ambiguous(event: dict[str, Any]) -> bool:
    if not event.keys() <= _STREAM_2026_TOOL_CALL_EVENT_KEYS:
        return True
    if not _stream_2026_tool_call_event_metadata_allowed("call_id", event.get("call_id")):
        return True
    for meta_key in ("timestamp_ms", "model_call_id", "session_id"):
        if meta_key not in event:
            continue
        if not _stream_2026_tool_call_event_metadata_allowed(meta_key, event.get(meta_key)):
            return True
    return False


def _stream_2026_tool_call_layers_ambiguous(
    event: dict[str, Any],
    subtype: str,
    tool_call: dict[str, Any],
) -> bool:
    if _stream_2026_tool_call_event_layer_ambiguous(event):
        return True
    call_id = event.get("call_id")
    if _stream_2026_tool_call_wrapper_ambiguous(
        tool_call, call_id if isinstance(call_id, str) else None
    ):
        return True
    return _stream_2026_variant_block_layer_ambiguous(tool_call, subtype)


def _completed_variant_result_is_ambiguous(variant: dict[str, Any]) -> bool:
    for key in _RECOGNIZED_TOOL_CALL_VARIANT_KEYS:
        if key not in variant:
            continue
        block = variant.get(key)
        if not isinstance(block, dict):
            continue
        result = block.get("result")
        if key == "mcpToolCall":
            if not isinstance(result, dict) or _mcp_result_dict_ambiguous(result):
                return True
            continue
        if key in ("shellToolCall", "editToolCall", "readToolCall"):
            if _native_2026_completed_result_is_ambiguous(result):
                return True
            continue
        if key == "globToolCall":
            if _glob_2026_completed_result_is_ambiguous(result):
                return True
            continue
        if not isinstance(result, dict):
            continue
    return False


def _tool_call_variant_kind(variant: dict[str, Any]) -> str | None:
    if "mcpToolCall" in variant:
        return "mcp"
    if "shellToolCall" in variant:
        return "shell"
    if "editToolCall" in variant:
        return "write"
    if "getMcpToolsToolCall" in variant:
        return "get_mcp_tools"
    if "readToolCall" in variant:
        return "read"
    if "globToolCall" in variant:
        return "glob"
    return None


def _observation_from_tool_call_variant(
    variant: dict[str, Any],
) -> ToolCallObservation | None:
    if "mcpToolCall" in variant:
        block = variant.get("mcpToolCall")
        if not isinstance(block, dict):
            return None
        args_block = block.get("args")
        if isinstance(args_block, dict):
            parsed = _mcp_from_payload(args_block)
            if parsed is None:
                return None
            name, mcp_server, args = parsed
        else:
            name, mcp_server, args = "mcp", None, {}
        return ToolCallObservation(name=name, status="", args=args, mcp_server=mcp_server)
    if "shellToolCall" in variant:
        block = variant.get("shellToolCall")
        args: dict[str, Any] = {}
        if isinstance(block, dict):
            raw = block.get("args")
            if isinstance(raw, dict):
                args = raw
        return ToolCallObservation(name="Shell", status="", args=args)
    if "editToolCall" in variant:
        block = variant.get("editToolCall")
        args = {}
        if isinstance(block, dict):
            raw = block.get("args")
            if isinstance(raw, dict):
                args = raw
        return ToolCallObservation(name="Write", status="", args=args)
    if "readToolCall" in variant:
        block = variant.get("readToolCall")
        args: dict[str, Any] = {}
        if isinstance(block, dict):
            raw = block.get("args")
            if isinstance(raw, dict):
                args = raw
        return ToolCallObservation(name="Read", status="", args=args)
    if "globToolCall" in variant:
        block = variant.get("globToolCall")
        args = {}
        if isinstance(block, dict):
            raw = block.get("args")
            if isinstance(raw, dict):
                args = raw
        return ToolCallObservation(name="Glob", status="", args=args)
    return None


def _mcp_result_from_block(block: dict[str, Any]) -> Any:
    result = block.get("result")
    if not isinstance(result, dict):
        return result
    rejected = result.get("rejected")
    if isinstance(rejected, dict):
        reason = rejected.get("reason")
        if isinstance(reason, str):
            return reason
        return rejected
    success = result.get("success")
    if isinstance(success, dict):
        content = success.get("content")
        if content is not None:
            return content
        return success
    return result


def _completed_status_and_result(
    variant: dict[str, Any],
    base: ToolCallObservation,
) -> tuple[str, Any]:
    if "mcpToolCall" in variant:
        block = variant.get("mcpToolCall")
        if isinstance(block, dict):
            raw_result = block.get("result")
            if isinstance(raw_result, dict) and _mcp_result_exact_transport_error(raw_result):
                return "error", raw_result
            result = _mcp_result_from_block(block)
            if isinstance(result, str) and _ALLOWLIST_POLICY_IN_TEXT_RE.search(result):
                return "error", result
            if isinstance(block.get("result"), dict) and isinstance(
                block["result"].get("rejected"), dict
            ):
                return "error", result
            return "completed", result
    if "readToolCall" in variant:
        block = variant.get("readToolCall")
        if isinstance(block, dict):
            result = block.get("result")
            if isinstance(result, dict):
                rejected = result.get("rejected")
                if isinstance(rejected, dict):
                    return "error", result
                if isinstance(result.get("permissionDenied"), dict):
                    return "error", result
                if _read_2026_permission_error_result(result):
                    return "error", result
            if isinstance(result, str) and _ALLOWLIST_POLICY_IN_TEXT_RE.search(result):
                return "error", result
            return "completed", result
    if "shellToolCall" in variant or "editToolCall" in variant:
        key = "shellToolCall" if "shellToolCall" in variant else "editToolCall"
        block = variant.get(key)
        if isinstance(block, dict):
            result = block.get("result")
            if isinstance(result, dict):
                rejected = result.get("rejected")
                if isinstance(rejected, dict):
                    return "error", result
                if isinstance(result.get("permissionDenied"), dict):
                    return "error", result
                if isinstance(result.get("writePermissionDenied"), dict):
                    return "error", result
                if isinstance(result.get("readPermissionDenied"), dict):
                    return "error", result
            if isinstance(result, str) and _ALLOWLIST_POLICY_IN_TEXT_RE.search(result):
                return "error", result
            return "completed", result
    if "globToolCall" in variant:
        block = variant.get("globToolCall")
        if isinstance(block, dict):
            result = block.get("result")
            if _glob_2026_completed_result_is_ambiguous(result):
                return "error", result
            return "completed", result
    return "completed", base.result


def _merge_stream_tool_call(
    event: dict[str, Any],
    pending: dict[str, ToolCallObservation],
    pending_kinds: dict[str, str],
    completed: list[ToolCallObservation],
    unparsed_variants: list[dict[str, Any]],
    matched_2026_call_ids: set[str],
) -> None:
    subtype = event.get("subtype")
    if subtype not in ("started", "completed"):
        return
    call_id = event.get("call_id")
    tool_call = event.get("tool_call")
    if not isinstance(call_id, str) or not call_id or not isinstance(tool_call, dict):
        unparsed_variants.append(
            {
                "call_id": call_id,
                "tool_call": tool_call,
                "subtype": subtype,
            }
        )
        return
    if _stream_2026_tool_call_layers_ambiguous(event, subtype, tool_call):
        unparsed_variants.append(tool_call)
        return

    completed_kind = _tool_call_variant_kind(tool_call)
    if completed_kind is None:
        unparsed_variants.append(tool_call)
        return

    base: ToolCallObservation | None
    if completed_kind == "get_mcp_tools":
        lookup_block = tool_call.get("getMcpToolsToolCall")
        lookup_args = (
            lookup_block.get("args")
            if isinstance(lookup_block, dict) and isinstance(lookup_block.get("args"), dict)
            else {}
        )
        base = ToolCallObservation(
            name="GetMcpTools",
            status="",
            call_id=call_id,
            args=lookup_args,
        )
    else:
        base = _observation_from_tool_call_variant(tool_call)
        if base is None:
            unparsed_variants.append(tool_call)
            return

    if subtype == "completed":
        started_kind_peek = pending_kinds.get(call_id)
        if started_kind_peek is not None and started_kind_peek != completed_kind:
            pending.pop(call_id, None)
            pending_kinds.pop(call_id, None)
            unparsed_variants.append(
                {
                    "call_id": call_id,
                    "started_kind": started_kind_peek,
                    "completed_kind": completed_kind,
                    "tool_call": tool_call,
                }
            )
            return
        if completed_kind == "get_mcp_tools":
            block = tool_call.get("getMcpToolsToolCall")
            result = block.get("result") if isinstance(block, dict) else None
            if not _get_mcp_tools_schema_lookup_result_allowed(result):
                pending.pop(call_id, None)
                pending_kinds.pop(call_id, None)
                unparsed_variants.append(tool_call)
                return
        elif _completed_variant_result_is_ambiguous(tool_call):
            unparsed_variants.append(tool_call)
            return

    if subtype == "started":
        if call_id in pending:
            unparsed_variants.append(
                {
                    "reason": "duplicate_tool_call_start",
                    "call_id": call_id,
                    "started_kind": pending_kinds.get(call_id),
                    "tool_call": tool_call,
                }
            )
            return
        pending[call_id] = ToolCallObservation(
            name=base.name,
            status="running",
            call_id=call_id,
            args=base.args,
            mcp_server=base.mcp_server,
        )
        pending_kinds[call_id] = completed_kind
        return

    if completed_kind == "get_mcp_tools":
        existing_lookup = pending.pop(call_id, None)
        started_kind = pending_kinds.pop(call_id, None)
        if existing_lookup is None or started_kind != "get_mcp_tools":
            unparsed_variants.append(
                {
                    "reason": "get_mcp_tools_completed_without_started",
                    "call_id": call_id,
                    "tool_call": tool_call,
                }
            )
            return
        completed_block = tool_call.get("getMcpToolsToolCall")
        if not isinstance(completed_block, dict):
            unparsed_variants.append(tool_call)
            return
        started_args = existing_lookup.args
        completed_args = completed_block.get("args")
        if (
            not isinstance(started_args, dict)
            or not isinstance(completed_args, dict)
            or started_args != completed_args
        ):
            unparsed_variants.append(
                {
                    "reason": "get_mcp_tools_started_completed_args_mismatch",
                    "call_id": call_id,
                    "tool_call": tool_call,
                }
            )
            return
        matched_2026_call_ids.add(call_id)
        return

    existing = pending.pop(call_id, None)
    if existing is None and completed_kind in ("mcp", "shell", "write", "read", "glob"):
        unparsed_variants.append(
            {
                "reason": "completed_without_started",
                "call_id": call_id,
                "completed_kind": completed_kind,
                "tool_call": tool_call,
            }
        )
        return
    started_kind = pending_kinds.pop(call_id, None)
    if started_kind is not None and started_kind != completed_kind:
        unparsed_variants.append(
            {
                "call_id": call_id,
                "started_kind": started_kind,
                "completed_kind": completed_kind,
                "tool_call": tool_call,
            }
        )
        return
    if (
        existing is not None
        and completed_kind in ("shell", "write", "read", "glob")
        and subtype == "completed"
    ):
        completed_args = _native_args_from_variant(tool_call, completed_kind)
        if completed_args == "malformed":
            unparsed_variants.append(
                {
                    "reason": "native_completed_args_malformed",
                    "call_id": call_id,
                    "tool_call": tool_call,
                }
            )
            return
        if completed_args is not None and not _native_stream_args_agree(
            existing.args, completed_args, completed_kind
        ):
            unparsed_variants.append(
                {
                    "reason": "native_started_completed_args_mismatch",
                    "call_id": call_id,
                    "started_args": existing.args,
                    "completed_args": completed_args,
                    "tool_call": tool_call,
                }
            )
            return

    if completed_kind == "mcp":
        mcp_block = tool_call.get("mcpToolCall")
        completed_args_state = _mcp_completed_args_state_from_block(mcp_block)
        if existing is not None:
            started_identity = _mcp_identity_from_observation(existing)
            if started_identity is None:
                unparsed_variants.append(
                    {
                        "reason": "mcp_started_without_identity",
                        "call_id": call_id,
                        "tool_call": tool_call,
                    }
                )
                return
            if completed_args_state == "malformed":
                unparsed_variants.append(
                    {
                        "reason": "mcp_completed_args_malformed",
                        "call_id": call_id,
                        "tool_call": tool_call,
                    }
                )
                return
            if completed_args_state is None:
                identity = started_identity
            elif not _mcp_stream_identities_agree(started_identity, completed_args_state):
                unparsed_variants.append(
                    {
                        "reason": "mcp_started_completed_identity_mismatch",
                        "call_id": call_id,
                        "tool_call": tool_call,
                    }
                )
                return
            else:
                identity = started_identity
        else:
            if completed_args_state is None:
                unparsed_variants.append(
                    {
                        "reason": "mcp_completed_without_identity",
                        "call_id": call_id,
                        "tool_call": tool_call,
                    }
                )
                return
            if completed_args_state == "malformed":
                unparsed_variants.append(
                    {
                        "reason": "mcp_completed_args_malformed",
                        "call_id": call_id,
                        "tool_call": tool_call,
                    }
                )
                return
            identity = completed_args_state
        tool_name, mcp_server, inner_args = identity
        obs = ToolCallObservation(
            name=tool_name,
            status=existing.status if existing is not None else "",
            call_id=call_id,
            args=inner_args,
            mcp_server=mcp_server,
        )
    elif existing is not None:
        obs = ToolCallObservation(
            name=existing.name,
            status=existing.status,
            call_id=call_id,
            args=existing.args,
            mcp_server=existing.mcp_server,
        )
    else:
        obs = ToolCallObservation(
            name=base.name,
            status="",
            call_id=call_id,
            args=base.args,
            mcp_server=base.mcp_server,
        )
    status, result = _completed_status_and_result(tool_call, obs)
    obs.status = status
    obs.result = result
    matched_2026_call_ids.add(call_id)
    completed.append(obs)


def _structured_allowlist_denial(result: Any) -> bool:
    """Structured Cursor denial or explicit allowlist/policy reason — not generic OS errors."""
    if result is None:
        return False
    if isinstance(result, dict):
        if _read_2026_permission_error_result(result):
            return True
        if _native_2026_result_exact_structured_denial(result):
            return True
        if _native_2026_policy_only_dict(result):
            return True
        if _native_result_dict_ambiguous(result):
            return False
        denial_keys = [key for key in _DENIAL_RESULT_KEYS if _present_arm(result, key)]
        if len(denial_keys) == 1 and len(result) == 1:
            nested = result.get(denial_keys[0])
            if isinstance(nested, dict) and _nested_denial_dict_allowed(nested):
                return True
        policy_only = [key for key in _POLICY_TEXT_KEYS if isinstance(result.get(key), str)]
        if len(result) == 1 and len(policy_only) == 1:
            text = result.get(policy_only[0])
            if isinstance(text, str) and _allowlist_policy_text_acceptable(text):
                return True
        return False
    if isinstance(result, str):
        return _allowlist_policy_text_acceptable(result)
    if isinstance(result, list):
        saw_policy = False
        for block in result:
            if not isinstance(block, dict) or block.get("type") != "text":
                return False
            if any(_present_arm(block, key) for key in _EXECUTION_EVIDENCE_KEYS):
                return False
            text = block.get("text")
            if not isinstance(text, str) or not _allowlist_policy_text_acceptable(text):
                return False
            saw_policy = True
        return saw_policy
    return False


def _result_indicates_permission_denial(result: Any) -> bool:
    return _structured_allowlist_denial(result)


def _mcp_result_argument_rejected(call: ToolCallObservation, tool_name: str) -> bool:
    if call.name.casefold() != tool_name.casefold():
        return False
    payload = _extract_json_payload_loose(call.result)
    if not isinstance(payload, dict):
        return False
    return payload.get("ok") is False and payload.get("error") == "argument_rejected"


def _result_as_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if isinstance(block, dict):
                text = _mcp_text_from_block(block)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _mcp_result_schema_validation_failed(call: ToolCallObservation, tool_name: str) -> bool:
    if call.name.casefold() != tool_name.casefold():
        return False
    if call.status not in ("completed", "error"):
        return False
    text = _result_as_text(call.result)
    if not text:
        return False
    if text.strip().startswith("Error:"):
        return bool(_SCHEMA_ARGUMENT_REJECTION_RE.search(text))
    return bool(_SCHEMA_ARGUMENT_REJECTION_RE.search(text))


def _parse_mcp_text_content_list(result: list[Any]) -> Any:
    if len(result) != 1:
        return None
    block = result[0]
    if not isinstance(block, dict) or _mcp_text_block_ambiguous(block):
        return None
    text = _mcp_text_from_block(block)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _extract_json_payload_loose(result: Any) -> Any:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return None
    if isinstance(result, list):
        text_blocks: list[str] = []
        for block in result:
            if isinstance(block, dict):
                text = _mcp_text_from_block(block)
                if isinstance(text, str):
                    text_blocks.append(text)
        if len(text_blocks) != 1:
            return None
        try:
            return json.loads(text_blocks[0])
        except ValueError:
            return None
    return None


def _extract_json_payload(
    result: Any,
    *,
    mode: GateAPositiveMcpPayloadMode = GateAPositiveMcpPayloadMode.LEGACY_V34,
    evidence_root_parent: Path | None = None,
    admitted_brief_hash: str | None = None,
) -> Any:
    clean_kwargs = {
        "mode": mode,
        "evidence_root_parent": evidence_root_parent,
        "admitted_brief_hash": admitted_brief_hash,
    }
    if isinstance(result, dict):
        return _clean_positive_mcp_payload(result, **clean_kwargs)
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            return None
        return _clean_positive_mcp_payload(parsed, **clean_kwargs)
    if isinstance(result, list):
        parsed = _parse_mcp_text_content_list(result)
        if parsed is None:
            return None
        return _clean_positive_mcp_payload(parsed, **clean_kwargs)
    return None


def parse_stream_json(stdout: str) -> StreamJsonSummary:
    summary = StreamJsonSummary()
    pending: dict[str, ToolCallObservation] = {}
    pending_kinds: dict[str, str] = {}
    completed: list[ToolCallObservation] = []
    unparsed_variants: list[dict[str, Any]] = []
    matched_2026_call_ids: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            unparsed_variants.append({"reason": "invalid_json", "line": line})
            continue
        if not isinstance(event, dict):
            unparsed_variants.append({"reason": "non_dict_json", "parsed": event})
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str) and model:
                summary.init_model = model
            api_src = event.get("apiKeySource")
            if isinstance(api_src, str) and api_src:
                summary.api_key_source = api_src
            perm_mode = event.get("permissionMode")
            if isinstance(perm_mode, str) and perm_mode:
                summary.permission_mode = perm_mode
        if event.get("type") != "tool_call":
            if _non_tool_call_event_carries_tool_evidence(event):
                unparsed_variants.append(
                    {
                        "reason": "tool_call_evidence_on_non_tool_call_event",
                        "type": event.get("type"),
                        "tool_call": event.get("tool_call"),
                    }
                )
            continue
        subtype = event.get("subtype")
        if subtype in ("started", "completed"):
            _merge_stream_tool_call(
                event,
                pending,
                pending_kinds,
                completed,
                unparsed_variants,
                matched_2026_call_ids,
            )
            continue
        if "tool_call" in event or subtype is not None:
            unparsed_variants.append(
                {
                    "reason": "unrecognized_tool_call_subtype",
                    "subtype": subtype,
                    "tool_call": event.get("tool_call"),
                }
            )
            continue
        if _legacy_tool_call_certification_ambiguous(event):
            unparsed_variants.append(
                {
                    "reason": "legacy_tool_call_ambiguous",
                    "event": event,
                }
            )
            continue
        summary.tool_calls.append(_unwrap_legacy_tool_call(event))
        summary.legacy_tool_call_events += 1
    for dangling_id in pending:
        unparsed_variants.append(
            {
                "reason": "dangling_tool_call_start",
                "call_id": dangling_id,
                "started_kind": pending_kinds.get(dangling_id),
            }
        )
    summary.tool_calls.extend(pending.values())
    summary.tool_calls.extend(completed)
    summary.unparsed_tool_call_variants = unparsed_variants
    summary.matched_2026_call_ids = matched_2026_call_ids
    return summary


def headless_stream_final_result_acceptable(stdout: str) -> tuple[bool, str]:
    """Require a terminal ``type=result`` / ``subtype=success`` with ``is_error=false``."""
    saw_success = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        subtype = event.get("subtype")
        if subtype == "success" and event.get("is_error") is False:
            saw_success = True
            continue
        if subtype is not None or event.get("is_error") is True:
            return False, f"stream final result failure subtype={subtype!r}"
    if not saw_success:
        return False, "stream missing successful final result event"
    return True, ""


def headless_stream_init_acceptable(summary: StreamJsonSummary) -> tuple[bool, str]:
    """
    Fail closed on login/session auth; keyed trials require ``apiKeySource=env``.
    """
    source = (summary.api_key_source or "").strip().lower()
    if not source:
        return False, "stream init missing apiKeySource"
    if source == "login":
        return False, "stream init apiKeySource=login (fail closed)"
    if source != "env":
        return False, f"stream init apiKeySource must be env, got {summary.api_key_source!r}"
    return True, ""


def headless_stream_permission_mode_note(summary: StreamJsonSummary) -> str:
    """Cursor may emit permissionMode=default while cli-config uses approvalMode allowlist."""
    mode = (summary.permission_mode or "").strip()
    if mode == "default":
        return (
            "Stream init permissionMode=default is informational only; "
            "cli-config approvalMode allowlist still governs native denials."
        )
    if mode and mode != "allowlist":
        return f"Stream init permissionMode={mode!r} (not treated as allowlist proof)."
    return ""


def _positive_stream_has_extra_completed_tools(summary: StreamJsonSummary) -> bool:
    certified = _bound_gate_a_mcp_stream_certifies(summary)
    if certified is None:
        return False
    bound_id = certified.call_id
    for call in summary.tool_calls:
        if call.status != "completed":
            continue
        if not _call_id_matched_2026_pair(summary, call.call_id):
            return True
        if call.call_id == bound_id:
            continue
        return True
    return False


def _call_id_matched_2026_pair(summary: StreamJsonSummary, call_id: str | None) -> bool:
    return isinstance(call_id, str) and bool(call_id) and call_id in summary.matched_2026_call_ids


def _gate_a_release_certification_ready(summary: StreamJsonSummary) -> bool:
    """Release predicates accept only unparsed-free 2026 stream-json (no legacy rows)."""
    if summary.unparsed_tool_call_variants:
        return False
    if summary.legacy_tool_call_events:
        return False
    return True


def _bound_gate_a_mcp_observations(summary: StreamJsonSummary) -> list[ToolCallObservation]:
    return [
        call
        for call in summary.tool_calls
        if call.mcp_server is not None
        and _bound_gate_a_mcp_identity(call.name, call.mcp_server, call.args)
    ]


def _bound_gate_a_mcp_stream_certifies(summary: StreamJsonSummary) -> ToolCallObservation | None:
    if not _gate_a_release_certification_ready(summary):
        return None
    bound = _bound_gate_a_mcp_observations(summary)
    if not bound:
        return None
    matched_bound = [call for call in bound if _call_id_matched_2026_pair(summary, call.call_id)]
    if len(matched_bound) != len(bound):
        return None
    if any(call.status == "running" for call in bound):
        return None
    if any(call.status == "error" for call in bound):
        return None
    successes = [call for call in bound if call.status == "completed"]
    if len(successes) != 1:
        return None
    return successes[0]


def _matched_2026_native_read_calls(summary: StreamJsonSummary) -> list[ToolCallObservation]:
    return [
        call
        for call in summary.tool_calls
        if call.name.casefold() == "read" and _call_id_matched_2026_pair(summary, call.call_id)
    ]


def positive_gate_a_tool_satisfied(
    summary: StreamJsonSummary,
    tool_name: str,
    *,
    mcp_payload_mode: GateAPositiveMcpPayloadMode = GateAPositiveMcpPayloadMode.LEGACY_V34,
    evidence_root_parent: Path | None = None,
    admitted_brief_hash: str | None = None,
) -> dict[str, Any] | None:
    """Return parsed MCP JSON payload when the bound tool completed in a matched 2026 pair."""
    from dev.factory.gate_a_mcp.constants import TOOL_NAME

    if tool_name != TOOL_NAME:
        return None
    if _matched_2026_native_read_calls(summary):
        return None
    if _positive_stream_has_extra_completed_tools(summary):
        return None
    call = _bound_gate_a_mcp_stream_certifies(summary)
    if call is None:
        return None
    return _extract_json_payload(
        call.result,
        mode=mcp_payload_mode,
        evidence_root_parent=evidence_root_parent,
        admitted_brief_hash=admitted_brief_hash,
    )


def _negative_native_tool_certified(summary: StreamJsonSummary, *names: str) -> bool:
    if not _gate_a_release_certification_ready(summary):
        return False
    wanted = {n.casefold() for n in names}
    matched: list[ToolCallObservation] = []
    for call in summary.tool_calls:
        if call.name.casefold() not in wanted:
            continue
        if not _call_id_matched_2026_pair(summary, call.call_id):
            return False
        matched.append(call)
    if not matched:
        return False
    return all(summary.permission_denied_for_tool(call.name) for call in matched)


def negative_attempt_satisfied(attempt_id: str, summary: StreamJsonSummary) -> bool:
    if attempt_id == "deny_shell_marker":
        return _negative_native_tool_certified(summary, "Shell")
    if attempt_id == "deny_write_marker":
        return _negative_native_tool_certified(summary, "Write")
    if attempt_id == "deny_alt_mcp_marker":
        if not _gate_a_release_certification_ready(summary):
            return False
        from dev.factory.gate_a_mcp.constants import MCP_SERVER_NAME

        for call in summary.tool_calls:
            if call.status != "error":
                continue
            if not _result_indicates_permission_denial(call.result):
                continue
            if call.name.casefold() == "mcp" and call.mcp_server:
                if call.mcp_server != MCP_SERVER_NAME:
                    return True
                continue
            if call.name.casefold() not in ("shell", "write"):
                return True
        return False
    if attempt_id == "deny_malformed_gate_a_tool":
        if not _gate_a_release_certification_ready(summary):
            return False
        from dev.factory.gate_a_mcp.constants import TOOL_NAME

        attempted = summary.attempted_tool_named(TOOL_NAME, "mcp") is not None
        if not attempted:
            return False
        if summary.permission_denied_for_tool(TOOL_NAME, "mcp"):
            return True
        return summary.mcp_tool_payload_rejected(TOOL_NAME)
    if attempt_id == "deny_web_fetch_marker":
        if not _gate_a_release_certification_ready(summary):
            return False
        return summary.attempted_tool_named(
            "WebFetch"
        ) is not None and summary.permission_denied_for_tool("WebFetch")
    if attempt_id == "deny_shell_without_mcp":
        return _negative_native_tool_certified(summary, "Shell")
    if attempt_id == "deny_write_without_mcp":
        return _negative_native_tool_certified(summary, "Write")
    return False
