#!/usr/bin/env node
/**
 * Minimal Motion Core motion-order.mjs stand-in for offline submit tests.
 * Mirrors structured-submit flat JSON output and stable struct-* order ids.
 * Supports: new --task-stdin --orders-root <path> --json
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { join } from "node:path";

const ALLOWED_CLIENTS = new Set(["fixture-client"]);
const ALLOWED_REPOS = new Set(["fixture-org/fixture-repo"]);

function normalizeClientId(raw) {
  return String(raw ?? "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9._-]/g, "");
}

function canonicalBriefPayload(submission) {
  return {
    client_id: submission.client_id,
    repo: submission.repo,
    worktree: submission.worktree,
    branch: submission.branch,
    objective: submission.objective,
    scope: submission.scope,
    acceptance: submission.acceptance,
    authority_ref: submission.authority_ref,
    gates: submission.gates,
    base_sha: submission.base_sha,
    human_gates: submission.human_gates,
    review: submission.review,
    non_goals: submission.non_goals,
  };
}

function computeCanonicalBriefHash(submission) {
  const payload = canonicalBriefPayload(submission);
  const stable = JSON.stringify(payload, Object.keys(payload).sort());
  return createHash("sha256").update(stable).digest("hex");
}

function stableStructuredOrderId(clientId, idempotencyKey) {
  const digest = createHash("sha256").update(`${clientId}\0${idempotencyKey}`).digest("hex");
  return `struct-${digest.slice(0, 32)}`;
}

function loadIndex(ordersRoot) {
  const path = join(ordersRoot, ".fixture-order-index.json");
  if (!existsSync(path)) {
    return { path, data: {} };
  }
  const raw = readFileSync(path, "utf8");
  return { path, data: JSON.parse(raw) };
}

function saveIndex(path, data) {
  writeFileSync(path, JSON.stringify(data, null, 2) + "\n", "utf8");
}

function flatOkPayload({ orderId, briefHash, idempotent, ordersRoot }) {
  return {
    ok: true,
    order_id: orderId,
    work_id: orderId,
    order_path: join(ordersRoot, orderId),
    brief_hash: briefHash,
    idempotent,
    structured: true,
    state: "new",
    linear: null,
  };
}

function runNew(argv) {
  const ordersIdx = argv.indexOf("--orders-root");
  if (ordersIdx < 0 || !argv[ordersIdx + 1]) {
    process.stderr.write("missing --orders-root\n");
    process.exit(2);
  }
  const ordersRoot = argv[ordersIdx + 1];
  mkdirSync(ordersRoot, { recursive: true });
  const stdin = readFileSync(0, "utf8");
  let task;
  try {
    task = JSON.parse(stdin);
  } catch {
    process.stderr.write("invalid task json\n");
    process.exit(2);
  }
  const clientId = normalizeClientId(task.client);
  if (!ALLOWED_CLIENTS.has(clientId) || !ALLOWED_REPOS.has(task.repo)) {
    process.stderr.write("registry rejected client/repo\n");
    process.exit(2);
  }
  const key = task.idempotency_key;
  const submission = { ...task, client_id: clientId };
  const briefHash = computeCanonicalBriefHash(submission);
  const orderId = stableStructuredOrderId(clientId, key);
  const { path: indexPath, data: index } = loadIndex(ordersRoot);
  const existing = index[key];
  if (existing) {
    if (existing.brief_hash !== briefHash) {
      process.stderr.write("idempotency conflict\n");
      process.exit(4);
    }
    if (existing.order_id !== orderId) {
      process.stderr.write("idempotency index ambiguous\n");
      process.exit(5);
    }
    const payload = flatOkPayload({
      orderId: existing.order_id,
      briefHash: existing.brief_hash,
      idempotent: true,
      ordersRoot,
    });
    process.stdout.write(JSON.stringify(payload) + "\n");
    return;
  }
  const record = {
    order_id: orderId,
    brief_hash: briefHash,
    state: "new",
    leak_probe: "must not appear in chat",
    authority_ref: task.authority_ref,
    objective: task.objective,
  };
  index[key] = record;
  saveIndex(indexPath, index);
  const payload = flatOkPayload({
    orderId,
    briefHash,
    idempotent: false,
    ordersRoot,
  });
  process.stdout.write(JSON.stringify(payload) + "\n");
}

const cmd = process.argv[2];
if (cmd === "new") {
  runNew(process.argv.slice(2));
} else {
  process.stderr.write(`unsupported command: ${cmd ?? ""}\n`);
  process.exit(2);
}
