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
| 2026-07-17 | `634c4fb` (reverted `e82bb29`) | Tried an explicit real-time pacer (`next_publish_time`) in `_run_playout_consumer` to fix backlog-burst breaking; implemented, tested, and task-reviewer-approved, but reverted by the user same-day without a stated reason. See narrative entry below. |
| 2026-07-02 | `f3c16ed` (Tasks 1-5) | Streaming rebuild: replaced the VAD-chunked HTTP-per-request pipeline (rows above) with a persistent-WebSocket duplex streaming design, a never-raw/fail-closed audio policy, and a fail-closed pre-dial/pre-bridge warm gate — see the dedicated entry below. |

Note the `eb016f3`/`da46c48` "Reverted: Buffer Changed for voice issue" commits bracketing
this sequence — the buffer/pre-buffer design went through at least one full revert cycle
before landing on the current adaptive approach. If a buffering bug resurfaces, check
whether it's a regression of something already tried and reverted here before re-deriving
a fix from scratch.

## Decisions
- **RVC-first SaaS pivot; LLVC training paused (2026-07-16).** The product requirement is
  multi-tenant: each client supplies an authorized target voice and should reach a usable
  preview without training a new large model. That invalidates LLVC as the default product
  engine because the current LLVC recipe distills a separate target-specific student for
  every voice. Preserve the completed LLVC protocol/safety/benchmark work, keep
  `LLVC_PILOT_ENABLED=false`, and do not generate the 360-hour corpus or deploy a real LLVC
  service. First make the existing RVC path's profile telemetry truthful, measure a warm
  staff PSTN baseline, and test only the 160/240/40/160 ms Candidate B geometry against
  shared quality gates. Revisit a streaming zero-shot engine only after the RVC baseline is
  stable; LLVC remains a possible future per-tenant compilation optimization for unusually
  high-volume fixed voices, not the SaaS onboarding path.
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

## 2026-07-15 — Second-brain sync repair; control-plane security landed; Alia naming aligned

**Local `main` had drifted 35 commits behind `origin/main`, and an earlier session's
uncommitted second-brain-audit draft was authored against that stale snapshot.** Pulled
`origin/main` first and discarded the stale draft rather than merging it — reapplying it
would have deleted current `.agents/` content (adaptive-pitch-lock docs, presence EQ, the
gulp-drain finding) and reintroduced superseded claims (e.g. "dashboard hardcodes male,"
no longer true post adaptive-pitch-lock). Salvaged only the two pieces that were still
valid regardless of staleness: stripped a live Render MCP bearer token from tracked
`.mcp.json` (rotation not yet done — see [[active-backlog]]), and gitignored a local
`debug/` scratch dir. Rejected: merging the stale draft by hand, since too much of it
directly contradicted current reality.

**Trimmed CLAUDE.md's inline pipeline/SIP mechanism section to pointers into
`.agents/`, instead of refreshing its stale numbers.** Why: a second copy of load-bearing
constants (block size, buffer target) that isn't the source of truth will always drift
again — this dump is exactly why CLAUDE.md and `.agents/` had gone out of sync. Also
gitignored `graphify-out/` (regenerable codemap artifact) and replaced the untracked,
byte-for-byte-duplicate `AGENTS.md` with a symlink to `CLAUDE.md`. Committed `276d37a`.

**A second, larger uncommitted change set appeared mid-session — not authored by this
agent — implementing the P0 "secure the public control plane" backlog item**:
`backend/security.py` (constant-time bearer-token verification + E.164/identity/gender
validators), wired into `main.py`'s operator/dashboard routes via `KEIRA_CONTROL_TOKEN`;
`backend/test_control_plane.py`; matching README/render.yaml docs; and a
`make session-close` read-only repo/wiki/secret-audit script. Ran both test suites
(`backend.test_control_plane`, `backend.test_pipeline`) before trusting it — both passed —
and confirmed `verify_bearer_token` is actually called from `main.py`, not a dangling
helper. Committed `e501104` and pushed, after confirming no live call was in progress
(Render `autoDeploy: commit` redeploys — and kills in-flight calls — on every push).

**Aligned the new close-out script with the user's existing global Alia skill's naming
convention.** Alia expects `scripts/second_brain_close.py` + a `second-brain-close` Makefile
target; the concurrently-added script used `session_close.py` / `session-close`. Renamed
(`git mv`) rather than leaving the mismatch, so `/alia` resolves in this repo without a
special-case check every time. Left the historical plan doc
(`docs/superpowers/plans/2026-07-15-control-plane-and-session-close.md`) and `wiki/log.md`'s
existing entry under their original name — they're records of what was true when written,
not the current convention. Ran `make second-brain-close`: CHECKS PASSED (0 wiki-lint
errors, 0 credential matches, 0 stale-claim matches).

## 2026-07-16 — RVC-first optimization; LLVC paused; measured baseline before tuning

**Changed the product assumption before spending more training effort.** Keira is intended
as a SaaS where each client uploads a target voice, not a single fixed Keira brand voice.
A separately trained causal LLVC model per client would make onboarding slow and expensive,
so LLVC training/deployment is paused and `LLVC_PILOT_ENABLED=false`. Its verified streaming
and fail-closed scaffolding stays in the tree. Decision: optimize the existing lightweight
RVC onboarding path first, then investigate a zero-shot or lightweight-conditioned streaming
VC engine. Rejected: generating a very large teacher corpus or treating fake-server LLVC
latency as model performance.

**Made RVC measurements self-identifying before changing geometry** (commits `cfe7891`,
`a379f59`, `d8e83c9`, `e90a1a1`). Modal readiness/stats now expose the effective named
profile, block/context/SOLA/output geometry, TensorRT/cache state, and an artifact-derived
model/index fingerprint. The backend no longer assumes 320ms when accounting for converter
blocks. `scripts/rvc_stream_benchmark.py` drives one continuous authenticated WebSocket and
reports readiness, inference, converter/network estimates, duration delta, and drops.
Baseline and Candidate B profiles are defined; Candidate C is not implemented and Candidate
B has no matching promoted TensorRT artifacts yet.

**Deployed and verified Modal v10/v11 after the user rotated credentials and authenticated
the `thezhafoundry` workspace.** The stable function remains `fastapi_app`, with Singapore
compute and Modal's default US input routing. Commit `7a24200` added a parallel
`fastapi_app_ap` only for measurement (`routing_region="ap-south"`, broad `region="ap"`);
its first container landed in Tokyo. Render remains on the stable endpoint. Rejected:
switching Render based on a laptop benchmark or assuming broad AP placement means Singapore.

**First live baseline (developer laptop → stable Modal edge):** 9.6s input, 30 blocks,
72,510.73ms cold active readiness; TensorRT inference median/p95 50.75/51.61ms;
converter wait 1207.11/1358.56ms; estimated network 837.05/988.91ms; zero drops;
9388.54ms output, delta -211.46ms. The result verifies the model/profile/TRT path, but is
not a Render-origin or PSTN mouth-to-ear result. The duration loss, Render-origin stable/AP
A/B, non-fatal `F0Predictor` startup-warm-up import failure, and a warm staff PSTN test are
hard gates before lowering block/buffer sizes. The one-second adaptive-pitch interpolation
is implemented/tested in v11 but still needs that live listen test.

## 2026-07-17 — Real-time playout pacer implemented, reviewed, deployed, then reverted same-day

**Diagnosed why "voice was clear at first, then breaking" from a real PSTN call's Twilio
recording and `[Worker][LatencySummary]` telemetry.** `playout_buffer_bytes` was clean for
the first ~15-20 blocks then oscillated between ~0 and 500-740ms for the rest of the call,
matching the 2026-07-14 "Open finding" in [[subsystem-notes]] almost exactly — the 2026-07-15
bounded-100ms-drain fix for that finding had not resolved it. Root cause: `_run_playout_consumer`
had no wall-clock pacing of its own, only LiveKit's `capture_frame` backpressure (~200ms queue
headroom), which still let a backlogged chunk through faster than real time whenever
`converter_wait_ms`/`network_rtt_ms` (consistently 900-1900ms against a 320ms block budget)
put the pipeline behind.

**Implemented a self-correcting `next_publish_time` pacer** in `_run_playout_consumer`
(plan: `docs/superpowers/plans/2026-07-16-playout-consumer-real-time-pacing.md`) via
subagent-driven-development. Task-reviewer approved the code (all Global Constraints held,
correct self-correction on genuine stalls); the one Important finding was about the
implementer's report overstating verification confidence, not a code defect, and was
corrected directly. Deployed to Render (`634c4fb`..`a265262` pushed to `origin/main`).

**Reverted same session, same day, before any live listen test happened.** The user asked to
revert `634c4fb` (code) and, separately, `14f9bca` (the backlog/subsystem-notes doc claims
this fix generated) — both as explicit, deliberate revert commits (`e82bb29`, `c5eea8c`),
pushed to `origin/main`. **The user did not state a reason** despite being asked directly;
`main` is now back to exactly its pre-2026-07-17 state (the 2026-07-15 bounded-100ms fix is
again the last real attempt, `active-backlog.md` again says "targeted live listen test
pending"). Do not re-propose this identical pacer design without first asking the user why it
was reverted — the plan document and this entry exist so the reasoning/code aren't lost, but
the revert itself is unexplained and should not be treated as "just needs to be redone."

## 2026-07-19 — Removed the KEIRA_CONTROL_TOKEN operator-auth gate (frontend + backend)

**Removed the bearer-token control-plane auth added 2026-07-15 (`e501104`, see entry above)
at explicit user request ("remove the kiera control token thing" → confirmed scope as "remove
everywhere both front and back").** Deleted: `frontend/app.js`'s `getControlToken()` prompt,
the `Authorization: Bearer` header on `apiFetch`, and the `keira-auth.<token>` WS subprotocol;
backend `require_control_token` dependency and its use on every operator route
(`/api/token`, `/api/start-bot`, `/api/stop-bot`, `/api/call/*`, `/api/setup`, `/api/warmup`,
`/api/deploy`), the `/api/call/ws` handshake check, `CONTROL_PLANE_TOKEN`, and the now-dead
`verify_bearer_token` helper + its unit test in `backend/test_control_plane.py`. Also dropped
`KEIRA_CONTROL_TOKEN` from `render.yaml`, `README.md` §3, and CLAUDE.md's control-plane rules
and env-var list. Twilio signature validation (`require_twilio_signature`) and rate limiting
are untouched — this only removed the operator/dashboard bearer-token gate.

**Net effect: every operator HTTP route and the call-events WebSocket are now unauthenticated.**
On the public Render deployment this means anyone who can reach the URL can start/stop bots,
place outbound calls, or trigger `/api/deploy` — this is a deliberate regression of the P0 fix
from `e501104`, not an oversight. If control-plane auth is wanted again later, `e501104`'s diff
(and this repo's git history around 2026-07-15) has the previous implementation to reference
rather than rebuilding from scratch.

**Correction, same day:** this was made and left as a local-only, uncommitted change per
explicit instruction ("do not push the code into main"). The user then committed it directly
in the IDE anyway (`1f1fb5d`, "Removed Control Token from backend and frnotend", 16:22 IST)
and pushed to `origin/main` — outside this session's visibility; only surfaced during
`second-brain-close`. Per `render.yaml`'s `autoDeploy: commit`, that push has already
redeployed the backend, so **the control plane is unauthenticated in production right now**,
not just locally. See [[active-backlog]]'s updated row. Consistent with the established
[[feedback_concurrent-repo-edits]] pattern — re-check git state after any gap rather than
trusting the last-known instruction.

## 2026-07-19 — Two call-diagnosis findings: GPU preemption outage, then a SOLA micro-glitch

**Call 1, 16:29:59 IST (`CA7b845842d9d8eda788c8f2143f32feb2`, 65s):** agent-voice audio ran
normally for the first 33s then went completely silent for the remaining ~31s of a still-
connected call. Cross-referencing the Twilio dual-channel recording (envelope analysis),
Render's `[RVCStreamingConverter]` logs (endless "reconnect buffer full — dropped oldest
input frame" spam plus one logged `WS connection lost/failed: server rejected WebSocket
connection: HTTP 500`), and Modal's own worker logs for the window pinned the root cause to
the second: **Modal preempted the GPU_L4 container serving the call at 16:30:32 IST** (`
Container terminated due to preemption`) — exactly the moment the audio dropped — and the
`ap-southeast` region was capacity-constrained enough that no replacement container came up
for the rest of the call, or for 13 minutes afterward. This is a Modal platform-level/regional
capacity issue, not a pipeline bug — the fail-closed silence-on-outage design (see
[[subsystem-notes]]) behaved correctly. Directly corroborates the open "A/B Modal routing"
backlog row (`ap-southeast` vs `ap-south`/broader `ap`) as an availability risk, not just a
latency one. No code changed for this finding.

**Call 2, 21:20:40 IST (`CA097fa9be0e716fb489b373bb89e474a6`, 62s), after `DEBUG_SAVE_AUDIO=1`
was turned back on for this investigation (see [[active-backlog]]):** user reported one word
("stating," from a fixed diagnostic script) sounded unclear while the rest of the call was
fine. With the Modal debug `in16k`/`out48k` WAV pair now available, a direct pre- vs. post-
conversion comparison (5ms RMS envelope) around the phrase's timing (~19.2–21.5s) found the
raw input continuous and unbroken there, but the converted output had a genuine **~40–60ms
silence gap spliced in mid-phrase at t≈20.66–20.72s** that doesn't exist in the source. Modal
and Render logs for this call's window are clean — no preemption, reconnects, or errors — so
this is a different failure class from Call 1: a small, intermittent artifact consistent with
a SOLA-splice/block-boundary handoff (320ms block, 80ms SOLA crossfade) rather than a
data-loss or capacity event. Leading hypothesis only, not yet root-caused in
`modal_deploy/streaming.py`; flagged for the user, no code changed.

## 2026-07-23 — Merged origin/main; re-dropped control-token auth from the desktop feature

**Local `main` (20 commits, the desktop voice-changer feature) had diverged from
`origin/main` (12 commits) with real conflicts, not just textual ones.** The desktop
feature was built on a base that predated the 2026-07-19 control-token removal (see above)
and assumed `KEIRA_CONTROL_TOKEN` auth still existed — its new `/api/desktop/session`
endpoint and `require_desktop_session_auth`/`_local_no_auth_allowed` machinery depended on
`require_control_token`. Merging naively (accepting `HEAD`'s conflict-marked side, which is
what a plain `git merge` auto-resolves toward for the unmarked hunks too) would have
silently reintroduced auth on 10+ operator routes, `/api/call/ws`, and the desktop session
endpoint — reversing a change already confirmed live in production and, per that entry,
made at explicit user request. Asked the user directly rather than guessing; confirmed:
**drop auth everywhere, matching `origin/main`**, including on the new desktop endpoints.

**What that took, beyond resolving the 3 marked conflict hunks in `backend/main.py`:** git's
3-way merge silently (no conflict markers) resolved several *unmarked* hunks toward
`origin/main`'s side purely because HEAD hadn't touched them, which combined with a naive
"take HEAD" resolution of the marked hunks would have left the merged file internally
inconsistent — e.g. `Depends`/`Header` imports present but every other route decorator
missing `dependencies=[Depends(require_control_token)]`, `verify_bearer_token` called in a
websocket handler but not imported, `CONTROL_PLANE_TOKEN` referenced but never defined. Also
silently dropped by the same mechanism: `verify_bearer_token` itself (`backend/security.py`),
its unit test (`backend/test_control_plane.py`), the `KEIRA_CONTROL_TOKEN` `render.yaml`
entry, and all of `frontend/app.js`'s `getControlToken()`/`Authorization` header/WS
subprotocol logic. Diffing each touched file against both the pre-merge `HEAD` and
`origin/main` (not just eyeballing conflict markers) is what surfaced these — a `git merge`
with zero conflict markers reported is not proof nothing was lost from either side.

**Resolution:** removed the control-token/local-no-auth scaffolding from `backend/main.py`
entirely (all 11 route decorators, the `/api/call/ws` handshake check, `require_control_token`,
`require_desktop_session_auth`, `_local_no_auth_allowed`, the `KEIRA_LOCAL_*` env vars, the
now-unused `Depends`/`Header`/`urlsplit` imports); restored `frontend/app.js` to
`origin/main`'s tokenless version; rewrote `backend/test_desktop_audio.py`'s three
auth-dependent tests down to one (`/api/desktop/session` now issues a ticket with no auth
header at all) and deleted the two testing removed functions
(`_local_no_auth_allowed`/`require_control_token`); updated `README.md`'s desktop-setup
section, which still described the old "authenticated deployment vs. local no-auth mode"
split as if both existed. The desktop feature's own ticket-based WS auth
(`DesktopSessionStore`, single-use `keira-desktop.<ticket>` subprotocol) is unrelated to the
operator control-token gate and was left untouched — it was never gated by
`require_control_token` for the actual audio relay, only session issuance was. Ran
`python -m backend.test_pipeline` and `pytest backend/test_control_plane.py
backend/test_desktop_audio.py` after, both clean, before committing the merge (`6e284bd`).

**Net effect unchanged from the 2026-07-19 entry: the control plane, including the new
desktop-session endpoint, remains fully unauthenticated.** If control-plane auth is wanted
back, this merge commit's parent (`88e1801`) and `e501104` (original P0 add) both have a
working implementation to reference.

## 2026-07-23 — Desktop relay validated on macOS; cold Modal capacity is the gating failure

**Separated the disabled desktop buttons from actual conversion health instead of treating
them as one failure.** The page had been opened in the Codex in-app browser, whose test
surface did not complete the required Web Audio setup or expose the complete CoreAudio device list.
That kept the page in its fallback state and showed only built-in devices. The correct
acceptance surface is a current regular Chrome/Edge build with microphone permission.
For the page's contracts, **Test converted voice does not require a virtual device**; only
**Start conversion** requires `AudioContext.setSinkId` plus BlackHole/Loopback/CABLE Input.

**Verified BlackHole below the browser boundary.** The installed driver is
`/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver`; macOS CoreAudio reports `BlackHole 2ch`
with two input and two output channels at 48kHz. This rejects the hypothesis that the
dropdown was empty because the driver was absent. Chrome was opened at
`http://127.0.0.1:8000/desktop/`; the remaining user-run step is to grant microphone
permission, confirm the device appears, and perform the human mic/WhatsApp acceptance.

**Proved both converter and desktop relay with fresh live audio.** A warm 2.0s synthetic
direct WebSocket run produced 174,086 bytes / 1,813.4ms, six stats blocks, TensorRT
inference median/p95 55.64/58.08ms, no drops. A generated 2,208.9ms spoken sentence sent
through the exact local `/api/desktop/session` → `/api/desktop/audio` path returned 173,760
bytes in 181 valid 960-byte 48kHz frames (1,810.0ms), ready in 1,856.36ms, six stats
messages, zero input drops. The ignored local listening artifact is
`kiera_conversion_test_rerun.wav`. This proves transport, inference, and output framing;
subjective identity/clarity still requires the user's ear and real microphone.

**The reproducible failure is cold capacity/readiness, not conversion.** Modal first logged
the stable function waiting about five minutes for an `ap-southeast` L4, then spent ~34s
loading the model/TRT pipeline. The first direct WebSocket opening handshake timed out and
active readiness failed after 180s; `/health` returned zero bytes within 45s. A later cold
desktop session reached its 150s fail-closed limit and returned
`{"type":"error","code":"converter_unavailable"}`. An explicit `/health` warm-up then took
about 94s and returned `ready` on an NVIDIA L4; the identical desktop test passed
immediately afterward. Decision: preserve fail-closed behavior and treat a warm gate as a
first-class desktop UX/state problem. The next implementation must expose warming/progress
and make an explicit cost/availability choice among longer bounded wait/retry,
`RVC_KEEPWARM=1`, or the pending broader-AP routing experiment.

**Two adjacent tooling/logging defects were recorded rather than silently worked around.**
Running `scripts/rvc_stream_benchmark.py` directly fails to import `backend`; module form
(`python -m scripts.rvc_stream_benchmark`) works. Also, the server prints that the keep-warm
loop started even when `RVC_KEEPWARM` is default-off and the task exits immediately.
Post-merge `scripts/run_local.py` still sets local-auth launcher markers that the backend no
longer reads because operator auth was removed globally. These are backlog cleanups, not
reasons to change the verified audio pipeline.
