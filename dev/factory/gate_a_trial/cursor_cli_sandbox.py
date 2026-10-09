"""Parent-owned Seatbelt profile denying plugin/skill/npm hydration in isolated HOME."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dev.factory.gate_a_trial.trial_env import default_isolated_home_parent


class GateACursorCliSandboxError(RuntimeError):
    """Fail-closed when macOS sandbox-exec cannot confine the Cursor CLI child."""


def seatbelt_canonical_subpath(path: Path) -> str:
    """Resolved absolute path for SBPL ``subpath`` (macOS ``/var`` → ``/private/var``)."""
    return path.expanduser().resolve().as_posix()


def gate_a_denied_home_subpaths(home_dir: Path) -> list[str]:
    """Exact canonical paths denied read/write for Gate A isolated HOME."""
    home = Path(home_dir).expanduser()
    return [
        seatbelt_canonical_subpath(home / ".cursor" / "plugins"),
        seatbelt_canonical_subpath(home / ".cursor" / "skills-cursor"),
        seatbelt_canonical_subpath(home / ".npm"),
        seatbelt_canonical_subpath(home / "Library" / "Caches" / "cursor-compile-cache"),
    ]


def build_gate_a_isolated_home_deny_profile(home_dir: Path) -> str:
    """SBPL: default-allow with explicit denies on hydration trees under isolated HOME."""
    lines = ["(version 1)", "(allow default)"]
    for subpath in gate_a_denied_home_subpaths(home_dir):
        lines.append(f'(deny file-read* file-write* (subpath "{subpath}"))')
    return "\n".join(lines) + "\n"


def profile_sha256(profile_text: str) -> str:
    return hashlib.sha256(profile_text.encode("utf-8")).hexdigest()


@dataclass
class GateACursorCliSandbox:
    """Short-lived ``sandbox-exec -f`` profile owned by the adapter parent."""

    profile_path: Path
    profile_sha256: str
    home_dir: Path

    def wrap_argv(self, argv: list[str]) -> list[str]:
        return ["sandbox-exec", "-f", str(self.profile_path), *argv]

    def cleanup(self) -> None:
        with contextlib.suppress(OSError):
            self.profile_path.unlink(missing_ok=True)


def prepare_gate_a_cursor_cli_sandbox(home_dir: Path) -> GateACursorCliSandbox:
    if shutil.which("sandbox-exec") is None:
        raise GateACursorCliSandboxError("sandbox-exec missing; Gate A Cursor CLI fails closed")
    profile_text = build_gate_a_isolated_home_deny_profile(home_dir)
    digest = profile_sha256(profile_text)
    parent = default_isolated_home_parent()
    parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="gate-a-cursor-cli-", suffix=".sb", dir=str(parent))
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(profile_text)
        os.chmod(path, 0o600)
    except OSError as exc:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        raise GateACursorCliSandboxError(f"failed to write Gate A sandbox profile: {exc}") from exc
    return GateACursorCliSandbox(profile_path=path, profile_sha256=digest, home_dir=Path(home_dir))


_DENIED_HOME_RELATIVE_ROOTS = (
    ".cursor/plugins",
    ".cursor/skills-cursor",
    ".npm",
    "Library/Caches/cursor-compile-cache",
)


def denied_home_trees_have_content(home_dir: Path) -> list[str]:
    """Fail closed when Seatbelt-denied trees contain any file (hydration bypass)."""
    home = Path(home_dir)
    problems: list[str] = []
    for rel_root in _DENIED_HOME_RELATIVE_ROOTS:
        root = home / rel_root
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                problems.append(
                    f"denied home tree gained content: {path.relative_to(home).as_posix()}",
                )
                break
    return problems
