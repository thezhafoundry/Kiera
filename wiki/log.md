# Keira Wiki — Log

Append-only. Format: `## [YYYY-MM-DD] ingest|query|lint | Title`.
Parse recent entries with: `grep "^## \[" wiki/log.md | tail -5`

## [2026-07-02] ingest | Bootstrap: existing project docs

Set up the wiki structure (`WIKI.md`, `index.md`, `pages/{sources,concepts,issues}/`)
and did the first ingest pass over six existing docs, without duplicating any raw
content — sources are linked to their repo paths, not copied:

- `LATENCY.md`
- `.agents/decisions/log.md`
- `.agents/context/subsystem-notes.md`
- `.agents/context/stack-and-rules.md`
- `.agents/projects/active-backlog.md`
- `README.md`

Produced 6 source pages, 4 concept pages
(audio-pipeline-latency-budget, adaptive-playout-buffer, buffering-history,
rvc-cold-start), and 3 issue pages (modal-render-region-mismatch,
render-autodeploy-kills-live-calls, readme-latency-budget-contradiction).

## [2026-07-02] lint | README vs LATENCY.md fail-safe budget

While cross-referencing README.md against LATENCY.md during the bootstrap ingest,
found a contradiction: README states a 5000ms RVC fail-safe budget, LATENCY.md and
`.agents/` say 2000ms. Checked `backend/main.py` and `backend/converters/rvc.py`
directly — the actual configured value is 2000ms (README's 5000ms is an unused class
default, not what's running). Filed as
[readme-latency-budget-contradiction](pages/issues/readme-latency-budget-contradiction.md).
