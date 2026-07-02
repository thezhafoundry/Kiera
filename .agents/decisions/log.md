# Historical Decisions & Migrations

## Migration Index
<!-- date | migration file | what changed -->
No formal DB/schema migrations in this project (no persistent datastore) — this section
tracks pipeline/architecture migrations instead:

| Date (approx, from git log) | Commit | What changed |
|---|---|---|
| — | `8aac8be` | Replaced ordered buffer with direct publish to eliminate a 26s latency backlog (later found insufficient on its own — see below). |
| — | `523e6d9` | Introduced a 1-second one-shot pre-buffer for smooth lead playback before streaming begins. |
| — | `0a76fe1` | Discard fail-safe (raw fallback) chunks during the pre-buffer window so the lead only ever hears the converted voice once playback starts. |
| — | `2a20b3a` | Removed an `age_before` check that was silently dropping/silencing lead audio; added a 4s pre-buffer timeout fallback. |
| 2026-07-02 | `6661db7` | Rewrote LATENCY.md with live-measured numbers and added the 4s timeout regression note. |
| 2026-07-02 | `fe678d6` | Replaced the one-shot pre-buffer with the current adaptive, per-session standing playout buffer described in LATENCY.md §5 / [[subsystem-notes]]. |
| 2026-07-02 | `f3c16ed` (Tasks 1-5) | Streaming rebuild: replaced the VAD-chunked HTTP-per-request pipeline (rows above) with a persistent-WebSocket duplex streaming design, a never-raw/fail-closed audio policy, and a fail-closed pre-dial/pre-bridge warm gate — see the dedicated entry below. |

Note the `eb016f3`/`da46c48` "Reverted: Buffer Changed for voice issue" commits bracketing
this sequence — the buffer/pre-buffer design went through at least one full revert cycle
before landing on the current adaptive approach. If a buffering bug resurfaces, check
whether it's a regression of something already tried and reverted here before re-deriving
a fix from scratch.

## Decisions
- **One-way voice conversion only (agent→lead).** Why: the agent needs to hear the lead's
  real voice for natural conversation; only the outbound "brand voice" needs to be
  consistent. Rejected: bidirectional conversion (adds latency and distorts the agent's
  own listening experience for no product benefit).
- **Adaptive per-session playout buffer over a fixed one-shot pre-buffer.** Why: a fixed
  buffer only smoothed the start of a call; mid-call RVC slowdowns still produced gaps/
  reordering with no buffer left. Rejected: raising the fixed pre-buffer duration globally
  (would add latency to every call to cover the worst case instead of adapting per-session).
  See [[subsystem-notes]] for the mechanism.
- **2000ms RVC conversion budget, deliberately shorter than Modal cold-start time.** Why:
  guarantees the lead always hears *something* (raw voice fail-safe) within a bounded
  delay rather than silence while waiting out a 30-90s cold start. Rejected: a longer
  budget that would ride out cold starts, because it trades a worse failure mode (dead
  air) for a better one only in the cold-start case, at the cost of added latency risk on
  every call.
  **Superseded 2026-07-02** by the streaming rebuild's never-raw/fail-closed policy — see the
  new entry below. This budget/raw-fallback design is kept here as historical context for why
  it existed (the tradeoff reasoning was real at the time), not as the current behavior.
- **Modal region pinned to `ap-southeast`** on the assumption Render would be colocated.
  Status: **currently wrong** — Render is in Oregon. Not yet resolved; see
  [[active-backlog]]. Do not treat the region comment in `modal_deploy/worker.py` as
  authoritative without checking current Render region first.
  **2026-07-02 note**: the Modal pin itself was already correct/unchanged going into the
  streaming rebuild and stays `ap-southeast` — see the new entry below. The Render side of
  this mismatch (migrating Render to Singapore) is still pending, tracked as a separate task,
  not done as part of this rebuild.

## 2026-07-02 — Streaming rebuild: persistent WS pipeline, never-raw fail-closed policy, fail-closed warm gate

Replaced the VAD-chunked/HTTP-per-request pipeline (`_conversion_consumer` + `_run_playout`,
described above and in the pre-2026-07-02 revision of LATENCY.md) with a persistent-WebSocket
streaming design: `backend/pipeline.py`'s `VoiceConversionWorker` now drives the converter as
one long-lived duplex stream for the life of a call (`_run_conversion_stream`), a new
`backend/converters/rvc_stream.py::RVCStreamingConverter` speaks to a new `/ws` session
endpoint on the Modal worker (`modal_deploy/worker.py`), and a new `modal_deploy/streaming.py`
holds the pure-numpy block/context/SOLA-crossfade DSP driving the server side of that session.

- **Full rebuild over patching the chunked pipeline.** Why: the VAD-chunk/semaphore/reorder-
  buffer/adaptive-standing-buffer design had already been through multiple revert/re-fix cycles
  (see the migration table above) chasing latency and ordering bugs that were symptomatic of
  the per-request HTTP model itself — every chunk paid RVC's fixed per-request overhead, and
  reordering/buffering only existed to paper over that. A persistent duplex stream removes the
  problem class (nothing to reorder, no per-chunk overhead) rather than tuning around it further.
  Rejected: continuing to tune chunk sizes/buffer targets on the old design, since the class of
  bug (jitter/reordering from parallel per-chunk requests) is inherent to that architecture, not
  a parameter-tuning problem.
- **Never-raw, fail-closed policy — replaces the old raw-audio fail-safe.** Why: the old "forward
  raw denoised audio on conversion timeout/error" design (see the superseded 2000ms-budget entry
  above) was a real production incident, not a hypothetical risk — LATENCY.md §4.2 documents a
  2026-07-02 outbound call where the lead heard the agent's real, unconverted voice for the
  entire call because every chunk timed out. The rebuild deletes the raw-fallback path
  structurally (not just avoids triggering it): on any conversion outage the pipeline publishes
  silence until real converted audio resumes. Rejected: keeping a bounded/short raw-fallback
  window (e.g. "raw voice for the first 2s only") — any duration of raw voice reaching the lead
  is the exact failure this product cannot ship with; a brief hold is an acceptable tradeoff,
  audibly-the-wrong-voice is not.
- **Fail-closed warm gate — replaces the old fail-open `_wait_for_rvc_ready`-only gate.** Why:
  previously, outbound dial and inbound SIP bridge both proceeded even if `_wait_for_rvc_ready`
  timed out (logged a warning and dialed/bridged anyway) — combined with the old raw-fallback,
  an unready GPU meant the lead got raw voice instead of a delayed connection. Now,
  `POST /api/call/outbound` `await`s `worker.wait_until_ready(150.0)` after spawning the bot and
  returns HTTP 503 without dialing if it's not ready in time; `POST /api/call/wait` (inbound)
  only bridges to LiveKit SIP once the call is accepted **and** `worker.is_ready`. Rejected:
  keeping the fail-open gate now that raw fallback is gone — fail-open plus fail-closed audio
  would mean an unready GPU produces dead air mid-bridge instead of a clean pre-bridge 503/hold,
  which is a worse failure mode for both the agent and the lead.
- **Modal region: `ap-southeast` (Singapore) — unchanged, decision not revisited.** The pin
  already predates this rebuild (see the region-mismatch entry above) and stays as-is; this
  rebuild did not touch it. The Render side of the mismatch (migrating the deployed Render
  service to Singapore so it's colocated with the Modal pin) is a separate, still-pending task —
  **not done** as part of this rebuild. Do not treat Render as having moved.
- Also out of scope for this rebuild, deliberately (not oversights): multi-call concurrency
  (the `/ws` endpoint is single-tenant, MVP), `min_containers` Modal keep-warm, and API
  auth/security hardening. See [[active-backlog]] for tracking.
