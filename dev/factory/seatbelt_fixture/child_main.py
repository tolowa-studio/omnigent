"""Sandboxed child entrypoint (fixture only)."""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
from pathlib import Path

# Duplicated for the checkout copy (standalone script); parent uses manifest constants.
_SEATBELT_FILE_DENIAL_ERRNOS: frozenset[int] = frozenset({errno.EPERM})
_SEATBELT_CONNECT_DENIAL_ERRNOS: frozenset[int] = frozenset({errno.EPERM})

ALLOWED_VERBS: frozenset[str] = frozenset(
    {
        "positive",
        "probe_outside_write",
        "probe_outside_read",
        "probe_local_connect",
        "probe_env_secret",
        "probe_home_read",
        "probe_home_symlink",
    }
)
CANARY_FILE_TOKEN = "SYNTHETIC_FIXTURE_CANARY_CONTENT"
HOME_SENTINEL_TOKEN = "OMNIGENT_SEATBELT_PROBE_HOME_SYNTHETIC_TOKEN"
ENV_PROBE_NAME = "FIXTURE_PROBE_ENV_SECRET"


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _seatbelt_file_denial(exc: OSError) -> bool:
    return exc.errno in _SEATBELT_FILE_DENIAL_ERRNOS


def _seatbelt_connect_denial(errno_value: int | None) -> bool:
    return errno_value is not None and errno_value in _SEATBELT_CONNECT_DENIAL_ERRNOS


def _run_positive() -> int:
    checkout = Path.cwd()
    target = checkout / "allowed.txt"
    target.write_text("allowed\n", encoding="utf-8")
    _emit({"verb": "positive", "ok": True, "created": "allowed.txt"})
    return 0


def _run_probe_outside_write(outside_path: str) -> int:
    write_denied = False
    executed = True
    write_denial_errno: int | None = None
    detail = "write not attempted"
    try:
        Path(outside_path).write_text("outside-write-attempt\n", encoding="utf-8")
        detail = "outside write unexpectedly succeeded"
    except OSError as exc:
        write_denial_errno = exc.errno
        if _seatbelt_file_denial(exc):
            write_denied = True
            detail = "write denied"
        else:
            detail = f"write failed errno={exc.errno}"
    ok = executed and write_denied
    _emit(
        {
            "verb": "probe_outside_write",
            "ok": ok,
            "detail": detail,
            "executed": executed,
            "write_denied": write_denied,
            "write_denial_errno": write_denial_errno,
            "target_path": outside_path,
        }
    )
    return 0 if ok else 2


def _run_probe_outside_read(outside_path: str) -> int:
    leaked = False
    read_denied = False
    executed = True
    read_denial_errno: int | None = None
    detail = "read not attempted"
    try:
        content = Path(outside_path).read_text(encoding="utf-8")
        read_denied = False
        if CANARY_FILE_TOKEN in content:
            leaked = True
            detail = "canary token observed in read buffer"
        else:
            detail = "outside read unexpectedly succeeded"
    except OSError as exc:
        read_denial_errno = exc.errno
        if _seatbelt_file_denial(exc):
            read_denied = True
            detail = "read denied"
        else:
            detail = f"read failed errno={exc.errno}"
    ok = executed and read_denied and not leaked
    _emit(
        {
            "verb": "probe_outside_read",
            "ok": ok,
            "detail": detail,
            "leaked": leaked,
            "executed": executed,
            "read_denied": read_denied,
            "read_denial_errno": read_denial_errno,
            "target_path": outside_path,
        }
    )
    return 0 if ok else 2


def _run_probe_local_connect(port: str) -> int:
    connected = False
    executed = True
    connect_denial_errno: int | None = None
    detail = "connect not attempted"
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=2.0):
            connected = True
            detail = "connect unexpectedly succeeded"
    except OSError as exc:
        connect_denial_errno = exc.errno
        if _seatbelt_connect_denial(exc.errno):
            detail = "connect denied"
        else:
            detail = f"connect failed errno={exc.errno}"
    ok = executed and connected is False and _seatbelt_connect_denial(connect_denial_errno)
    _emit(
        {
            "verb": "probe_local_connect",
            "ok": ok,
            "detail": detail,
            "connected": connected,
            "executed": executed,
            "connect_denial_errno": connect_denial_errno,
        }
    )
    return 0 if ok else 2


def _run_probe_home_read(home_sentinel_path: str) -> int:
    executed = True
    read_denied = False
    read_denial_errno: int | None = None
    leaked = False
    detail = "read not attempted"
    try:
        Path(home_sentinel_path).read_text(encoding="utf-8")
    except OSError as exc:
        read_denial_errno = exc.errno
        if _seatbelt_file_denial(exc):
            read_denied = True
            detail = "read denied"
        else:
            detail = f"read failed errno={exc.errno}"
    except Exception:
        executed = False
        detail = "probe aborted with non-seatbelt error"
    else:
        leaked = True
        detail = "home sentinel read unexpectedly succeeded"
    ok = executed and read_denied and not leaked
    _emit(
        {
            "verb": "probe_home_read",
            "ok": ok,
            "detail": detail,
            "leaked": leaked,
            "executed": executed,
            "read_denied": read_denied,
            "read_denial_errno": read_denial_errno,
            "target_path": home_sentinel_path,
        }
    )
    return 0 if ok else 2


def _run_probe_home_symlink(home_sentinel_path: str, link_name: str) -> int:
    executed = False
    read_denied = False
    write_denied = False
    read_denial_errno: int | None = None
    write_denial_errno: int | None = None
    leaked = False
    detail = "symlink probe not started"
    link = Path.cwd() / link_name
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(home_sentinel_path)
        executed = True
    except OSError as exc:
        detail = f"symlink creation failed errno={exc.errno}"
        _emit(
            {
                "verb": "probe_home_symlink",
                "ok": False,
                "detail": detail,
                "leaked": leaked,
                "executed": executed,
                "read_denied": read_denied,
                "write_denied": write_denied,
                "read_denial_errno": read_denial_errno,
                "write_denial_errno": write_denial_errno,
                "target_path": home_sentinel_path,
                "link_name": link_name,
            }
        )
        return 2

    try:
        content = link.read_text(encoding="utf-8")
        read_denied = False
        if HOME_SENTINEL_TOKEN in content:
            leaked = True
            detail = "home sentinel token observed via symlink"
        else:
            detail = "symlink read unexpectedly succeeded"
    except OSError as exc:
        read_denial_errno = exc.errno
        if _seatbelt_file_denial(exc):
            read_denied = True
            detail = "symlink read denied"
        else:
            detail = f"symlink read failed errno={exc.errno}"

    try:
        link.write_text("home-symlink-write-attempt\n", encoding="utf-8")
        write_denied = False
        detail = "symlink write unexpectedly succeeded"
    except OSError as exc:
        write_denial_errno = exc.errno
        if _seatbelt_file_denial(exc):
            write_denied = True
            if read_denied and not leaked:
                detail = "symlink read and write denied"
        else:
            detail = f"symlink write failed errno={exc.errno}"

    ok = executed and read_denied and write_denied and not leaked
    _emit(
        {
            "verb": "probe_home_symlink",
            "ok": ok,
            "detail": detail,
            "leaked": leaked,
            "executed": executed,
            "read_denied": read_denied,
            "write_denied": write_denied,
            "read_denial_errno": read_denial_errno,
            "write_denial_errno": write_denial_errno,
            "target_path": home_sentinel_path,
            "link_name": link_name,
        }
    )
    return 0 if ok else 2


def _run_probe_env_secret() -> int:
    executed = True
    value = os.environ.get(ENV_PROBE_NAME)
    leaked = value is not None and value != ""
    ok = executed and not leaked
    detail = "env secret denied to child" if ok else "env secret visible to child"
    _emit(
        {
            "verb": "probe_env_secret",
            "ok": ok,
            "detail": detail,
            "leaked": leaked,
            "executed": executed,
            # Never echo the value — only whether a non-empty value was seen.
        }
    )
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        _emit({"ok": False, "detail": "missing verb"})
        return 2
    verb = args[0]
    if verb not in ALLOWED_VERBS:
        _emit({"ok": False, "detail": f"disallowed verb {verb!r}"})
        return 2

    if verb == "positive":
        return _run_positive()
    if verb == "probe_outside_write":
        if len(args) != 2:
            _emit({"ok": False, "detail": "outside path required"})
            return 2
        return _run_probe_outside_write(args[1])
    if verb == "probe_outside_read":
        if len(args) != 2:
            _emit({"ok": False, "detail": "outside path required"})
            return 2
        return _run_probe_outside_read(args[1])
    if verb == "probe_local_connect":
        if len(args) != 2:
            _emit({"ok": False, "detail": "listener port required"})
            return 2
        return _run_probe_local_connect(args[1])
    if verb == "probe_env_secret":
        return _run_probe_env_secret()
    if verb == "probe_home_read":
        if len(args) != 2:
            _emit({"ok": False, "detail": "home sentinel path required", "executed": False})
            return 2
        return _run_probe_home_read(args[1])
    if verb == "probe_home_symlink":
        if len(args) != 3:
            _emit({"ok": False, "detail": "home sentinel path and link name required", "executed": False})
            return 2
        return _run_probe_home_symlink(args[1], args[2])
    _emit({"ok": False, "detail": "unreachable"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
