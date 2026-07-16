# Keira Wiki — Schema & Maintenance Guide

This is a **human-facing knowledge base** about the Keira project, separate from
[.agents/](../.agents/) (which is terse, low-context operating memory *for the coding
agent itself*). This wiki is narrative, cross-referenced, and meant to be browsed —
open the `wiki/` folder as an Obsidian vault, or just read it as plain markdown.

| | `.agents/` | `wiki/` |
|---|---|---|
| Audience | The coding agent, mid-task | You, browsing/researching |
| Style | Terse, routing-oriented | Narrative, synthesized |
| Trigger | Read automatically per task scope | Read/updated on request (`ingest`, `query`, `lint`) |
| Content | Invariants, file map, backlog | Synthesis, history, open questions, incident writeups |

They overlap in subject matter but not purpose — `.agents/decisions/log.md` records a
decision tersely for an agent about to touch code; `wiki/pages/issues/*.md` explains the
same decision's full context for a human trying to understand *why*.

## Layers

1. **Raw sources** — never duplicated here. For code and docs already in this repo
   (`backend/*.py`, `README.md`, `.agents/**`), the wiki links straight to
   the file path. Only sources that don't already live in the repo as a durable file
   (a pasted call transcript, an incident Slack thread, a one-off log dump) get saved
   under `wiki/raw/` before being ingested.
2. **The wiki** (`wiki/pages/`) — LLM-maintained markdown. Three kinds of page:
   - `pages/sources/` — one page per ingested source: what it is, when it was pulled in,
     the key claims extracted from it. This is the audit trail back to raw material.
   - `pages/concepts/` — durable, evergreen explanations of how something works
     (e.g. the playout buffer, the latency budget). Updated in place as understanding
     improves; not a changelog.
   - `pages/issues/` — open or resolved problems/incidents with their own arc (region
     mismatch, a production incident, a doc contradiction). Gets a `status:` in
     frontmatter and is updated as the issue evolves, not re-created.
3. **This file** — the schema. Update it when the conventions themselves change.

## Conventions

- **Frontmatter** on every page in `pages/`:
  ```yaml
  ---
  title: Short title
  type: source | concept | issue
  status: open | resolved | n/a   # issues only
  sources: [relative/path/to/raw/file.md, ...]
  updated: YYYY-MM-DD
  ---
  ```
- **Linking**: use Obsidian-style `[[adaptive-playout-buffer]]` links (no `.md`, no path — filenames are
  unique across `pages/`) for wiki-to-wiki links, matching the convention already used
  in `.agents/`. Use normal markdown links for paths outside the wiki (source files,
  `.agents/` pages).
- **Citations**: every claim in a concept/issue page should be traceable to a source
  page or a `file:line`-style reference. Don't assert something the underlying source
  doesn't actually say.

## Workflows

### Ingest
Given a source (a repo file, a pasted transcript, a new doc):
1. Read it.
2. Write or update its `pages/sources/<slug>.md` page (what it is, key claims, date).
3. Update or create the relevant `pages/concepts/` and `pages/issues/` pages —
   a single source can touch several.
4. Update `index.md`.
5. Append an entry to `log.md`.

### Query
1. Read `index.md` first to find candidate pages — don't re-derive from raw sources
   if a concept/issue page already synthesizes the answer.
2. Drill into the relevant `pages/` files (and raw sources if the wiki page is thin).
3. Answer with citations (page names / file paths).
4. If the answer is itself worth keeping (a comparison, an analysis, a root-cause
   writeup), file it back as a new or updated page rather than letting it evaporate
   into chat history — then update `index.md` and `log.md`.

### Lint
Periodically (or when asked):
- Check for contradictions between pages or between a page and its cited source
  (see [[readme-latency-budget-contradiction]] for a live example: `README.md` says a
  5000ms RVC budget, `LATENCY.md` / `backend/main.py` say 2000ms — one of these is stale).
- Flag pages with a `status: open` issue older than ~a few sessions with no updates.
- Flag orphan pages (nothing in `index.md` or another page links to them).
- Suggest concepts mentioned across multiple sources but lacking their own page.

## Log format

`log.md` entries start with `## [YYYY-MM-DD] ingest|query|lint | Title` so they're
greppable: `grep "^## \[" wiki/log.md | tail -5`.
