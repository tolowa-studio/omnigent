# Gate A private MCP trial (disposable)

Local stdio MCP server with exactly one tool (`execute_internal_stage_order`) that
admits a fresh qualified Seatbelt fixture receipt and runs the closed
`OrderScopedWorkerAdapter` positive binding.

## Automated tests (no Cursor)

```bash
cd /path/to/omnigent-gate-a-private-mcp
pytest tests/dev/factory/test_gate_a_mcp_bridge.py -q
```

## Cursor CLI trial bootstrap (offline by default)

Materializes a disposable private `CURSOR_CONFIG_DIR`, empty global `mcp.json`,
project-local `.cursor/mcp.json` (Gate A stdio server only), `cli-config.json`
allowlist (`Mcp(server:tool)` only) plus explicit `Shell` / `Write` / `Read` /
`Grep` / `Glob` / `Delete` / `WebFetch` denials, `AGENT_CLI_CREDENTIAL_STORE=memory`, and SHA-256
`effective_config_hashes`
(`pre_enable` / `post_enable` after `--inspect-cli`) in the transcript. Default
bootstrap does **not** invoke the agent CLI.

```bash
python dev/factory/gate_a_trial/run_trial.py --isolated-home --inspect-cli
python dev/factory/gate_a_trial/run_trial.py --isolated-home --inspect-cli --probe-workspace-mcp-removal
pytest tests/dev/factory/test_gate_a_trial_config.py tests/dev/factory/test_gate_a_trial_mcp_discovery.py tests/dev/factory/test_gate_a_trial_stream_json.py tests/dev/factory/test_gate_a_trial_harness.py -q
```

Bootstrap creates a **disposable git-root workspace** under the isolated profile temp
tree (not `dev/factory/gate_a_trial/workspace/` inside the Omnigent repo). Cursor
resolves project MCP config from the git root; nesting under Omnigent made `mcp list`
report no servers.

Each bootstrap uses a **fresh unique ``HOME``** under the temp parent (never a fixed
``home/`` reuse). The transcript records the path, cursor-state-absent proof, and
post-enable config hashes. Disposable HOME dirs are marked with ``.gate-a-disposable-home``
and are left on disk until you archive the transcript evidence and remove them manually.

Headless stream-json (positive MCP, negatives, workspace MCP removal phase, 90s
settle) requires `CURSOR_API_KEY` in your shell and `--pass-cursor-api-key` (never
logged). The bootstrap JSON includes an `opt_in_headless_cli` one-liner template.

Use `--print-commands-only` to print manual commands without writing config.
Use `--inspect-cli` (no API key) to run the MCP discovery gate: `mcp list`, local
`mcp enable`, and `mcp list-tools` must show exactly one server and one tool before
any keyed headless attempt.

## Known limits

- The **90-second** Seatbelt fixture runs once on the **prestarted loopback HTTP MCP
  server** (harness waits for a process-bound witness before writing workspace
  ``mcp.json``). It does not run inside each ``CallTool`` request. A single-use
  qualified receipt is consumed on the first ``execute_internal_stage_order`` call;
  MCP retries without a fresh server fail closed (not a timeout success).
- Headless stream-json passes ``--trust`` for the disposable git-root workspace (no
  ``--force`` / ``--yolo``).
- Cursor Shell bypass via durable allowlist entries is documented in
  `dev/factory/order_scoped/cursor_integration.py`; model refusals are not evidence.
- Stream-json init may report `permissionMode=default` while `cli-config.json` uses
  `approvalMode: allowlist`; the harness records that mismatch and does not treat
  `default` as proof of allowlist enforcement.
- Global MCP servers may still appear in discovery; the trial profile **denies** them
  by name in `cli-config.json` while allowing only the factory tool.
