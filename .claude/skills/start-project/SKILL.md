---
name: start-project
description: >-
  Tolowa factory playbook for the Omnigent orchestrator (tolowa-factory-orchestrator).
  Onboard or refresh an Active project — recall Stash /projects/{slug} and /ops/tolowa-factory,
  read the Notion hub + repo instruction files, gap-scan against MOTION Hub Standard v4,
  then create or link All Clients, hub/manifest, Linear project/epic with acceptance criteria,
  and Stash namespace with the hub URL. Supports hub-first and repo-first/design-first (live site +
  repo as truth; build Notion from them). Write links back to the hub and append State of the Site.
  Load when starting a new Tolowa engagement, promoting Backlog→Active, or running a hub compliance pass.
---

# start-project — Tolowa factory onboarding playbook

Use this skill when Jeramey or Desk asks to **start**, **stand up**, or **refresh** a Tolowa
project (venture or engagement). You are the **orchestrator**, not the sole implementer: delegate
repo work to coding sub-agents; use Notion/Linear/Stash MCP tools for library and index surfaces.

**Governing frame (signed):** [Motion OS — Thin Restructure Brief (TOL-638)](https://app.notion.com/p/3dd32e0a547481f0bbd9c26224cf015a) — one loop only; Business / Web / GTM are **capabilities inside the loop**, not peer OS bibles.

**Hub shape:** [MOTION Hub Standard v4 (DRAFT, Omnigent)](https://app.notion.com/p/3fc32e0a54748180a6bef9366032d2c2).

**Factory spec:** [Handoff 2 — Factory Strategy & Architecture Spec (TOL-639)](https://app.notion.com/p/3dd32e0a547481febcc8f99b5246e6ca).

**Human gates (never bypass):** pay, send, sign, delete, ship. Agents may draft and open PRs; humans merge and operate commercially.

---

## 0. Inputs you need before acting

Collect or infer:

| Input | Example | Required |
| --- | --- | --- |
| `slug` | `craft`, `konative` | yes |
| `display_name` | Tolowa Craft | yes |
| `segment` | Venture or Engagement (All Clients) | yes for hub placement |
| `repo` | `tolowa-studio/tolowa-craft-site` | yes if code exists |
| `hub_url` | existing Notion Project Hub | if already promoted |
| `linear_project` | Tolowa Craft | if already exists |

If any of segment, owner, or success metric are missing, **stop after gap report** — open a
Linear issue with acceptance criteria rather than inventing doctrine.

### Path: hub-first vs repo-first / design-first

| Path | When | Source of truth for *decisions already made* |
| --- | --- | --- |
| **Hub-first** | Greenfield or Notion-led promotion (e.g. Konative pattern) | Hub manifest + handoff docs; repo catches up |
| **Repo-first / design-first** | Working repo **and** live (or staging) site already exist — often designed in Claude/Cursor before a MOTION hub | **Repo + live site**; Notion is built **from** them, not the other way around |

**Repo-first / design-first (e.g. Tolowa Craft):**

1. Treat shipped copy, IA, design handoffs, and `docs/design/*-HANDOFF.md` as canonical for positioning and product decisions — do not relitigate in Notion without a written change/decision.
2. **Build the hub from the repo/site:**
   - **Positioning canon** (manifest): narrative, wedge, forbidden framings — extracted from live pages + handoff, not from stale Factory Handoff prose alone.
   - **GTM / offer lines:** one manifest subsection or §1 table row per revenue line visible on the site. For Craft, map at least: **low voltage**, **Starlink**, **networking / technology**, **property management** (plus any other live pillars).
   - **Backend scaffolding map** (§4 or manifest canonical surfaces): document what exists and what is planned — e.g. **Sanity CMS**, **CRM / Twenty**, **lead management**, **email / Resend**, **deploy** (host, CDN, env). Link to runbook and env; flag gaps as Linear issues with AC.
3. **Stay in sync:** when narrative or offers change in repo or on site, update **positioning canon** and **append State of the Site** (append-only). Hub follows product; Librarian routes new docs into §4/§5, not duplicate canon at hub root.
4. Factory Handoff bundles (like Craft’s) remain **history / research** — link from §5 or manifest, do not overwrite; hub is the Active operating surface.

If unsure which path applies: live site + meaningful `docs/design/*-HANDOFF.md` or pass-4 build in flight → default **repo-first**.

---

## 1. Recall Stash (always first)

1. `stash.recall` namespace **`/projects/{slug}`** — project facts, hub URLs, repo paths, locks.
2. `stash.recall` namespace **`/ops/tolowa-factory`** — factory sequencing, gate policy, model routing notes.
3. If namespace missing, plan to **`stash.remember`** after hub URL is canonical (step 6).

Do not treat Stash as authority over signed Notion briefs or versioned repo files; it is the **index**.

---

## 2. Read instruction surfaces (repo + Notion)

**Order depends on path:** repo-first → repo + live site **before** inferring hub content; hub-first → Notion hub/manifest first, then repo.

### Repo + live site (repo-first: read this block first)

1. Clone/checkout; read `CLAUDE.md`, `docs/RUNBOOK.md`, `docs/design/*-HANDOFF.md`.
2. Inventory **live or staging URL**: page structure, offer lines, CTAs, regulatory copy locks.
3. Inventory **backend scaffolding** from code/config/docs: CMS (e.g. Sanity), CRM (e.g. Twenty), forms/leads, email (e.g. Resend), deploy pipeline — note what is wired vs stubbed.
4. Then read `AGENTS.md` / `now.md` if present (add from templates if missing).

### Notion

1. Fetch the project's **Project Hub** (title pattern: `{name} — Project Hub`) or the handoff bundle if not yet promoted.
2. Fetch **Hub Manifest** if present.
3. Fetch **State of the Site** under §4 (Website & Product) if present.
4. Skim **MOTION OS Documentation** only for spine links — do not mass-edit Method in this playbook.

**Repo-first:** use Notion gaps to plan **hub content extracted from repo/site** (positioning canon, GTM lines, scaffolding map) — not to replace product truth.

### Repo (source of truth for agent behavior)

Read in order (after repo-first site pass if applicable):

1. `AGENTS.md` — authority, forbidden actions, human gates.
2. `CLAUDE.md` — project context (do not overwrite; extend via PR if needed).
3. `now.md` — current focus, loop stage, next decision date.
4. `docs/RUNBOOK.md`, `docs/design/*-HANDOFF.md`, or factory handoff docs referenced from the hub.

**Rule:** `AGENTS.md`, `CLAUDE.md`, and `now.md` live **in the repo they govern**. Instruction changes are behavior changes — **branch + PR + review**, same as code. Never treat a chat paste or local-only copy as canonical.

If files are missing, add them from templates in this skill directory (`templates/AGENTS.md`, `templates/now.md`) via a **draft PR** on the project repo.

---

## 3. Scan the repo

On the project checkout (clone if needed):

- Stack, deploy target, and branch model (`dev` → staging → prod per TOL-639).
- Existing CI (lint, build, smoke, eval harness).
- Whether `AGENTS.md` / `now.md` exist and match factory spec.
- Open PRs and default branch; note repo slug in Linear issue bodies (`Repo: org/name`).

Report **repo readiness** separately from **hub readiness**.

---

## 4. Linear (team: Tolowa Studio)

1. Search issues and projects for `{slug}` / display name.
2. **No work without a Linear issue** that includes **acceptance criteria** (TOL-639). Reject or rewrite issues that are titles only.
3. If no project exists, propose creating a **Linear project** + **epic** for factory onboarding with AC, for example:
   - Hub manifest + required §-pages exist per v4 checklist.
   - All Clients row populated.
   - Repo instruction files landed via PR.
   - Stash namespace seeded with hub URL.
   - Evidence URL field for verify stage when applicable.

Link the epic in the hub manifest and in `now.md` after creation.

---

## 5. Gap scan vs MOTION Hub Standard v4

Compare the project to the v4 checklist (manifest, §0–§7 as applicable, State of the Site, Documents DB rows, All Clients fields). Emit a table:

| Surface | Status | Action |
| --- | --- | --- |
| All Clients row | missing / partial / ok | create row or update fields |
| Project Hub root | … | create or link |
| Hub Manifest | … | create from Konative pattern |
| §0 Welcome | … | |
| §1 Business & viability | … | |
| §4 Website & product + State of the Site | … | |
| §5 Work & execution | … | |
| §6 Legal, brand & assets | … | |
| §7 Security & ownership | … | |
| Segment placement | … | Venture in mothership; Engagement staged in mothership |

**Portfolio Librarian** (`motion-control-librarian`) audits daily; your job is to **create or link** missing surfaces and register Documents DB rows — not to rewrite unrelated pages.

**Agent routing does not live in Notion** (v4). Conductor/subagent policy belongs in Omnigent agent config and skills (this repo), not hub §-pages.

For **dry-run** requests, produce the gap table only — do not create Notion/Linear/Stash artifacts.

---

## 6. Create or link missing surfaces

Execute only when the user explicitly authorized **start** (not dry-run).

### All Clients

Create or update row: `Stage=Active`, `Slug`, `Hub URL`, `Hub manifest page`, `Section schema`, aliases, `Librarian managed=true`, `Meeting-notes auto-move=false`.

### Notion hub tree

- One canonical `{display_name} — Project Hub`.
- Child **Hub Manifest** with positioning canon, section schema, naming, doc-type routing, canonical surfaces, forbidden surfaces, librarian behavior, revision log.
- **Repo-first:** seed manifest **positioning canon** and **GTM line table** from live site + design handoff; add **backend scaffolding map** (CMS, CRM, leads, email, deploy) with links to repo paths and runbook.
- Required §-pages per v4; **State of the Site** under §4 (append-only) — first entry documents hub bootstrap from repo/site snapshot date + URLs.
- Register canonical docs in **Documents DB** with `Project`, `Section`, `Status`, `Source`.
- **Ongoing sync:** product/narrative changes land in repo/site first; follow with manifest canon update + State of the Site append (never silent drift).

Do **not** archive or overwrite existing pages; mark superseded only when Jeramey directs.

### Linear

Create/link project and onboarding epic; every child issue has AC.

### Stash

`remember` in `/projects/{slug}`: hub URL, manifest URL, repo, Linear project URL, loop stage, owner, next decision date.

### Write-back

1. Update hub manifest **canonical surfaces** and All Clients fields with repo + Linear + Stash links.
2. **Append** a dated entry to **State of the Site** (newest first): what was created/linked, PR URLs, dry-run vs executed, residuals for human gates.

---

## 7. Loop alignment (TOL-638)

Record on hub + `now.md` which stage the project is in and the **exit gate** evidence:

1. Decision / contract — owner, constraint, metric, no-go.
2. Build / deliver — scope match; artifacts in hub/repo.
3. Release verify — acceptance evidence URL.
4. Activate commercial — one live offer path; demand owner.
5. Measure + service health — metric + next decision date.
6. Weekly decide — continue / change / stop (written).

No stage advance without the gate.

---

## 8. Templates

Copy from this directory into the **project repo** (adjust placeholders):

- `templates/AGENTS.md`
- `templates/now.md`

Respect existing `CLAUDE.md`, `docs/RUNBOOK.md`, and design handoffs — add files; do not replace project-specific locks.

---

## 9. Finish report

Return to the human:

1. Links: hub, manifest, State of the Site, All Clients row, Linear project/epic, repo PR(s).
2. Gap table (before/after).
3. What remains behind human gates.
4. Suggested next orchestrator skill (e.g. implementer for repo PR, verifier after CI).

---

## References

- Compliant hub example: Konative (see v3/v4 compliant table in Hub Standard).
- Linear team: **Tolowa Studio** (`TOL-*`).
- Stash layout: `/projects/{slug}`, `/clients/{slug}`, `/ops/tolowa-factory`.
