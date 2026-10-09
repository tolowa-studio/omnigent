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
