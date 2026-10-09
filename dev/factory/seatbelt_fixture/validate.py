"""Pre-spawn validation for order-scoped manifests."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .manifest import ALLOWED_VERBS, OrderManifest

_SHELL_METACHAR_RE = re.compile(r"[;|&`$<>(){}[\]!#*?~\n\r]")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_DISALLOWED_EXECUTABLES = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "fish",
        "csh",
        "tcsh",
        "dash",
        "ksh",
        "/bin/sh",
        "/bin/bash",
        "/bin/zsh",
    }
)


class ManifestValidationError(ValueError):
    """Manifest or argv failed closed validation before spawn."""


def _basename(path: str) -> str:
    return Path(path).name


def validate_manifest_before_spawn(
    manifest: OrderManifest,
    *,
    bound_order_id: str,
    expected_cwd: Path,
    expected_child_script: Path,
    expected_python: Path,
    prior_order_id: str | None,
) -> None:
    """
    Reject shell injection, traversal, disallowed verbs, and cross-order reuse.

    :raises ManifestValidationError: When the manifest must not spawn.
    """
    if prior_order_id is not None and prior_order_id != manifest.order_id:
        raise ManifestValidationError(
            f"cross-order manifest reuse: bound={prior_order_id!r} manifest={manifest.order_id!r}"
        )
    if manifest.order_id != bound_order_id:
        raise ManifestValidationError(
            f"order_id mismatch: executor={bound_order_id!r} manifest={manifest.order_id!r}"
        )
    if manifest.verb not in ALLOWED_VERBS:
        raise ManifestValidationError(f"disallowed verb: {manifest.verb!r}")

    cwd = Path(manifest.cwd).resolve(strict=False)
    if cwd != expected_cwd.resolve(strict=False):
        raise ManifestValidationError(f"cwd must be checkout root: {expected_cwd} != {cwd}")

    if not manifest.argv:
        raise ManifestValidationError("argv must not be empty")

    exe = manifest.argv[0]
    if Path(exe).resolve(strict=False) != expected_python.resolve(strict=False):
        raise ManifestValidationError("argv[0] must be the fixture python interpreter")

    if len(manifest.argv) < 2:
        raise ManifestValidationError("argv must include child script path")
    script = Path(manifest.argv[1]).resolve(strict=False)
    if script != expected_child_script.resolve(strict=False):
        raise ManifestValidationError("argv[1] must be the fixture child script")

    if manifest.argv[2] != manifest.verb:
        raise ManifestValidationError("argv[2] must equal manifest verb")

    if _basename(exe) in _DISALLOWED_EXECUTABLES or exe in _DISALLOWED_EXECUTABLES:
        raise ManifestValidationError("shell interpreter invocation is not allowed")

    for index, arg in enumerate(manifest.argv):
        if not arg:
            raise ManifestValidationError(f"empty argv segment at index {index}")
        if _TRAVERSAL_RE.search(arg):
            raise ManifestValidationError(f"path traversal in argv[{index}]")
        if _SHELL_METACHAR_RE.search(arg):
            raise ManifestValidationError(f"shell metacharacter in argv[{index}]")
        if arg.strip() != arg:
            raise ManifestValidationError(f"leading/trailing whitespace in argv[{index}]")

    # Reject a single string that would parse as multiple tokens (e.g. sh -c).
    try:
        split = shlex.split(" ".join(manifest.argv))
    except ValueError as exc:
        raise ManifestValidationError(f"argv is not a safe token sequence: {exc}") from exc
    if tuple(split) != manifest.argv:
        raise ManifestValidationError("argv must be an exact token list, not a shell string")

    if manifest.verb == "positive" and len(manifest.argv) != 3:
        raise ManifestValidationError("positive verb requires exactly three argv entries")
    if manifest.verb == "probe_env_secret":
        if len(manifest.argv) != 3:
            raise ManifestValidationError("probe_env_secret requires exactly three argv entries")
    elif manifest.verb == "positive":
        pass
    elif manifest.verb == "probe_home_read":
        if len(manifest.argv) != 4:
            raise ManifestValidationError("probe_home_read requires home sentinel path argument")
    elif manifest.verb == "probe_home_symlink":
        if len(manifest.argv) != 5:
            raise ManifestValidationError(
                "probe_home_symlink requires home sentinel path and link name arguments"
            )
    elif len(manifest.argv) < 4:
        raise ManifestValidationError(f"{manifest.verb} requires probe arguments")
