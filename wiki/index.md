# Keira Wiki — Index

See [WIKI.md](WIKI.md) for the schema and maintenance workflows before adding to this
wiki. This index is the first stop for any query — find the page here, then drill in.

## Concepts (evergreen explanations)
- [audio-pipeline-latency-budget](pages/concepts/audio-pipeline-latency-budget.md) — full
  mouth-to-ear latency breakdown, stage by stage, and why chunk size / conversion budget
  were tuned against each other.
- [adaptive-playout-buffer](pages/concepts/adaptive-playout-buffer.md) — how `_run_playout`
  smooths jitter with a per-session adaptive buffer, and the asyncio gotcha it works around.
- [buffering-history](pages/concepts/buffering-history.md) — the multi-attempt, one-revert
  migration path that led to the current buffer design; check here before re-fixing an
  old bug.
- [rvc-cold-start](pages/concepts/rvc-cold-start.md) — Modal T4 cold-start behavior
  (measured ~75s, not the assumed 8-30s), the intentional 2000ms fail-safe tradeoff, and
  a confirmed production incident where a lead heard raw voice for a whole call.

## Issues (open/resolved problems)
- [modal-render-region-mismatch](pages/issues/modal-render-region-mismatch.md) — **open,
  high priority.** Modal pinned to `ap-southeast`, Render actually in Oregon.
- [render-autodeploy-kills-live-calls](pages/issues/render-autodeploy-kills-live-calls.md)
  — **open, medium priority.** Every push to `main` redeploys and kills in-flight calls.
- [readme-latency-budget-contradiction](pages/issues/readme-latency-budget-contradiction.md)
  — **open, doc bug found by lint.** README says 5000ms, code/LATENCY.md say 2000ms.

## Sources (ingested raw material)
- [latency-md](pages/sources/latency-md.md) — `LATENCY.md`
- [decisions-log](pages/sources/decisions-log.md) — `.agents/decisions/log.md`
- [subsystem-notes](pages/sources/subsystem-notes.md) — `.agents/context/subsystem-notes.md`
- [stack-and-rules](pages/sources/stack-and-rules.md) — `.agents/context/stack-and-rules.md`
- [active-backlog](pages/sources/active-backlog.md) — `.agents/projects/active-backlog.md`
- [readme-md](pages/sources/readme-md.md) — `README.md`

## Not yet ingested (candidates for next pass)
- `backend/pipeline.py`, `backend/converters/rvc.py`, `modal_deploy/worker.py` — the
  actual source code backing the above concept pages (currently cited by path, not
  independently summarized as sources).
- `.agents/context/identity.md` — dev persona / code style, likely low-value for this
  wiki's purpose (it's about agent behavior conventions, not project knowledge) but
  worth a look.
- Anything not yet written down: incident postmortems, call-quality feedback, RVC model
  training notes for the current brand voice.
