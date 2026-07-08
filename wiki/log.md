# Keira Wiki — Log

Append-only. Format: `## [YYYY-MM-DD] ingest|query|lint | Title`.
Parse recent entries with: `grep "^## \[" wiki/log.md | tail -5`

## [2026-07-08] ingest | Voice identity + clarity root-caused and fixed; SIP-leg packet loss fixed; call-analysis instrumentation

The long-open [voice-identity-mismatch-investigation](pages/issues/voice-identity-mismatch-investigation.md)
is **resolved**. Built a 3-point call-capture rig — Twilio dual-channel trunk recording
(`record-from-answer-dual`), per-call Modal in/out debug WAVs (`_DEBUG_SAVE_AUDIO` in
`worker.py`, replacing the TRT-dropped `_DEBUG_SAVE_RAW_AUDIO`), and now-legible Render logs
(`PYTHONUNBUFFERED=1`) — and measured the actual audio at each stage. Two independent causes,
both upstream of the model, both env-gated fixes (`f748a89`):

- **Pitch overshoot**: hardcoded `+12` for "male" doubled the user's ~137Hz voice to ~274Hz,
  ~5 semitones above the model's ~208Hz center (measured output F0 271Hz vs 208Hz good
  reference) → wrong identity. Now `RVC_MALE_PITCH_SHIFT` env (live 7).
- **Double noise-suppression**: browser `getUserMedia` defaults + server WebRTC NS Level 3
  stripped HF detail (input centroid 413Hz vs 720Hz clean, −9dB at 6-8kHz) → muffle. Now raw
  browser capture + `NS_LEVEL` env (live 1).

Notably the 2026-07-03 investigation had marked pitch "ruled out" (it confirmed `pitch=12` was
*applied*, mistaking that for *correct*) and input-quality "ruled out" (checked levels, not
spectrum) — both false negatives because every offline test used a female-range clip that
auto-detected to 0 shift and never exercised the +12 male path. Lesson recorded in
[[subsystem-notes]]: reproduce a live-only bug with the *actual captured live input at the
actual live pitch*, not a clean reference clip.

Separately filed and resolved [sip-leg-packet-loss-edge-pinning](pages/issues/sip-leg-packet-loss-edge-pinning.md):
the same capture rig showed 5 dropouts of loud speech in Twilio's own recording with flat
GPU→Twilio delay ⇒ RTP loss on the LiveKit→Twilio leg. Root cause: `{trunk}.pstn.twilio.com`
resolves to Twilio US, a transpacific hop from Singapore. Re-pointed the trunk to the
Singapore edge (+ `;edge=singapore` origination URL); post-fix call dropped 5→1.

Updated `.agents/decisions/log.md`, `.agents/context/subsystem-notes.md`,
`.agents/projects/active-backlog.md`, both issue pages, and `index.md`. Open: one live
verification call for the identity/clarity fix, the `/api/setup` edge-param drift trap, and
turning the debug tap off once verified.

## [2026-07-07] ingest | TRT Phase 2 shipped same-day + unvoiced-noise audio bug found and fixed

After the Phase 1 merge/audit earlier today, the branch moved fast: `38fbef5`/`b9df41f`/
`7164b85` shipped Phase 2 (block 1000ms→320ms, playout buffer 1.25s→0.25s, benchmark
54ms median/55ms p95 — down from Phase 1's 66/68ms). Flagged in
[tensorrt-migration](pages/issues/tensorrt-migration.md) and the backlog: the 2026-07-05
decision gated Phase 2 on "~a week" of live Phase-1 soak time, and there's no evidence that
happened before Phase 2 shipped — a process deviation worth a conscious call, not something
to silently wave through.

Same day, `d463c41` found and fixed a real regression from the original TRT shim: zeroing
`SineGen`'s unvoiced-frame noise (to dodge TRT Myelin's ONNX-RandomNormal restriction) made
unvoiced segments fully silent, causing audible hissing/garbled consonants — caught by ear,
not by the automated gates. Fixed by generating real `N(0,1)` noise outside the ONNX graph
and passing it in as a model input (same externalize-as-input pattern the generator's `rnd`
reparam already used), rather than zeroing it. Updated
`.agents/context/subsystem-notes.md`, `.agents/decisions/log.md`,
`.agents/projects/active-backlog.md`, and this page + `index.md` accordingly. Still open:
C4 (offline A/B WAVs), a fresh C5 listen test against this specific fix, and confirming the
live Modal deploy actually serves TRT.

## [2026-07-07] audit | Full stale-data sweep — 11 files corrected, wiki re-ingested

Systematic audit of the entire codebase for stale docs, config, and notes after the TRT
migration merge (`9c1093a`) and the earlier 2026-07-03 Render-region migration. Found and
corrected 11 files:

- **`render.yaml`** — region `oregon`→`singapore` (colocated with Modal since 2026-07-03,
  never reflected in infra-as-code); added 8 missing env vars (`RVC_API_KEY`, `RVC_PITCH_SHIFT`,
  `RVC_INDEX_RATE`, `RVC_WS_URL`, `RVC_KEEPWARM`, `CORS_ORIGINS`, `TWILIO_SIP_USERNAME`,
  `TWILIO_SIP_PASSWORD`).
- **`CLAUDE.md`** — dead `.env.example` link (file doesn't exist), stale project-layout tree
  (missed `rvc_stream.py`, all 6 new TRT-era `modal_deploy/` files), ~3s→1.25s buffer target.
- **`README.md`** — architecture section described the pre-rebuild VAD-chunked/T4/raw-fallback
  design; rewritten for the current fail-closed streaming/L4/buffer pipeline. Env reference
  expanded to match what the code actually reads.
- **`LATENCY.md`** — top-of-document banner added (doc predates the 2026-07-03 buffer
  reintroduction and the 2026-07-07 TRT merge); the removed `_JITTER_TARGET_BYTES` table row
  replaced with the current standing buffer; all T4→L4; region-mismatch section flipped from
  "still pending" to "resolved 2026-07-03"; inference row updated with actual C3 TRT benchmark
  numbers (median 66ms).
- **`.agents/context/subsystem-notes.md`** / **`stack-and-rules.md`** — buffer 3s→1.25s,
  GPU tier T4→L4, file map expanded with TRT-era files.
- **Wiki pages** — `adaptive-playout-buffer.md` (1.25s is now current, not "pending"),
  `audio-pipeline-latency-budget.md` (full rewrite: VAD/T4/raw-fallback table replaced with
  current streaming/TRT/L4/buffer rows), `rvc-cold-start.md` (re-framed as historical — the
  2000ms-budget/raw-fallback mechanism it describes no longer exists), `readme-latency-budget-
  contradiction.md` (marked resolved — the mechanism itself is gone, not just the number),
  `index.md` (TRT status, buffer iteration count, concept-page blurbs).

**Not touched**: `wiki/pages/sources/*` (those are point-in-time ingestion mirrors—see the
source-page updates below for the re-ingest that followed this audit) and the already-tracked
backlog items (stale `TWILIO_AUTH_TOKEN`, untracked `RVC/` cleanup, `_DEBUG_SAVE_RAW_AUDIO`)
which are legitimately open work, not stale docs.

## [2026-07-07] ingest | TensorRT migration merged to main

Updated [tensorrt-migration](pages/issues/tensorrt-migration.md) status: `trt-migration`
branch merged into `main` (merge commit `9c1093a`), previously-uncommitted vendored
ONNX-export shims now committed, C3 GPU benchmark passed (median 66ms/p95 68ms). Remaining
open: C4/C5 (offline A/B + listen test) and confirming the live deploy actually serves TRT.
Also corrected stale "uncommitted working tree" language in `.agents/projects/active-backlog.md`
and `.agents/context/subsystem-notes.md`, which still described the pre-merge state.

## [2026-07-03] ingest | Voice-identity investigation (ongoing) + SIP fix confirmed live

Confirmed [sip-audio-mixing-isolation-bug](pages/issues/sip-audio-mixing-isolation-bug.md)
live via Render logs (`[SIP Isolation] ✅` on every call sampled since deploy) — updated its
status from "committed, not deployed" to resolved, and updated `index.md` accordingly.

Filed a new open issue,
[voice-identity-mismatch-investigation](pages/issues/voice-identity-mismatch-investigation.md):
converted voice doesn't match the trained voice on live calls, even with the choppiness and
SIP-mixing bugs fixed. Five hypotheses ruled out with real evidence (pitch, `index_rate`,
chunked-streaming architecture, noise suppression, raw input audio quality) by building a new
reusable offline diagnostic (`convert_file_chunked`/`main_chunked` in
`modal_deploy/worker.py`) that replays the live `/ws` pipeline's exact processing against a
static file with no network/SIP involved. Also found and documented: a `gpu="L4"` change had
been committed but never actually deployed (git push and `modal deploy` are separate actions)
— the live worker was still on a T4 until an unrelated deploy activated it. Investigation is
still open pending one more retest.

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

## [2026-07-03] ingest | First real Modal deploy of the streaming rebuild + LiveKit SIP trunk staleness

The 2026-07-02 streaming rebuild had been merged to `main` but never actually deployed to Modal
until this session. Filed two new issue pages:

- [modal-deploy-path-bugs](pages/issues/modal-deploy-path-bugs.md) -- resolved. The first real
  `modal deploy` failed twice in a row: a stale `Retrieval-based-Voice-Conversion-WebUI` folder
  name (the folder had been renamed to `RVC/`), then a `ModuleNotFoundError` because Modal never
  auto-bundles a sibling local module (`streaming.py`) just because the entrypoint imports it --
  needed an explicit `add_local_python_source("streaming")`. Both fixed and confirmed live
  (`/health` returned `{"status":"ready",...}` with CUDA/T4 confirmed).
- [livekit-sip-trunk-stale](pages/issues/livekit-sip-trunk-stale.md) -- open. First live outbound
  dial after the GPU came up failed with a LiveKit 404 ("object cannot be found") on a trunk ID
  that had just been successfully listed by name -- root cause not fully diagnosed. Re-running
  `POST /api/setup` recreated the outbound trunk, inbound trunk, and dispatch rule from scratch
  (new IDs confirm the old ones were genuinely stale), but the same `/api/setup` call's Twilio-
  webhook step separately 401'd (stale/rotated `TWILIO_AUTH_TOKEN` on Render) -- still open. The
  actual retried outbound call has not yet been confirmed to succeed.

Also incidentally confirmed while chasing the folder-rename bug: ~195 files under `RVC/` are
already committed to git despite the folder being intended as gitignored-and-too-large-for-
GitHub -- `.gitignore` never matched the *current* folder name until this session's fix, so it
had silently stopped excluding anything. Fixing the gitignore entry doesn't retroactively
untrack the already-committed files; noted in `.agents/projects/active-backlog.md` as cleanup
still needed.

## [2026-07-05] ingest | TensorRT migration plan approved; phased playout-buffer reversal

Planning session, no code changed. Analyzed a user-drafted TensorRT proposal against the
codebase, corrected it in three places (static shape must account for the vendored
pipeline's t_pad and the accumulator's variable first-block context; engines must be built
on-GPU and cached to the volume, not at image build; a 0.25s playout buffer is below the
1000ms block-arrival granularity), and wrote `implementation_plan.md` (repo root).

- New issue page: [[tensorrt-migration]] -- migration scope, design findings, the phased
  buffer decision (1.25s Phase 1 approved 2026-07-05, 0.25s Phase 2 benchmark-gated), and
  resolution criteria. Notably, implementation is delegated to a *different* model; the
  resident agent reviews diffs against the plan's gates.
- Updated [[adaptive-playout-buffer]] with a pending-change banner (3s numbers remain the
  deployed truth until Phase 1 ships).
- `.agents/` counterparts: decisions log 2026-07-05 entry, active-backlog TensorRT row
  (replaces the old "deferred ONNX/TensorRT" row).

## [2026-07-06] ingest | TRT migration review rounds 2-3: from dead-on-arrival to credible

Two full code reviews of the other model's uncommitted TRT implementation. Round 2 found
the TRT path unable to run at all (broken HuBERT export, wrong production image, block
geometry mismatched against the static engine shapes); round 3 confirmed all of those
fixed and surfaced two real TensorRT platform discoveries (ONNX random ops don't compile
under TRT; Myelin FP16 bug on the generator).

- Updated [[tensorrt-migration]] with the full review arc and current status (9 open
  findings, Task 9 GPU verification + user-run rollout remaining).
- New load-bearing trap recorded in `.agents/context/subsystem-notes.md` and cross-linked
  in `.agents/projects/active-backlog.md`: the TRT-export shims live as uncommitted edits
  to vendored RVC files, and the pre-existing "untrack RVC/" backlog item would erase them
  -- that item is now explicitly blocked until the shims are committed or relocated.
