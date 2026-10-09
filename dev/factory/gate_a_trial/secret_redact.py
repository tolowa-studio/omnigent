"""Redact caller secrets from trial stdout/stderr and error strings."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


def _secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    values: list[str] = []
    sources = [os.environ]
    if env is not None:
        sources.append(env)
    for mapping in sources:
        for key in ("CURSOR_API_KEY", "CURSOR_SESSION_TOKEN"):
            val = mapping.get(key)
            if isinstance(val, str) and len(val) >= 8:
                values.append(val)
    # De-dupe longest first so partial overlaps still redact.
    return sorted(set(values), key=len, reverse=True)


_BEARER_CAPABILITY_RE = re.compile(
    r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)


def _capability_values(env: Mapping[str, str] | None = None) -> list[str]:
    values: list[str] = []
    sources = [os.environ]
    if env is not None:
        sources.append(env)
    for mapping in sources:
        val = mapping.get("GATE_A_MCP_HTTP_CAPABILITY")
        if isinstance(val, str) and len(val) >= 8:
            values.append(val)
    return sorted(set(values), key=len, reverse=True)


def redact_secrets(text: str, *, env: Mapping[str, str] | None = None) -> str:
    """Replace exact secret values with a stable token (never log the key)."""
    if not text:
        return text
    out = text
    for secret in _capability_values(env):
        out = out.replace(secret, "<redacted:gate-a-capability>")
    for secret in _secret_values(env):
        out = out.replace(secret, "<redacted:cursor-secret>")
    # Cursor occasionally echoes env-style assignments in errors.
    out = re.sub(
        r"(CURSOR_API_KEY\s*=\s*)(\S+)",
        r"\1<redacted:cursor-secret>",
        out,
        flags=re.IGNORECASE,
    )
    return _BEARER_CAPABILITY_RE.sub("Bearer <redacted:gate-a-capability>", out)


def redact_jsonable(value: object, *, env: Mapping[str, str] | None = None) -> object:
    """Deep redact strings in transcript-shaped JSON."""
    if isinstance(value, str):
        return redact_secrets(value, env=env)
    if isinstance(value, dict):
        return {k: redact_jsonable(v, env=env) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_jsonable(item, env=env) for item in value]
    return value


def redact_mapping_strings(
    payload: dict[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Shallow redact for subprocess result dicts stored in transcripts."""
    out: dict[str, object] = {}
    for key, value in payload.items():
        if key in ("stdout", "stderr", "error") and isinstance(value, str):
            out[key] = redact_secrets(value, env=env)
        else:
            out[key] = value
    return out
