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
- **Adaptive per-call pitch lock replaces the fixed `RVC_MALE_PITCH_SHIFT` constant
  (2026-07-13/14, spec `docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md`,
  merged `88b3736`).** Why: the fixed shift (`+7`, calibrated 2026-07-08 against a ~137Hz
  agent F0) went stale the moment the agent's live delivery drifted — 07-13 calls measured
  152-158Hz, landing the converted output 1.5-2 semitones above the model's ~208Hz trained
  center, i.e. a wrong-identity voice again. New design: the Modal worker (`/ws` session)
  accumulates the engine's own pre-shift F0 (RMVPE on TRT, PM `compute_f0_uv` masked to
  voiced-only on the ONNX fallback), locks `12·log2(target_f0/median)` once ≥2s of voiced
  speech has been seen, then freezes for the rest of the call; the locked value rides the
  existing `stats` payload so a WS reconnect **resumes** the locked identity instead of
  re-detecting (this is what makes it safe against the exact failure that got the old GPU
  auto-detect reverted 2026-07-03 — see [[subsystem-notes]]). Rejected: continuous mid-call
  F0 tracking (would let the lead audibly hear the voice keep drifting) and per-agent-only
  scope (all agents get the lock; the gender toggle is now just the pre-lock prior).
  `RVC_ADAPTIVE_PITCH=0` (Render env) reverts to the exact legacy fixed-shift behavior with
  no Modal redeploy needed. **Field-confirmed working 2026-07-14** on two live calls (locked
  +3.33st from a measured 171.6Hz, and +5.67st from 149.9Hz — both landing ≈208Hz, both
  math-exact against the formula). **Open follow-up**: the lock produces one audible pitch
  *jump* partway into the call (prior → locked value) that was never listen-tested before
  shipping, and `RVC_TARGET_F0=208` itself is a single 2026-07-08 reference measurement, not
  re-derived from the model's actual training data — see [[active-backlog]].
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
  Status: **RESOLVED 2026-07-03** — Render is now confirmed live in Singapore
  (`srv-d932m4cvikkc73belt1g`, verified via Render API), colocated with Modal. The
  "Render is in Oregon" claim below and in [[active-backlog]]/[[subsystem-notes]] was stale;
  the Render service ID also changed (from `srv-d92lh7navr4c738i03a0`), consistent with the
  migration having actually happened at some point without the docs being updated.
  **2026-07-02 note (historical, superseded by the above)**: the Modal pin itself was already
  correct/unchanged going into the streaming rebuild and stayed `ap-southeast` — see the
  2026-07-02 entry below. The Render side of this mismatch (migrating Render to Singapore)
  was tracked as a separate, still-pending task as of that rebuild; it is no longer pending.

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

## 2026-07-03 — Cost/fan-out fixes, pitch-detection revert, latency-for-quality tradeoff

Session triggered by the user asking why 4 `rvc-worker` GPU containers were running
simultaneously in the Modal dashboard, which led into fixing several compounding issues in
the same call path. All changes deployed (`modal deploy` + `git push`, Render auto-deploys).

- **`max_containers=1` added to the Modal worker.** Why: the in-process single-tenancy gate
  (`_session_active`) only holds within one container; Modal's autoscaler doesn't know only
  one should ever run, and was spinning up a new paid GPU container per connection attempt
  that arrived while another was cold-starting/busy. Rejected: leaving it unbounded (direct
  cost bleed — 4x concurrent T4 GPU-seconds for an app designed for exactly 1 concurrent
  call). See [[subsystem-notes]].
- **Pitch/gender auto-detection reverted to the manual UI toggle.** Why: confirmed via
  production Modal logs misdetecting a known-male agent as female twice the same day,
  feeding audio outside the trained voice's pitch range (sounds like "not the trained voice,"
  not just mis-pitched) — and it re-runs from scratch on every WS reconnect since the
  detected value is never persisted/reported back to the client. Rejected: hardening the
  autocorrelation detector instead (more effort, still heuristic) when the ground truth
  (agent's own gender) is already known and available via the existing UI toggle — use it.
  See [[subsystem-notes]].
- **FAISS index caching (monkeypatch, not a vendored-code edit).** Why: `RVC/`'s own
  `pipeline.py` re-reads and fully reconstructs a 221MB FAISS index from disk on every
  `vc_single()` call — a non-issue for one-shot file conversion, ~1.4-2.0s of dead weight
  per ~480ms streaming block. Rejected: editing the vendored `RVC/` file directly (this repo
  treats `RVC/` as third-party, not Keira's own code to maintain) in favor of monkeypatching
  `faiss.read_index` from `worker.py` instead — same effect, zero vendored-code changes. See
  [[subsystem-notes]]. Confirmed in production logs: `npy` time dropped ~1.4-2.0s → ~0.05s.
- **Explicit product decision: call latency is not a priority, voice continuity is.**
  Directly reverses part of the 2026-07-02 streaming rebuild's implicit latency-first
  framing — not a regression of that rebuild, a deliberate re-prioritization once the FAISS
  fix alone wasn't enough to fully stop "part by part" audio. Drove two follow-on changes:
  `BLOCK_MS`/`CONTEXT_MS` increased 320/160 → 1000/400 (more context per inference call,
  ~3x fewer SOLA crossfade seams/second), and the one-shot 100ms jitter buffer replaced with
  a bounded (~3s target/~5s cap, drop-oldest) standing playout buffer in `pipeline.py`
  (`_run_playout_consumer`). Rejected: chasing a fully real-time-synchronous pipeline further
  (e.g. GPU tier bump, ONNX/TensorRT) as the *first* lever, since those add engineering risk
  for a latency goal the product no longer has — deferred to [[active-backlog]] as the next
  lever only if the buffer approach proves insufficient on a live call. See
  [[subsystem-notes]].
## 2026-07-03 (later) — Root-caused "voice not clear / breaking" to a dead SIP-isolation fix, not the pipeline

User-reported symptom ("voice was not clear and it was breaking" while speaking on a live
call) turned out to be unrelated to the playout-buffer/FAISS work earlier the same day —
traced instead to the *raw+converted audio mixing* bug that `_restrict_sip_audio`
(`backend/main.py`, commit `1324fe2`, "made changes for getting mixed voices") was supposed
to have already fixed.

- **Root cause: wrong protobuf field name, 100% silent failure.** `_restrict_sip_audio` built
  `api.UpdateSubscriptionsRequest(..., participant_identity=resolved_sip_identity, ...)` — but
  the real field on that message is `identity`, not `participant_identity` (confirmed via
  `livekit.protocol.room.UpdateSubscriptionsRequest.DESCRIPTOR.fields_by_name`). Every attempt
  threw `Protocol message UpdateSubscriptionsRequest has no "participant_identity" field`,
  confirmed via Render logs (`srv-d932m4cvikkc73belt1g`) across *every* call sampled — the
  isolation logic never once succeeded, so the SIP/lead leg stayed subscribed to both the raw
  agent mic track and the bot's converted track for the entire call. Two simultaneous copies
  of the same speech (different pitch, different network timing) is what produced the
  reported "not clear, breaking" audio — not a pipeline/latency issue.
  Note: `CreateSIPParticipantRequest`'s `participant_identity="sip-lead"` field (`main.py:527`,
  outbound dial) is a *different* message that genuinely has that field name — that call site
  was already correct and was not touched.
- **Fix**: changed the one call site to `identity=resolved_sip_identity` (commit `cf60ca5`).
  Rejected: rewriting `_restrict_sip_audio`'s retry/detection logic — the detection logic
  (finding the SIP participant and raw agent track SIDs) was working correctly per the logs;
  only the final API call's field name was wrong, so a single-line fix was correct, not a
  larger rework.
  **Confirmed live 2026-07-03**: pushed, deployed, and `[SIP Isolation] ✅ unsubscribed`
  now appears on every outbound call sampled since (15:05, 15:07, 16:10, 17:05, 17:07),
  zero failure lines. Resolved.

- **Considered and explicitly rejected for now: swapping to a causal/streaming-native voice
  model** (e.g. in the shape of Google's StreamVC) to eliminate block-based inference
  entirely. Why rejected: HuBERT (RVC v2's feature extractor) is bidirectional/non-causal by
  architecture, has no incremental mode — "removing chunking" would mean a different model
  family and **retraining the Keira voice from scratch**, not a pipeline code change. The
  current block+context+SOLA-crossfade approach is also not naive — it's the same technique
  RVC's own real-time community tooling (w-okada's voice-changer) uses to make this model
  family behave in near-real-time. Revisit only if inference-speed engineering (GPU tier,
  ONNX/TensorRT) turns out insufficient.

## 2026-07-05 — TensorRT migration planned (implementation_plan.md); phased playout-buffer reversal approved

Planning-only session: analyzed a user-drafted proposal to migrate the Modal worker's
inference to TensorRT on the L4, verified it against the codebase, and wrote
`implementation_plan.md` (repo root). Execution is delegated to a **different model**;
this agent's role is reviewing the resulting diffs against the plan's gates.

- **TRT via 3 static-shape engines (HuBERT / SynthesizerTrnMs768NSFsid / RMVPE E2E), FAISS
  stays NumPy.** Why: matches the real `Pipeline.vc` structure; FAISS retrieval can't be a
  TRT engine; RVC's own `models_onnx.py` proves generator exportability. Primary motivation
  is **re-enabling RMVPE** (pm was a pure ~300ms-latency tradeoff, see 2026-07-03 entry) at
  streaming-compatible speed — quality first, latency headroom second.
- **Static shape corrected during analysis: NOT 22,400 samples.** The vendored pipeline
  reflect-pads `t_pad = 16000×x_pad` per side, and `BlockAccumulator`'s first blocks have
  0..6400 context. Plan pins a fixed 16,000/side pad (54,400 into HuBERT) and zero-fills
  short blocks, making output exactly 3× input. Rejected: exporting at raw 22,400 (would
  have failed at runtime on every first block and mismatched production padding).
- **Engines built on the L4 and cached to the `rvc-models` volume, never at image build.**
  Why: Modal image builds have no GPU; engines are SM89/TRT-version-specific; cold start is
  already ~75s and an uncached engine build adds minutes. `/health` exposes cache state.
- **Phased playout-buffer reversal — user-approved 2026-07-05, partially reverses the
  2026-07-03 "latency is not a priority" decision.** Phase 1: `_PLAYOUT_BUFFER_TARGET_BYTES`
  3.0s → 1.25s (floor for `BLOCK_MS=1000` — converted audio arrives in ~1s bursts, so a
  cushion below one block interval starves every cycle). Phase 2 (0.25s + smaller
  `BLOCK_MS`): separate future plan, gated on live TRT p95 ≤ 400ms sustained. Rejected: the
  proposal's original direct 0.25s cut (mathematically below block-arrival granularity —
  gaps guaranteed regardless of GPU speed).
- **Fail-closed preserved**: TRT init failure falls back to the PyTorch *converted* path
  only — never raw. All `modal deploy`/push steps in the plan are marked [USER-RUN].

## 2026-07-03 (later still) — Voice-identity mismatch: five hypotheses ruled out, new diagnostic tooling built

Separate from both the buffer/latency work and the SIP-mixing fix above: converted voice
still doesn't match the trained "Keira" voice on live calls, even with those two fixed. Full
narrative in [wiki/pages/issues/voice-identity-mismatch-investigation.md](../../wiki/pages/issues/voice-identity-mismatch-investigation.md)
— summary of decisions here:

- **`RVC_INDEX_RATE` env var added, default bumped 0.75→0.9.** Why: hypothesis that not
  enough FAISS-retrieved target timbre was coming through. **Ruled out empirically** — a
  real call confirmed `index_rate=0.9` applied, symptom persisted. Rejected further
  index_rate tuning as a lead; kept the env var (harmless, reversible) rather than reverting,
  since re-tuning it costs nothing and it's now easy to test again if a future symptom
  actually correlates with it.
- **Built `convert_file_chunked`/`main_chunked` instead of guessing a fourth parameter.**
  Why: two speculative parameter fixes (pitch — separately already fixed — and index_rate)
  had both failed to explain a live-only symptom; continuing to guess parameters violates the
  "3+ failed fixes → question the architecture, don't keep guessing" principle. Built a
  diagnostic that replays the live pipeline's actual block+SOLA+noise-suppression logic
  offline instead, so hypotheses could be tested with direct evidence rather than another live
  call each time. Rejected: continuing to test only against the *live* path per hypothesis,
  which is slower (needs a real call each time) and conflates multiple variables at once
  (network, SIP, telephony) instead of isolating pipeline logic specifically. See
  [[subsystem-notes]] for the tool itself.
- **Chunking+SOLA and noise suppression both ruled out via that tool.** Both replayed offline
  against the same reference file the known-good single-pass test uses, both still sounded
  correct. This leaves the pipeline's own audio processing looking clean as far as it's been
  possible to isolate and test.
- **Added temporary raw-audio capture (`_DEBUG_SAVE_RAW_AUDIO`) to the live path rather than
  keep guessing pipeline-side.** Why: after four pipeline-side hypotheses were ruled out, the
  more informative move was checking whether the *input* itself differs live vs. offline
  (real mic/room vs. a clean test file) rather than a fifth pipeline guess. This is a
  production-code change to the live `/ws` handler, deployed deliberately (asked first, per
  standing practice on this project) rather than assumed. **Not yet reverted** — must be
  flipped back to `False` and redeployed once the investigation concludes; tracked in
  [[active-backlog]] specifically so it isn't forgotten.
- **GPU tier found stale (T4 deployed despite `gpu="L4"` committed) — not a decision, a
  discovery.** Recorded here because it affects how to read every diagnostic comparison run
  during this investigation: all of them (the T4-pinned `convert_file`/`convert_file_chunked`
  tools, and the live calls tested against them) ran on matched T4 hardware. The live worker
  is now on L4 as of an incidental redeploy — future live-call tests will not be GPU-matched
  against the existing diagnostics unless that's revisited. See [[subsystem-notes]].

- **Keep-warm loop: env-gated (option b), default OFF (2026-07-06).** The `_rvc_keepwarm_loop()`
  in `backend/main.py` now exits immediately unless `RVC_KEEPWARM=1` is set in Render's env.
  Rationale: 24/7 keep-warm on an L4 costs ~$550–600/mo for a capability that's only needed
  during active shifts. The env-gate preserves the capability with zero redeploy cost — set
  `RVC_KEEPWARM=1` at shift start from the Render dashboard, clear it at end-of-shift.
  Cold-start risk (~75s) is accepted during unwarmed periods; the fail-closed engine check
  (A1) ensures the container never serves raw audio while loading, so the user hears silence
  (or a hold message) rather than the agent's real voice during that window.
  Rejected: option (a) delete entirely (loses the capability permanently without a code deploy),
  option (c) keep 24/7 (unacceptable ongoing cost).

## 2026-07-07 — TensorRT migration merged to main

`trt-migration` branch merged into `main` (merge commit `9c1093a`, done directly by the
project owner). Carries all round-3 hardening fixes, the committed vendored ONNX-export
shims (`4bdbe5f`, `cd7749c`), and the passed C3 GPU benchmark (median 66ms/p95 68ms vs.
≤400ms gate, 2026-07-06 L4 ap-southeast run). Still open post-merge: C4/C5 (offline A/B +
listen test for the deterministic-noise shim's breathiness tradeoff) and confirming the
live Modal deploy actually serves TRT (`/api/health` → `"engine": "trt"`) rather than
inferring it from committed code — see [[active-backlog]], [[subsystem-notes]].

## 2026-07-07 (later) — Phase 2 (320ms block / 0.25s buffer) shipped same-day, ahead of its own gate

`38fbef5` shrank `BLOCK_MS` 1000→320 and the playout buffer 1.25s→0.25s, with
`b9df41f` fixing a stale-TRT-engine-cache bug found in the process (cache keyed by
filename, not shape hash — a `BLOCK_MS` change left old engines that failed with a
"Static dimension mismatch" until the cache files were purged before rebuild). `7164b85`
recorded the new benchmark: 54ms median / 55ms p95 (down from Phase 1's 66/68ms), gate
still passed (≤400ms) with real-time ratio down from 21× to 13×.

**Flagging a process deviation, not blocking it retroactively**: the 2026-07-05 decision
log entry gated Phase 2 on "live TRT p95 ≤ 400ms sustained over ~a week" of Phase 1 running
in production. Phase 2 shipped ~1 day after the Phase 1 merge, and there's no evidence in
this repo of a live (as opposed to offline-benchmark) soak period happening in between —
worth confirming with the user whether that gate was consciously waived or just skipped.

Same day, `d463c41` found and fixed a real audio-quality regression from the original
TRT shim: zeroing `SineGen`'s unvoiced-frame noise (see [[subsystem-notes]]) made unvoiced
segments fully silent, causing audible hissing/garbled consonants. Fixed by generating real
`N(0,1)` noise outside the ONNX graph (numpy RNG in `trt_pipeline.py`) and passing it in as
a model input — this is now the reference pattern for any future TRT-incompatible random
op, and worth a fresh C5-style listen-test pass since it changes the generator's audio
output.

Remaining open, same as before: C4 (offline A/B WAVs) and a fresh C5 listen test against
this fix specifically, plus confirming the live Modal deploy actually serves TRT — see
[[active-backlog]].

## 2026-07-07 (evening) — Presence EQ on converted output for PSTN clarity

A live-call recording (WhatsApp/Android capture in `voices/`) prompted a clarity
investigation. Signal analysis cleared the pipeline itself: TRT streaming output
(`test11_chunked_4.wav`) is spectrally identical to the pre-TRT baseline out to 6kHz,
and the publish path adds no coloration. The real ceilings are (a) the G.711 PSTN leg
(hard ~3.4kHz cap — physics, not fixable) and (b) that specific recording chain
(~8kbps/ch AAC Android call recorder, which destroyed everything above ~800Hz and is
NOT representative of what the lead hears).

Decision: boost the one band we control that PSTN actually carries — new
`PresenceEQ` in `backend/audio_eq.py` (+4dB, 1.2–3.4kHz, linear-phase FIR,
frequency-sampling design, numpy-only since scipy isn't a dep) applied in
`_run_conversion_stream` before the playout buffer. Streaming state (taps−1 input
tail) makes chunked output byte-identical to single-pass — no boundary clicks.
Env `PRESENCE_EQ_GAIN_DB` (default 4, 0 disables). Group delay ~1.3ms, negligible.

Deliberately NOT done without asking: Twilio server-side call recording (the decisive
instrument for measuring what the lead actually hears within the 3.4kHz band) — it's
an infra/billing mutation; user chose the EQ first. If clarity complaints persist
after the EQ, that recording test is the next diagnostic step.

## 2026-07-08 — Voice identity + clarity root-caused and fixed; call-analysis instrumentation

Ran the recording test flagged as "next diagnostic step" in the 2026-07-07 EQ entry.
Built a **3-point call capture** to measure what the lead actually hears vs each pipeline
stage: (1) Twilio trunk dual-channel recording (`record-from-answer-dual` on trunk
`TK8958…da94`), (2) per-call Modal in/out debug WAVs (`_DEBUG_SAVE_AUDIO` in `worker.py`,
saving 16kHz post-denoise input + 48kHz post-SOLA output to the volume `debug/` dir —
replaces the TRT-migration-dropped `_DEBUG_SAVE_RAW_AUDIO`), (3) Render logs
(now legible after adding `PYTHONUNBUFFERED=1` — prints were block-buffered under one
timestamp). See [[subsystem-notes]] for the analysis method.

**Finding 1 — SIP-leg packet loss (fixed).** 2026-07-08 baseline call: pipeline itself
clean (no clipping/underruns), but 5 dropouts of 0.18–0.40s of *loud* speech (~1.4s/68s)
in Twilio's own recording with flat GPU→Twilio delay ⇒ RTP loss on the LiveKit→Twilio leg,
not playout underrun, not the lead's mobile net. Root cause: the plain
`{trunk}.pstn.twilio.com` termination domain does NOT geo-route — resolves to Twilio
US/Virginia, a transpacific hop from Singapore LiveKit/Render. Fixed by re-pointing the
LiveKit outbound trunk `ST_BMamqedncjzb` (in place, ID unchanged) to
`…pstn.singapore.twilio.com`, matching `.env`/Render `TWILIO_SIP_URI`, and adding
`;edge=singapore` to the Twilio origination URL (inbound). Post-fix call: 1 dropout.

**Finding 2 — voice never matched the trained voice + muffled (fixed).** Confirmed present
since the start on BOTH TRT and pre-TRT engines (reproduced offline from git `326098e`), so
NOT the TRT migration. Two independent causes:
- **Identity = pitch overshoot.** Hardcoded `pitch_shift = 12 if male else 0` doubled the
  user's ~137Hz fundamental to ~274Hz — ~5 semitones ABOVE the mi-test model's ~208Hz
  center (measured output F0: +12 → 271Hz vs 208Hz on the good offline reference). RVC
  driven outside its trained range → wrong identity. Correct shift for a ~137Hz agent is
  **+7** (→~205Hz). The 2026-07-07 offline test only sounded right because its clean input
  auto-detected female→0 shift.
- **Muffle = double noise-suppression.** Agent voice was NS'd twice pre-model: browser
  `getUserMedia({audio:true})` defaults (noiseSuppression + autoGainControl) THEN server
  `WebRTCNoiseSuppressor(ns_level=3)`, stripping the HF detail HuBERT needs (live input
  centroid 413Hz vs 720Hz clean, −9dB at 6-8kHz; propagates to −3dB presence in the output).

Fix (commit `f748a89`, both env-gated with legacy defaults so revertible from Render with
no code change): `RVC_MALE_PITCH_SHIFT` env drives the male shift (default 12, Render set
**7**); `NS_LEVEL` env drives server suppression (default 3, Render set **1**); frontend
`app.js` captures raw mic (noiseSuppression/autoGainControl OFF, echoCancellation kept ON
for speaker-echo safety). Also enabled Twilio recording (billing/consent — user-approved).

**Deliberately gated on user approval** (per [[feedback_production_actions]]): every live
mutation here (Twilio recording, trunk edge re-point, Render env changes, `git push`
auto-deploy, Modal `worker.py` redeploy for the debug tap) was confirmed with the user first.

Still open: one post-fix verification call (expect input centroid → ~720Hz, output F0 →
~205Hz); if it's clear + on-pitch but still not an exact match, the remaining gap is model/
index training quality or the 16kHz HuBERT ingress ceiling, not these knobs. PSTN ~3.4kHz
still caps what the lead hears regardless. See [[active-backlog]].
