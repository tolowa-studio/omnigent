# Dry-run: `start-project` for Tolowa Craft (`slug=craft`)

**Executed:** 2026-10-17 (updated) · **Mode:** dry-run only (no Notion/Linear/Stash mutations).  
**Path:** **repo-first / design-first** — site and repo designed in Claude before a MOTION hub exists.

## Inputs

| Field | Value |
| --- | --- |
| slug | `craft` |
| display_name | Tolowa Craft |
| segment | Venture (assumed; no All Clients row yet) |
| repo | `tolowa-studio/tolowa-craft-site` (Linear project description) |
| path | Repo-first / design-first |

## Source of truth (repo-first)

Decisions already made live in **repo + live/staging site**, not in the Factory Handoff bundle alone.

| Surface | Treat as canonical for hub extraction |
| --- | --- |
| Live / staging site | Offer lines, narrative, IA, regulatory copy locks |
| `docs/design/CRAFT-PASS-4-HANDOFF.md` | Pass-4 scope and design decisions |
| `docs/RUNBOOK.md`, `CLAUDE.md` | Ops, deploy, agent context |
| Factory Handoff Notion bundle | Research/history — **link**, do not overwrite |

**Would read first (when repo clone available):** site routes and content model, then handoff + runbook, then map backend from code/config.

## Hub content to build from repo/site (not invent in Notion)

### Positioning canon (manifest)

- Extract narrative, wedge, and forbidden language from **live copy** + handoff (e.g. no taxi/shuttle/handyman; by-appointment framing).
- Factory Handoff prose used only where it still matches the site; conflicts → site wins, note in State of the Site.

### GTM / offer lines (manifest or §1)

Map each **live** revenue line from the site into the hub (one row per line), including at minimum:

| Line | Hub mapping |
| --- | --- |
| Low voltage | §1 + manifest GTM table |
| Starlink | §1 + manifest GTM table |
| Networking / technology | §1 + manifest GTM table |
| Property management | §1 + manifest GTM table |

(Add other live pillars from site IA if present.)

### Backend scaffolding map (§4 + manifest canonical surfaces)

| Component | Hub documents |
| --- | --- |
| Sanity CMS | schema/studio URL, content ownership |
| CRM / Twenty | integration status, lead flow |
| Lead management | forms, routing, human gates on send |
| Email / Resend | domains, templates, **no send** without Jeramey |
| Deploy | host/CDN/env per runbook; link evidence URL when verified |

Stubbed or missing pieces → Linear issues with AC, not fictional “live” in Notion.

### Stay in sync

- **State of the Site:** append when site/repo narrative or scaffolding changes; bootstrap entry records snapshot date + URLs used.
- **Positioning canon:** revision log entry when manifest canon changes to match site.

## Stash (`/projects/craft`)

- No dense project namespace surfaced in recall; factory ops context lives under `/ops/tolowa-factory`.
- **Would create:** namespace with hub URL, repo, live site URL, scaffolding map pointer after hub promotion.

## Notion gaps (vs v4)

| Surface | Status | Repo-first action |
| --- | --- | --- |
| All Clients row | **Missing** | Create after hub root |
| Project Hub root | **Missing** | Create; content **derived from repo/site** |
| Hub Manifest | **Missing** | Positioning + GTM lines + scaffolding map from repo |
| §0–§7 section pages | **Missing** | §1/§4 heavy from extraction; link Factory Handoff under §5 |
| State of the Site | **Missing** | First append = repo/site bootstrap snapshot |
| Documents DB rows | **Missing** | Register manifest + SOTS after create |
| Loop stage on hub | **Missing** | Likely stage 2–3 (build/verify) from Linear pass-4 work |

## Linear

| Surface | Status |
| --- | --- |
| Project **Tolowa Craft** | **Exists** — <https://linear.app/tolowastudio/project/tolowa-craft-6c5e64e15d2f> |
| Epic **TOL-1378** Craft pass 4 build | **Exists** (Next) |
| Factory onboarding epic | **Missing** — AC: v4 hub from repo-first extraction, instruction files PR, scaffolding map complete |

## Repo

| File | Status |
| --- | --- |
| `AGENTS.md` | **Missing** (would add from template via PR) |
| `now.md` | **Missing** (would add; loop stage + link to hub after create) |
| `CLAUDE.md`, `docs/RUNBOOK.md`, `docs/design/CRAFT-PASS-4-HANDOFF.md` | **Canonical** — do not overwrite |
| Live site | **Source of truth** for GTM lines (cloud agent could not clone repo here — 404) |

## Would execute (if authorized)

1. Clone repo; scrape/staging URL; draft manifest positioning + GTM table + scaffolding map.
2. Draft PR on `tolowa-craft-site` adding `AGENTS.md` + `now.md` (hub URL back-filled after step 3).
3. Create All Clients row + `Tolowa Craft — Project Hub` + manifest + § pages + State of the Site (bootstrap append); link Factory Handoff without archiving.
4. Linear onboarding epic with repo-first AC; link in manifest.
5. `stash.remember` in `/projects/craft` with hub URL, site URL, repo.
