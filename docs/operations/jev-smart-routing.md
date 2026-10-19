# Jev Smart Routing shadow — TOL-1304 SOP draft

Owner: Factory / Jeramey. The Notion librarian publishes this draft.

## Purpose and location

Omnigent's server runs a Jev observer next to its existing Smart Routing LLM
judge or Databricks router. The existing router remains authoritative. Jev first
chooses SIMPLE, MODERATE, COMPLEX or ESCAPE, then chooses a harness/model from the
same candidate menu or escapes. No dispatch, send, review or model is changed.
This is independent of BATON's existing routing and Control's existing Work > Jev
panel; this file does not add a second panel or publish receipts to Control.

## Enable and verify

Default is off. On an explicitly selected server, set
`OMNIGENT_JEV_ROUTING=shadow`, `OMNIGENT_JEV_RECEIPTS=/persistent/shared/jev-routing.jsonl`
and the existing `TYPESAFE_API_KEY`. Restart the server. All processes and replicas
of this fork MUST use the same persistent receipt file on a filesystem supporting
flock and fsync. Do not rotate, truncate, delete, or use separate files per worker:
the append-only file is also the daily budget ledger. Multi-host deployments without
such a shared filesystem must leave shadow off.

Submit a short Smart Routing task. The ordinary judge still supplies its result;
inspect the paired JSONL record for `effect:none`, `followed:true` and the source.
Off, missing key/path, oversized input and an exhausted cap make no TypeSafe calls.
Calls use the existing TypeSafe HTTP endpoint and env key convention, Python httpx,
fixed `jev-1.13.0`, no retry, 1.4 seconds per HTTP operation and 3 seconds total.
The observer runs in a background task and never delays routing. At most eight
observations run per process; excess observations are skipped. Shutdown may cancel
an observation after its reservation; reservations are never refunded.

## Receipts and budget

Each attempt durably appends a reservation before network access, then a paired
receipt. Both carry UTC day, pair ID, previous hash and canonical SHA-256 receipt
hash. Paired records carry state hash, judge/JeV pick hashes, complexity, source,
status, agreement, elapsed time, mode and effect. No task, rationale, key or raw
response is written. `followed` means the original judge result was returned from
the routing boundary, not that a downstream harness ultimately executed it.
A null judge yields `followed:false`; errors yield no shadow pick.

The hard cap is $0.04 per UTC day, reserved atomically across processes. Each
pair reserves 2 × 8192 input tokens at $0.042/M tokens ($0.000688128), allowing
58 attempts/day. Each of its two payloads is limited to 4096 UTF-8 JSON bytes;
the token allowance is deliberately conservative. Failed requests are never
refunded. Storage/chain failures deny new calls. Pricing source:
https://docs.typesafe.ai/models (checked 2026-10-16).

## Warn, block, promotion and off switch

Shadow never blocks the authoritative router. Receipt-write errors warn with a
static message; uncertainty, API failures, missing configuration, budget exhaustion
and corrupt storage block only Jev calls. Keep ledger permissions restricted.

Promotion requires a separate approved change: at least 50 paired observations,
review of disagreement/escape rates on representative real tasks, latency and
cost evidence, independently verified downstream outcomes, and explicit owner GO.
Synthetic probes prove plumbing and noninterference, not routing quality.
There is no active mode. Switch off with `OMNIGENT_JEV_ROUTING=off` and restart.
Do not delete the ledger to switch off or to reset the budget.

## Existence check

Checked fork PRs (Jev/TOL-1304 and routing), code search in both requested repos,
local routing branches, RuntimeCaps and Smart Routing. The agents repo's PR #29
already describes Jev inside BATON; its configs do not implement this server hook.
Control UI access was unavailable through Google IAP. Inspected its existing Jev
asset and the motion-control `/api/baton/v2/jev/summary` route instead. No duplicate
UI or new provider/key path was introduced. The issue's newer task instructions
explicitly select direct TypeSafe HTTP rather than its older daemon suggestion.
