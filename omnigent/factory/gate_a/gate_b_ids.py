"""Gate B identifier validation shared by chat grammar and Motion Core bridges."""

from __future__ import annotations

import re

_GATE_B_COMMENT_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_GATE_B_COMMENT_UUID_ERROR = "gate-b-comment-id must be a UUID"


def _is_single_line_gate_b_token(value: str) -> bool:
    return "\n" not in value and "\r" not in value and " " not in value


def require_gate_b_comment_id(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError(_GATE_B_COMMENT_UUID_ERROR)
    if not _is_single_line_gate_b_token(raw):
        raise ValueError(_GATE_B_COMMENT_UUID_ERROR)
    if not _GATE_B_COMMENT_UUID_RE.fullmatch(raw):
        raise ValueError(_GATE_B_COMMENT_UUID_ERROR)
    return raw
