# Dry-run: `start-project` for Tolowa Craft (`slug=craft`)

**Executed:** 2026-10-17 · **Mode:** dry-run only (no Notion/Linear/Stash mutations).

## Inputs

| Field | Value |
| --- | --- |
| slug | `craft` |
| display_name | Tolowa Craft |
| segment | Venture (assumed; no All Clients row yet) |
| repo | `tolowa-studio/tolowa-craft-site` (Linear project description) |

## Stash (`/projects/craft`)

- No dense project namespace surfaced in recall; factory ops context lives under `/ops/tolowa-factory` (generic factory/Tech Hub facts only).
- **Would create:** namespace entries for hub URL, manifest URL, repo, Linear project link after hub promotion.

## Notion gaps (vs v4)

| Surface | Status | Notes |
| --- | --- | --- |
| All Clients row | **Missing** | No dedicated Tolowa Craft Active row found in search |
| Project Hub root | **Missing** | Only <https://app.notion.com/p/3fa32e0a5474813ba5b8d51e1236ad37> *Tolowa Craft — Factory Handoff* bundle |
| Hub Manifest | **Missing** | |
| §0–§7 section pages | **Missing** | Handoff sections are research/briefs, not MOTION § schema |
| State of the Site | **Missing** | |
| Documents DB rows | **Missing** | |
| Loop stage on hub | **Missing** | Brief exists; not wired to six-stage contract |

## Linear

| Surface | Status |
| --- | --- |
| Project **Tolowa Craft** | **Exists** — <https://linear.app/tolowastudio/project/tolowa-craft-6c5e64e15d2f> |
| Epic **TOL-1378** Craft pass 4 build | **Exists** (Next) |
| Factory onboarding epic with hub AC | **Missing** — would add with acceptance criteria for v4 surfaces + instruction files PR |

## Repo

| File | Status |
| --- | --- |
| `AGENTS.md` | **Missing** (would add from template via PR) |
| `now.md` | **Missing** (would add from template via PR) |
| `CLAUDE.md`, `docs/RUNBOOK.md`, `docs/design/CRAFT-PASS-4-HANDOFF.md` | **Assume present** — do not overwrite (could not clone repo from this environment) |

## Would execute (if authorized)

1. Draft PR on `tolowa-craft-site` adding `AGENTS.md` + `now.md`.
2. Create All Clients row + `Tolowa Craft — Project Hub` + manifest + §0/§1/§4/§5/§6/§7 + State of the Site; migrate links from Factory Handoff without archiving handoff pages.
3. Open Linear onboarding epic linked to hub manifest.
4. `stash.remember` in `/projects/craft` with hub URL.
5. Append State of the Site entry listing created links and PR URL.
