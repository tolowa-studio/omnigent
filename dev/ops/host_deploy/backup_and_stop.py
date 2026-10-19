#!/usr/bin/env python3
"""Stop the live host and capture a rollback backup before an Omnigent upgrade."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request

from dev.ops.host_deploy.backup_policy import (
    assert_within_cap,
    filtered_copytree,
    max_backup_bytes_from_env_or_flag,
    planned_backup_bytes,
    prune_rollbacks,
    relative_path_is_excluded,
)

os.umask(0o077)

HOME = pathlib.Path.home()
CLI = HOME / ".local/bin/omni"
TOOL = HOME / ".local/share/uv/tools/omnigent"
DATA = HOME / ".omnigent"
LABELS = ("ai.omnigent.host", "com.tolowa.omnigent.host-register")
PLISTS = tuple(HOME / "Library/LaunchAgents" / (label + ".plist") for label in LABELS)

RUN: pathlib.Path


def run(args: list[str], name: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=HOME)
    path = RUN / "commands" / (name + ".json")
    path.write_text(
        json.dumps(
            {
                "argv": args,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            },
            sort_keys=True,
        )
        + "\n"
    )
    path.chmod(0o600)
    return proc


def digest_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: pathlib.Path | str) -> dict[str, int | str]:
    root_path = pathlib.Path(root)
    tree_hash = hashlib.sha256()
    count = 0
    bytes_total = 0
    for base, dirs, files in os.walk(root_path, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs + files:
            path = pathlib.Path(base) / name
            rel = path.relative_to(root_path).as_posix()
            count += 1
            if path.is_symlink():
                tree_hash.update(
                    b"L\0" + rel.encode() + b"\0" + os.readlink(path).encode() + b"\0"
                )
            elif path.is_file():
                size = path.stat().st_size
                bytes_total += size
                tree_hash.update(
                    b"F\0"
                    + rel.encode()
                    + b"\0"
                    + str(size).encode()
                    + b"\0"
                    + digest_file(path).encode()
                    + b"\0"
                )
            elif path.is_dir():
                tree_hash.update(b"D\0" + rel.encode() + b"\0")
    return {"entries": count, "regular_bytes": bytes_total, "tree_sha256": tree_hash.hexdigest()}


def inventory() -> dict[str, int | str]:
    counts: dict[str, int] = {}
    rows: list[tuple[str, str]] = []
    cursor: str | None = None
    pages = 0
    while True:
        sessions_url = (
            "http://127.0.0.1:6767/v1/sessions?"
            "limit=1000&include_archived=true&kind=any&visibility=all"
        )
        if cursor:
            sessions_url += "&" + urllib.parse.urlencode({"cursor": cursor})
        request = urllib.request.Request(sessions_url)
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        batch = payload.get("data", payload.get("sessions", []))
        pages += 1
        for row in batch:
            key = (str(row.get("id", "")), str(row.get("status", "unknown")))
            rows.append(key)
            counts[key[1]] = counts.get(key[1], 0) + 1
        cursor = payload.get("next_cursor") or payload.get("nextCursor")
        if not cursor:
            break
    serialized = json.dumps(sorted(rows), separators=(",", ":")).encode()
    return {
        "pages": pages,
        "total": len(rows),
        "unique": len({item[0] for item in rows}),
        "running": counts.get("running", 0),
        "waiting": counts.get("waiting", 0),
        "idle": counts.get("idle", 0),
        "failed": counts.get("failed", 0),
        "id_status_sha256": hashlib.sha256(serialized).hexdigest(),
    }


def status() -> dict:
    proc = run([str(CLI), "server", "status", "--json"], "server-status")
    if proc.returncode:
        raise RuntimeError("server status command failed")
    return json.loads(proc.stdout)


def resume_old(loaded: list[str]) -> list[dict]:
    outcome: list[dict] = []
    with open(os.devnull, "w") as sink:
        proc = subprocess.run(
            [str(CLI), "server", "--background"],
            stdout=sink,
            stderr=sink,
            timeout=720,
            cwd=HOME,
        )
        outcome.append({"action": "start-old-server", "exit_code": proc.returncode})
        for label in loaded:
            proc = subprocess.run(
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{os.getuid()}",
                    str(HOME / "Library/LaunchAgents" / (label + ".plist")),
                ],
                stdout=sink,
                stderr=sink,
                timeout=30,
            )
            outcome.append({"action": "bootstrap", "label": label, "exit_code": proc.returncode})
    deadline = time.monotonic() + 300
    attached = False
    while time.monotonic() < deadline:
        try:
            payload = json.loads(
                subprocess.run(
                    [str(CLI), "server", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout
            )
            if payload.get("running") and payload.get("daemon_attached"):
                attached = True
                break
        except Exception:  # noqa: BLE001 — poll until status JSON is readable
            pass
        time.sleep(2)
    outcome.append({"restored_running_attached": attached})
    return outcome


def shim_paths() -> list[pathlib.Path]:
    return [HOME / ".local/bin" / name for name in ("omni", "omnigent")]


def build_backup_plan(cap_bytes: int) -> dict:
    directory_roots: list[pathlib.Path] = []
    file_paths: list[pathlib.Path] = []
    if TOOL.is_dir():
        directory_roots.append(TOOL)
    for shim in shim_paths():
        if shim.exists() or shim.is_symlink():
            file_paths.append(shim)
    for plist in PLISTS:
        if plist.is_file():
            file_paths.append(plist)
    if DATA.is_dir():
        directory_roots.append(DATA)
    planned = planned_backup_bytes(directory_roots=directory_roots, file_paths=file_paths)
    excluded_samples: list[str] = []
    if DATA.is_dir():
        for base, dirs, files in os.walk(DATA, topdown=True, followlinks=False):
            base_path = pathlib.Path(base)
            for name in list(dirs) + list(files):
                rel = (base_path / name).relative_to(DATA)
                if relative_path_is_excluded(rel) and len(excluded_samples) < 20:
                    excluded_samples.append(rel.as_posix())
    return {
        "planned_bytes": planned,
        "cap_bytes": cap_bytes,
        "within_cap": planned <= cap_bytes,
        "directory_roots": [str(p) for p in directory_roots],
        "file_paths": [str(p) for p in file_paths],
        "excluded_sample_paths": excluded_samples,
    }


def main() -> None:
    global RUN
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-json", required=True)
    parser.add_argument(
        "--control-dir",
        default=os.environ.get("OMNIGENT_HOST_DEPLOY_CONTROL"),
        help="Host deploy state dir (default: $OMNIGENT_HOST_DEPLOY_CONTROL)",
    )
    parser.add_argument(
        "--max-backup-mib",
        type=int,
        default=None,
        help="Maximum rollback backup size in MiB (default: 200 or $OMNIGENT_HOST_BACKUP_MAX_MIB)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the backup and retention plan without stopping services or copying data",
    )
    args = parser.parse_args()
    if not args.control_dir:
        raise SystemExit("missing --control-dir or OMNIGENT_HOST_DEPLOY_CONTROL")

    control = pathlib.Path(args.control_dir)
    cap_bytes = max_backup_bytes_from_env_or_flag(args.max_backup_mib)
    pre = json.loads(pathlib.Path(args.preflight_json).read_text())
    baseline_commit = pre["installed_commit"]

    rollback_parent = control / "rollback"
    plan = build_backup_plan(cap_bytes)
    prune_targets = prune_rollbacks(rollback_parent, dry_run=True)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "rollback_dir": str(rollback_parent),
                    "prune_rollback_dirs": [str(p) for p in prune_targets],
                    "backup_plan": plan,
                },
                sort_keys=True,
                indent=2,
            )
        )
        return

    assert pre["running"] and pre["daemon_attached"]
    assert pre["session_inventory"]["running"] == 0 and pre["session_inventory"]["waiting"] == 0
    assert pre["critical_store_symlinks"] == 0

    assert_within_cap(plan["planned_bytes"], cap_bytes)
    prune_rollbacks(rollback_parent, dry_run=False)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    root = rollback_parent / stamp
    backup = root / "backup"
    run_dir = root / "execution"
    RUN = run_dir
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup.mkdir(mode=0o700)
    run_dir.mkdir(mode=0o700)
    (run_dir / "commands").mkdir(mode=0o700)

    manifest: dict = {
        "host": os.uname().nodename,
        "created_utc": stamp,
        "baseline_version": pre["version"],
        "baseline_commit": baseline_commit,
        "preflight_inventory": pre["session_inventory"],
        "preflight_plists": pre["launch_agents"],
        "loaded_agents": [
            item["label"] for item in pre["launch_agents"] if item["loaded_state"] == "running"
        ],
        "data_paths": [str(DATA), str(DATA / "chat.db"), str(DATA / "config.yaml")],
        "external_harness_symlinks_preserved_as_links": pre[
            "external_harness_symlinks_preserved_as_links"
        ],
        "backup_size_cap_bytes": cap_bytes,
        "backup_planned_bytes": plan["planned_bytes"],
        "completed": False,
    }
    loaded = list(manifest["loaded_agents"])
    try:
        if any(not path.is_file() for path in PLISTS):
            raise RuntimeError("required LaunchAgent plist missing")
        for index, path in enumerate(PLISTS):
            if digest_file(path) != pre["launch_agents"][index]["plist_sha256"]:
                raise RuntimeError("LaunchAgent plist changed after preflight")
        before = status()
        assert before.get("running") and before.get("daemon_attached")
        live = inventory()
        assert live == pre["session_inventory"], "session inventory changed since preflight"
        assert live["running"] == 0 and live["waiting"] == 0, "active session appeared"
        if (
            not TOOL.is_dir()
            or not (DATA / "chat.db").is_file()
            or not (DATA / "config.yaml").is_file()
        ):
            raise RuntimeError("expected package or data paths missing")

        tool_backup = backup / "uv-tool"
        filtered_copytree(TOOL, tool_backup)
        manifest["uv_tool_backup"] = tree_digest(tool_backup)

        shim_dir = backup / "shims"
        shim_dir.mkdir(mode=0o700)
        shim_info = []
        for name in ("omni", "omnigent"):
            src = HOME / ".local/bin" / name
            if src.exists() or src.is_symlink():
                dst = shim_dir / name
                if src.is_symlink():
                    dst.symlink_to(os.readlink(src))
                elif src.is_dir():
                    filtered_copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                shim_info.append(
                    {
                        "name": name,
                        "sha256": (
                            digest_file(src)
                            if src.is_file() and not src.is_symlink()
                            else hashlib.sha256(os.readlink(src).encode()).hexdigest()
                        ),
                        "symlink": src.is_symlink(),
                    }
                )
        manifest["shims"] = shim_info

        plist_dir = backup / "launch-agents"
        plist_dir.mkdir(mode=0o700)
        manifest["launch_agents_backup"] = []
        for src in PLISTS:
            dst = plist_dir / src.name
            shutil.copy2(src, dst)
            manifest["launch_agents_backup"].append({"name": src.name, "sha256": digest_file(dst)})

        immediate = inventory()
        assert immediate == live and immediate["running"] == 0 and immediate["waiting"] == 0
        quiesce = []
        for label in loaded:
            proc = subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            log = run_dir / "commands" / ("bootout-" + label.replace(".", "-") + ".json")
            log.write_text(
                json.dumps(
                    {
                        "action": "bootout",
                        "label": label,
                        "exit_code": proc.returncode,
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            log.chmod(0o600)
            quiesce.append({"label": label, "exit_code": proc.returncode})
            if proc.returncode:
                raise RuntimeError("LaunchAgent bootout failed")
        after_agents = inventory()
        assert (
            after_agents == live and after_agents["running"] == 0 and after_agents["waiting"] == 0
        )

        proc = run([str(CLI), "server", "stop"], "server-stop", timeout=180)
        manifest["server_stop_exit_code"] = proc.returncode
        if proc.returncode:
            raise RuntimeError("native server stop failed")
        stopped = status()
        assert stopped.get("running") is False
        try:
            urllib.request.urlopen("http://127.0.0.1:6767/health", timeout=2)
        except Exception:  # noqa: BLE001 — any connection error means health is down
            pass
        else:
            raise RuntimeError("health endpoint remained available after server stop")

        data_backup = backup / "live-state" / "omnigent"
        filtered_copytree(DATA, data_backup)
        source_db = DATA / "chat.db"
        safe_db = backup / "live-state" / "chat.db.wal-safe"
        source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=15)
        target = sqlite3.connect(safe_db, timeout=15)
        try:
            source.backup(target, pages=256, sleep=0.05)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
        finally:
            target.close()
            source.close()
        shutil.copystat(source_db, safe_db)
        os.replace(safe_db, data_backup / "chat.db")
        for suffix in ("-wal", "-shm"):
            side = data_backup / ("chat.db" + suffix)
            if side.exists():
                side.unlink()
        snapshot_db = data_backup / "chat.db"
        check = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if integrity != "ok":
            raise RuntimeError("final snapshot SQLite integrity check failed")

        manifest["data_backup"] = {
            **tree_digest(data_backup),
            "sqlite_integrity": integrity,
            "database_sha256": digest_file(data_backup / "chat.db"),
            "config_sha256": digest_file(data_backup / "config.yaml"),
            "excluded_globs": ["logs/**", "*.log", "*.jsonl", "artifacts/**", "cache/**"],
        }
        manifest["quiesce"] = {
            "launch_agents": quiesce,
            "server_running_after_stop": False,
            "health_closed": True,
        }
        manifest["completed"] = True
    except BaseException as exc:  # noqa: BLE001 — preserve rollback manifest on any failure
        manifest["error_type"] = type(exc).__name__
        manifest["error_message"] = str(exc)[:240]
        manifest["recovery_attempts"] = resume_old(loaded)
    finally:
        out = root / "manifest.json"
        out.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        out.chmod(0o600)

    print(
        json.dumps(
            {
                "backup_root": str(backup),
                "manifest": str(root / "manifest.json"),
                "completed": manifest["completed"],
                "error_type": manifest.get("error_type"),
                "baseline_commit": baseline_commit,
                "backup_package_tree_sha256": manifest.get("uv_tool_backup", {}).get(
                    "tree_sha256"
                ),
                "backup_data_tree_sha256": manifest.get("data_backup", {}).get("tree_sha256"),
                "sqlite_integrity": manifest.get("data_backup", {}).get("sqlite_integrity"),
                "server_stopped": manifest.get("quiesce", {}).get("server_running_after_stop")
                is False,
            },
            sort_keys=True,
        )
    )
    if not manifest["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
