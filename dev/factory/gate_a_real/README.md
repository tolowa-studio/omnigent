# Gate A real-operator task (local, opt-in)

Runs **one** JSON-bound task in a caller workspace through
`~/.local/bin/motion-cursor-agent` (override with
`GATE_A_REAL_CURSOR_EXECUTABLE` for tests). This path is separate from the
synthetic `OMNIGENT_FACTORY_GATE_A_CURSOR_CLI` fixture and does not enable
server front-door writes.

## Enable

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
```

### Omnigent chat beta (default off)

Local one-shot operator chat over harness `factory-gate-a-real`. This does **not**
enable production queue intake, deployment, or live factory promotion.

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
export OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1
export OMNIGENT_FACTORY_GATE_A_REAL_SPEC=/absolute/path/to/task.spec.json
export OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS=/absolute/path/to/artifacts-dir
```

#### Multi-task registry (local beta)

Run or review **multiple** operator-prepared specs from one long-lived host without
restarting for each task. Pin directories at startup (not per chat message):

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
export OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1
export OMNIGENT_FACTORY_GATE_A_REAL_SPEC_DIR=/absolute/path/to/approved-specs
export OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT=/absolute/path/to/artifact-roots
```

Layout:

- `OMNIGENT_FACTORY_GATE_A_REAL_SPEC_DIR/<task_id>.json` — hash-bound spec (one file
  per task; filename must match `task_id` inside the JSON)
- `OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT/<task_id>/` — isolated artifacts for
  that task (`receipt.json`, builder logs, etc.)

Chat commands are unchanged (`run approved task <task_id>`, `review approved task
<task_id>`, `status <task_id>`). Task IDs are validated strictly (no paths,
separators, or whitespace). Symlinks, traversal, missing specs, and mismatched
`task_id` in the spec file are rejected before any run.

Do **not** set `OMNIGENT_FACTORY_GATE_A_REAL_SPEC` / `OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS`
together with the registry vars; use either single-spec binding or registry binding.

When using `omnigent.cli host`, extend passthrough on the **host** shell:

```bash
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_FACTORY_GATE_A_REAL_TASK,OMNIGENT_FACTORY_GATE_A_REAL_CHAT,OMNIGENT_FACTORY_GATE_A_REAL_SPEC_DIR,OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT,CLOUDSDK_CONFIG
```

Single-spec mode passthrough (unchanged):

```bash
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_FACTORY_GATE_A_REAL_TASK,OMNIGENT_FACTORY_GATE_A_REAL_CHAT,OMNIGENT_FACTORY_GATE_A_REAL_SPEC,OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS,CLOUDSDK_CONFIG
```

Bind an agent spec with `executor.harness: factory-gate-a-real` (the harness id is
accepted by spec validation even when the subprocess registry is off). The harness
process is registered only when **both** env vars above are set at Omnigent
startup. In chat, use exact operator commands (model tool calls cannot change
paths or task IDs):

- `run approved task <task_id>` — runs the bound spec when `<task_id>` matches
  (single-spec: one file; registry: `<task_id>.json` under the spec dir)
- `review approved task <task_id>` — review-only resume (same bound spec and
  artifacts dir for that task; does not rerun the builder)
- `status <task_id>` — read-only `receipt.json` summary (never starts a run);
  verifies receipt `task_id`, `spec_sha256`, and `workspace` against the bound spec

`status` fails closed on malformed receipts or when `task_id`, `spec_sha256`, or
`workspace` in `receipt.json` do not match the bound spec. The chat summary shows
`problem_count` and the receipt path only (not raw problem text) and includes
`verify_exit_code`; it is a snapshot from `receipt.json`, not a live artifact check.

Repeated `run approved task` after a builder attempt is refused by the runner;
use `review approved task <task_id>` (chat), the CLI `--review-only` path, or a
fresh artifacts directory instead.

#### One-shot CLI (no persisted session)

With the four env vars exported in the same shell, a single turn does not need a
running server or host (in-process `--no-session`):

```bash
uv run python -m omnigent.cli run /absolute/path/to/agent.yaml --no-session \
  -p "status <task_id>"
```

#### Persistent session + web UI (local beta only)

For a durable `/c/<session_id>` chat and the bundled web UI, run a **dedicated**
server from **this checkout**. A machine-global daemon on the default port (`6767`)
may be an older build that does not register harness `factory-gate-a-real`.

Terminal 1 — isolated server (pick any free port; `6877` is an example):

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
export OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1
export OMNIGENT_FACTORY_GATE_A_REAL_SPEC=/absolute/path/to/task.spec.json
export OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS=/absolute/path/to/artifacts-dir

uv run python -m omnigent.cli server \
  --host 127.0.0.1 --port 6877 \
  --database-uri sqlite:////absolute/path/to/gate-a-real-beta.db \
  --artifact-location /absolute/path/to/gate-a-real-server-artifacts \
  --agent /absolute/path/to/agent.yaml
```

Terminal 2 — host bound to that server. Set
`OMNIGENT_RUNNER_ENV_PASSTHROUGH` **when starting the host**: the normal
daemon→runner env filter drops the four Gate A binding vars unless they are
named here. Include `CLOUDSDK_CONFIG` only if Cursor auth on the host uses that
config path (omit otherwise).

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
export OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1
export OMNIGENT_FACTORY_GATE_A_REAL_SPEC=/absolute/path/to/task.spec.json
export OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS=/absolute/path/to/artifacts-dir
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_FACTORY_GATE_A_REAL_TASK,OMNIGENT_FACTORY_GATE_A_REAL_CHAT,OMNIGENT_FACTORY_GATE_A_REAL_SPEC,OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS,CLOUDSDK_CONFIG

uv run python -m omnigent.cli host --server http://127.0.0.1:6877 --no-open
```

Terminal 3 — create the session (same four binding vars in the shell):

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
export OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1
export OMNIGENT_FACTORY_GATE_A_REAL_SPEC=/absolute/path/to/task.spec.json
export OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS=/absolute/path/to/artifacts-dir

uv run python -m omnigent.cli run /absolute/path/to/agent.yaml --server http://127.0.0.1:6877 \
  -p "status <task_id>"
```

On success the CLI prints a session id; open `http://127.0.0.1:6877/c/<session_id>`
for the chat UI. This path is for **local operator beta** only — not production
queue intake, deployment, or live factory promotion.

## Prepare a spec

1. Materialize profile hashes (offline):

```bash
python - <<'PY'
import json, tempfile
from pathlib import Path
from dev.factory.gate_a_real.profile import (
    materialize_real_task_cursor_config_dir,
    materialize_real_task_review_config_dir,
)
from dev.factory.gate_a_real.spec import canonical_spec_sha256

parent = Path(tempfile.mkdtemp())
profile = materialize_real_task_cursor_config_dir(parent)
review_profile = materialize_real_task_review_config_dir(parent / "review")
spec = {
    "task_id": "my-task",
    "workspace": "/absolute/path/to/worktree",
    "expires_at": "2026-12-31T23:59:59Z",
    "prompt": "…",
    "deliverable_paths": ["path/under/workspace"],
    "verify_command": ["pytest", "tests/foo.py", "-q"],
    "config_hashes": profile["effective_config_hashes"],
    "review_config_hashes": review_profile["effective_config_hashes"],
}
spec["spec_sha256"] = canonical_spec_sha256(spec)
print(json.dumps(spec, indent=2))
PY
```

2. Write the JSON to a file and run:

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
python -m dev.factory.gate_a_real \
  --spec /path/to/task.spec.json \
  --artifacts-dir /tmp/gate-a-real-my-task
```

Review-only (after a successful builder recorded in `resume_state.json`):

```bash
python -m dev.factory.gate_a_real \
  --spec /path/to/task.spec.json \
  --artifacts-dir /tmp/gate-a-real-my-task \
  --review-only
```

## Guarantees

- Builder: `composer-2.5`, sandbox enabled, no `--yolo` / `--force`, isolated `HOME`,
  empty global MCP, `Mcp(*:*)` denied in `cli-config.json`.
- Review: `grok-4.7-high`, `--mode ask`, `stream-json`; terminal result must include a
  standalone `REVIEW: PASS` line (no quoted/substring passes).
- Fail closed on spec/workspace/config drift, env API keys, MCP tool activity,
  verify failure, missing deliverables, or post-review mutation.

Artifacts: `builder.*`, `review-attempts/`, `verify.log`, `verify.meta.json`,
`freeze/`, `receipt.json`. Each review attempt retains its own stream, stderr,
and metadata; the receipt points to the latest review log.
