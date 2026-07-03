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

## [2026-07-03] ingest | Post-launch audio-quality debugging session

Session started from the user asking why 4 Modal GPU containers were running at once and
ended up covering four distinct, evidence-backed fixes plus a resolved doc-staleness
finding:

- New issue page [part-by-part-audio-investigation](pages/issues/part-by-part-audio-investigation.md)
  (resolved): Modal `max_containers` fan-out, unreliable GPU-side pitch/gender
  auto-detection, FAISS index re-read-per-call (~1.4-2.0s/block, fixed via monkeypatch —
  no vendored-code edits), and an explicit product decision that call latency is not a
  priority, voice continuity is.
- Rewrote [adaptive-playout-buffer](pages/concepts/adaptive-playout-buffer.md) and
  extended [buffering-history](pages/concepts/buffering-history.md): the pages were
  describing a pre-2026-07-02-rebuild design (`_run_playout`, P95-adaptive sizing) that no
  longer existed even before today's session — corrected to the current bounded
  standing-buffer design (3rd distinct iteration of this buffer).
- **Doc-staleness finding, resolved same session**: [modal-render-region-mismatch](pages/issues/modal-render-region-mismatch.md)
  had been "confirmed open" across five docs (this wiki included) since 2026-07-02, but
  Render actually moved to Singapore at some point without any doc being updated — caught
  by querying the Render API directly for an unrelated reason. Corrected across
  `.agents/decisions/log.md`, `.agents/context/subsystem-notes.md`,
  `.agents/context/stack-and-rules.md`, `.agents/projects/active-backlog.md`, and this
  wiki (index, the issue page, and three source pages).
- Also corrected stale pipeline claims in [subsystem-notes](pages/sources/subsystem-notes.md)
  and [stack-and-rules](pages/sources/stack-and-rules.md) source pages (raw-fallback
  invariant, playout sequence numbers) that predated even the 2026-07-02 rebuild's own
  ingest — these were never accurate reflections of the post-rebuild code.

Net: 2 issue pages resolved, 1 new issue page, 2 concept pages corrected, 4 source pages
updated, index.md updated. `.agents/` decisions log, subsystem-notes, active-backlog, and
stack-and-rules all updated in parallel (same underlying facts, terse form) — see
`.agents/decisions/log.md`'s 2026-07-03 entry for the terse version of the same findings.

## [2026-07-02] lint | README vs LATENCY.md fail-safe budget

While cross-referencing README.md against LATENCY.md during the bootstrap ingest,
found a contradiction: README states a 5000ms RVC fail-safe budget, LATENCY.md and
`.agents/` say 2000ms. Checked `backend/main.py` and `backend/converters/rvc.py`
directly — the actual configured value is 2000ms (README's 5000ms is an unused class
default, not what's running). Filed as
[readme-latency-budget-contradiction](pages/issues/readme-latency-budget-contradiction.md).

## [2026-07-03] ingest | SIP audio isolation field-name bug (raw+converted voice mixing)

User reported live-call voice "not clear, breaking." Traced to `_restrict_sip_audio`
(`backend/main.py`, deployed commit `1324fe2`) silently failing on every call: it built
`UpdateSubscriptionsRequest(participant_identity=..., ...)` but the real protobuf field is
`identity` (confirmed via `livekit.protocol.room.UpdateSubscriptionsRequest
.DESCRIPTOR.fields_by_name`). Confirmed 100% failure rate across every call sampled in
Render production logs (`srv-d932m4cvikkc73belt1g`) -- no call ever showed the intended
success line. Net effect: the lead was hearing the raw agent mic and the RVC-converted
track mixed together for the whole call, the exact bug this helper was meant to prevent.
Fixed (commit `cf60ca5`, one-line field-name change) but not yet pushed/verified live --
see [active-backlog](pages/sources/active-backlog.md). Filed as
[sip-audio-mixing-isolation-bug](pages/issues/sip-audio-mixing-isolation-bug.md).

Also found and fixed a doc-drift bug while cross-referencing: `CLAUDE.md` still described
the old one-shot jitter buffer / "no playout queue" design, superseded by the 2026-07-03
standing playout buffer -- the sync commit for that change (`1b35d59`) never touched
`CLAUDE.md`. Updated `CLAUDE.md` in place to match `.agents/context/subsystem-notes.md`.
