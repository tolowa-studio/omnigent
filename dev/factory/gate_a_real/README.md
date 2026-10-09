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

Chat commands for Gate A tasks are unchanged (`run approved task <task_id>`,
`review approved task <task_id>`, `status <task_id>`). An additional read-only
Motion Core bridge command is available when motion pin env vars are set (see
below): `order status <order_id>`. Opt-in draft submit is available when
`OMNIGENT_FACTORY_MOTION_ORDER_SUBMIT=1` and task contracts are pinned (see
below): `order submit <task_id>`. Opt-in order start is available when
`OMNIGENT_FACTORY_MOTION_ORDER_START=1` and local approval files are pinned
(see below): `order start <order_id>`. Cancel, merge, and deployment are
**not** implemented in this harness.

Task IDs are validated strictly (no paths, separators, or whitespace). Symlinks,
traversal, missing specs, and mismatched `task_id` in the spec file are
rejected before any run.

Do **not** set `OMNIGENT_FACTORY_GATE_A_REAL_SPEC` / `OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS`
together with the registry vars; use either single-spec binding or registry binding.

#### Motion Core order status (phase-2 bridge, read-only)

Pin a local Motion Core checkout and orders directory at host startup (absolute
paths only; symlinks rejected). The harness runs
`node <core_root>/bin/motion-order.mjs status <order_id> --orders-root <orders_root> --json`
with a short timeout and returns a safe summary (order id, state, cancellation
phase, receipt category count, and validated report flags: `report_ok`,
`report_unavailable_count`, `report_mismatch_count`). `order_status_ok` means the
status query succeeded; `report_ok` reflects whether Core’s evidence report
completed without unavailable or mismatched sections. This is a snapshot from
Core, not live worker state. It does not write to Motion Core, Linear, or the
order directory.

```bash
export OMNIGENT_FACTORY_MOTION_CORE_ROOT=/absolute/path/to/motion-core
export OMNIGENT_FACTORY_MOTION_ORDERS_ROOT=/absolute/path/to/orders
```

Chat command (exact):

```text
order status <order_id>
```

#### Motion Core draft order submit (phase-2 bridge, opt-in)

Default **off**. When enabled, creates a **draft** order via Motion Core
`new --task-stdin` only (no worker start). Pin approved task JSON contracts at
host startup; chat supplies a strict `task_id` only (never JSON or paths).

```bash
export OMNIGENT_FACTORY_MOTION_ORDER_SUBMIT=1
export OMNIGENT_FACTORY_MOTION_TASK_CONTRACTS_DIR=/absolute/path/to/approved-task-contracts
export OMNIGENT_FACTORY_MOTION_CORE_ROOT=/absolute/path/to/motion-core
export OMNIGENT_FACTORY_MOTION_ORDERS_ROOT=/absolute/path/to/orders
```

Layout: `OMNIGENT_FACTORY_MOTION_TASK_CONTRACTS_DIR/<task_id>.json` — one bounded
JSON object per task matching Motion Core structured submit v2 stdin: required string
fields `client` (already-canonical client id), `repo`, `worktree` (absolute path),
`branch`, `objective`, `scope`, `acceptance`, `authority_ref`, and `idempotency_key`
(must equal `<task_id>`); required policy fields `gates` and `human_gates` (each a
non-empty array of up to 20 single-line strings, max 512 chars per entry), `base_sha`
(40- or 64-char lowercase hex commit), `review` (non-empty single-line string), and
`non_goals` (array, may be empty, same entry bounds); optional `brief_hash`. Unknown
top-level fields are rejected before Core runs (generic error, no field names echoed).
The harness returns a safe summary
(`order_id`, `work_id`, `brief_hash`, `state`, `submit_status` of `created` or
`idempotent_replay`) and does not echo task body, authority, or CLI stderr.

Chat command (exact):

```text
order submit <task_id>
```

#### Motion Core order start (phase-2 bridge, opt-in)

Default **off**. When enabled, launches a **new** structured order via Motion
Core `start --execute-direct-path` only after a local operator approval file
matches a fresh Core `status` precheck. This is a local operator beta — not
production activation and not proof of enterprise authorization.

```bash
export OMNIGENT_FACTORY_MOTION_ORDER_START=1
export OMNIGENT_FACTORY_MOTION_ORDER_APPROVALS_DIR=/absolute/path/to/order-start-approvals
export OMNIGENT_FACTORY_MOTION_CORE_ROOT=/absolute/path/to/motion-core
export OMNIGENT_FACTORY_MOTION_ORDERS_ROOT=/absolute/path/to/orders
```

Layout: `OMNIGENT_FACTORY_MOTION_ORDER_APPROVALS_DIR/<order_id>.json` — one
bounded JSON object per order (max 64 KiB; symlinks and traversal rejected).
Exact fields only: `schema_id` =
`omnigent.factory.motion-order-start-approval.v1`, `approved` = true, `order_id`
(must match chat token), `brief_hash` (64 lowercase hex), `base_sha` (40- or
64-char lowercase hex commit), `approved_by` and `approval_ref` (non-empty
single-line strings). Unknown fields are rejected before Core runs.

The harness runs Core `status <order_id> --orders-root <root> --json`, requires
`ok:true`, `order.state` = `new`, structured submit schema
`motion.order.structured-submit.v2`, matching `brief_hash` and `base_sha`, and no
active cancellation (`cancel_request`, cancel phases other than Core's idle
`active`/`none`, or markers). It compares those bindings to the approval
file, then runs `start <order_id> --execute-direct-path --orders-root <root>
--json` with `MOTION_ORDER_AUTO_MERGE=0`, `MOTION_ORDER_DUPLICATE_CHECK=enforce`,
`MOTION_ORDER_DETACH=launchd`, and `MOTION_ORDER_CLIENT_SECRET_SCOPING=1`.
The child uses the pinned `CLOUDSDK_CONFIG`; credential-file override variables
are not forwarded. On timeout or nonzero exit it does not claim
success — use `order status` to inspect outcome. On success it reports launch
submitted only when Core returns `state: started`, `direct_path.mocked: false`,
`direct_path.detached.ok: true`, and `direct_path.detached.mechanism: launchd`.

**Creating an approval file (human operator):** after draft submit, copy
`brief_hash` and `base_sha` from Core's JSON status into a new file
named exactly `<order_id>.json` under the pinned approvals directory. Set
`approved_by` / `approval_ref` to your local operator identity and ticket ref.
Do not commit approval files to the repo unless they are fixture data.

Chat command (exact):

```text
order start <order_id>
```

When using `omnigent.cli host`, include the motion pins in passthrough together
with the Gate A binding vars. Append `,CLOUDSDK_CONFIG` only if Cursor auth on
the host uses that config path (omit otherwise). For example:

```bash
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_FACTORY_GATE_A_REAL_TASK,OMNIGENT_FACTORY_GATE_A_REAL_CHAT,OMNIGENT_FACTORY_GATE_A_REAL_SPEC_DIR,OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS_ROOT,OMNIGENT_FACTORY_MOTION_ORDER_SUBMIT,OMNIGENT_FACTORY_MOTION_ORDER_START,OMNIGENT_FACTORY_MOTION_ORDER_APPROVALS_DIR,OMNIGENT_FACTORY_MOTION_TASK_CONTRACTS_DIR,OMNIGENT_FACTORY_MOTION_CORE_ROOT,OMNIGENT_FACTORY_MOTION_ORDERS_ROOT,CLOUDSDK_CONFIG
```

Single-spec passthrough example:

```bash
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_FACTORY_GATE_A_REAL_TASK,OMNIGENT_FACTORY_GATE_A_REAL_CHAT,OMNIGENT_FACTORY_GATE_A_REAL_SPEC,OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS,OMNIGENT_FACTORY_MOTION_CORE_ROOT,OMNIGENT_FACTORY_MOTION_ORDERS_ROOT,CLOUDSDK_CONFIG
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
- `order status <order_id>` — read-only Motion Core order snapshot when
  `OMNIGENT_FACTORY_MOTION_CORE_ROOT` and `OMNIGENT_FACTORY_MOTION_ORDERS_ROOT`
  are set (does not use Gate A spec/artifacts binding)
- `order submit <task_id>` — Motion Core draft `new` when
  `OMNIGENT_FACTORY_MOTION_ORDER_SUBMIT=1`, task contracts dir, and motion pins
  are set (does not use Gate A spec/artifacts binding)
- `order start <order_id>` — Motion Core `start --execute-direct-path` when
  `OMNIGENT_FACTORY_MOTION_ORDER_START=1`, approvals dir, and motion pins are
  set (does not use Gate A spec/artifacts binding)

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
config path (omit otherwise). Gate A task chat only — passthrough without Motion
Core pins:

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
export OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1
export OMNIGENT_FACTORY_GATE_A_REAL_SPEC=/absolute/path/to/task.spec.json
export OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS=/absolute/path/to/artifacts-dir
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_FACTORY_GATE_A_REAL_TASK,OMNIGENT_FACTORY_GATE_A_REAL_CHAT,OMNIGENT_FACTORY_GATE_A_REAL_SPEC,OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS

uv run python -m omnigent.cli host --server http://127.0.0.1:6877 --no-open
```

For `order status`, pin Motion Core on the host shell before passthrough (same
paths as above) and extend the passthrough list; append `,CLOUDSDK_CONFIG` only
when needed:

```bash
export OMNIGENT_FACTORY_GATE_A_REAL_TASK=1
export OMNIGENT_FACTORY_GATE_A_REAL_CHAT=1
export OMNIGENT_FACTORY_GATE_A_REAL_SPEC=/absolute/path/to/task.spec.json
export OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS=/absolute/path/to/artifacts-dir
export OMNIGENT_FACTORY_MOTION_CORE_ROOT=/absolute/path/to/motion-core
export OMNIGENT_FACTORY_MOTION_ORDERS_ROOT=/absolute/path/to/orders
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_FACTORY_GATE_A_REAL_TASK,OMNIGENT_FACTORY_GATE_A_REAL_CHAT,OMNIGENT_FACTORY_GATE_A_REAL_SPEC,OMNIGENT_FACTORY_GATE_A_REAL_ARTIFACTS,OMNIGENT_FACTORY_MOTION_CORE_ROOT,OMNIGENT_FACTORY_MOTION_ORDERS_ROOT

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
