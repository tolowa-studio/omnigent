# Agent guidance — {PROJECT_NAME}

Instruction file for AI agents working in **{REPO_NAME}**. Version-controlled;
changes require a **pull request** — same discipline as code.

## Authority

- Agents may: implement scoped work on feature branches, open draft PRs, update
  `now.md` on the same branch when focus shifts, append hub **State of the Site**
  entries via Notion MCP when authorized.
- Agents may **not** (human gates): **pay**, **send** (email/campaign), **sign**,
  **delete** production data or customer records, **ship** to production or change
  DNS without explicit operator approval.

## Source of truth

| Surface | Location |
| --- | --- |
| Behavior policy | This file (`AGENTS.md`) |
| Project context | `CLAUDE.md` |
| Current focus | `now.md` |
| Durable docs | `docs/` |
| Operating hub | {NOTION_HUB_URL} |
| Work queue | Linear team **Tolowa Studio** — project **{LINEAR_PROJECT}** |

Do not treat chat transcripts, local copies, or Stash alone as canonical over the
repo + Notion hub.

## Motion loop (one loop)

Stages: decide → build → verify → activate commercial → measure → weekly
continue/change/stop. Record loop stage and exit-gate evidence in `now.md` and the
hub State of the Site.

Business judgment, web delivery, and GTM demand are **capabilities inside the
loop**, not separate OS bibles.

## Engineering

- Branch model: `dev` → staging → `prod` (no direct commits to production).
- No Linear issue without **acceptance criteria** — ask for AC instead of guessing.
- Repo line in every Linear body: `Repo: {ORG}/{REPO}`.

## Pre-commit / CI

Run the repo's documented lint and test commands before pushing. Fix pre-commit
failures before merge.

## Related factory docs

- Omnigent `start-project` skill (Tolowa factory orchestrator).
- Notion: MOTION Hub Standard v4; Handoff 2 (TOL-639) for factory gates.
