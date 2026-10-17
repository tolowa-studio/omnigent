# Host upgrade rollback harness

Versioned scripts for stopping a live Omnigent host, capturing a bounded rollback
backup, upgrading, and restarting. Copy the trio into the per-upgrade control
directory under `~/.local/state/omnigent/<label>/` (or point
`OMNIGENT_HOST_DEPLOY_CONTROL` at that directory).

## `backup_and_stop.py`

Run after `preflight.py` has written a passing preflight JSON document.

```bash
export OMNIGENT_HOST_DEPLOY_CONTROL="$HOME/.local/state/omnigent/pr27-66be6d9a"
uv run python dev/ops/host_deploy/backup_and_stop.py \
  --preflight-json "$OMNIGENT_HOST_DEPLOY_CONTROL/preflight.json"
```

Dry-run (plan only — no stop, no copy):

```bash
uv run python dev/ops/host_deploy/backup_and_stop.py \
  --preflight-json "$OMNIGENT_HOST_DEPLOY_CONTROL/preflight.json" \
  --dry-run
```

Optional size cap (default 200 MiB, overridable via `OMNIGENT_HOST_BACKUP_MAX_MIB`):

```bash
uv run python dev/ops/host_deploy/backup_and_stop.py \
  --preflight-json "$OMNIGENT_HOST_DEPLOY_CONTROL/preflight.json" \
  --max-backup-mib 150
```

Rollback backups land in `$OMNIGENT_HOST_DEPLOY_CONTROL/rollback/<timestamp>/`.
The script keeps the three newest rollback directories and drops older ones under
that `rollback/` folder only. Logs, transcript JSONL, caches, and artifact trees
are excluded from the live-state copy so rollback stays small.

Typical host upgrade order:

1. `preflight.py` → preflight JSON
2. `backup_and_stop.py` → rollback backup + stop server/agents
3. Install the new Omnigent build (outside this harness)
4. `restart_and_postflight.py` → restore or roll forward
