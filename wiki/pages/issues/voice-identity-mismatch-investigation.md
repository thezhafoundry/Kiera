---
title: Converted voice doesn't match the trained voice (live calls only) — ongoing
type: issue
status: open
sources: [decisions-log, subsystem-notes, active-backlog]
updated: 2026-07-03
---

Distinct from [[part-by-part-audio-investigation]] (choppiness/latency) and
[[sip-audio-mixing-isolation-bug]] (raw+converted mixing, now resolved) — this is about
**timbre/identity**: on live calls, the converted voice doesn't sound like the trained
"Keira" voice, even with those two other issues fixed. First reported as "sounds like a
generic/different voice" — clean audio, no glitches, just not identity-matched.

## The key clue: offline sounds right, live doesn't

The user's own offline test (`modal run modal_deploy/worker.py::main`, single continuous
pass over a test WAV, no chunking) reliably produces the correct trained-voice sound. The
live `/ws` streaming path, on the same kind of content, does not. Every hypothesis below was
tested by trying to reproduce the *live* symptom *offline*, where it can be isolated from
network/SIP/telephony noise entirely.

## Ruled out, in order, each with real evidence

1. **Pitch/gender mismatch** — already root-caused and fixed earlier the same day (see
   [[part-by-part-audio-investigation]] finding 2): GPU auto-detect misclassified a known-male
   agent as female twice in production. Reverted to the manual UI toggle. Confirmed via logs
   that live calls now consistently show `pitch_shift=12` — correct — yet the symptom
   persisted after this fix, so it's necessarily something else too.
2. **`index_rate` too low (0.75)** — hypothesis: not enough FAISS-retrieved target timbre vs.
   raw HuBERT content features. Bumped to `0.9` via a new `RVC_INDEX_RATE` env var
   (`backend/main.py`). **Ruled out empirically**: confirmed via Render/Modal logs that
   `index_rate=0.9` was genuinely applied on a real call (`pitch=12 index_rate=0.9` in
   `[Timing]` logs) — the voice still didn't match. Also ruled out on evidence grounds before
   even testing: the offline reference uses the *same* 0.75 default and sounds correct, so a
   too-low value can't be the whole story.
3. **Chunked-streaming architecture (block accumulate + independent per-block RVC + SOLA
   crossfade) vs. the offline single continuous pass** — the most structurally plausible
   suspect, since HuBERT/the generator have no persistent state across independent block
   calls. Built a new diagnostic, `convert_file_chunked`/`main_chunked` in
   `modal_deploy/worker.py`: replays the *exact* block-accumulate + per-block-RVC +
   trim-context + SOLA-crossfade logic `ws_stream` uses, but driven by a static WAV file via
   `modal run` instead of a live WebSocket — same reference file, same pitch, no network/SIP
   involved. **Ruled out**: the chunked output sounded correct, matching the offline
   single-pass reference. This is a real, reusable diagnostic tool now, not a one-off script —
   see "New diagnostic tooling" below.
4. **Noise suppression (`WebRTCNoiseSuppressor`, Level 3)** — the live path runs every 10ms
   frame through this before it ever reaches the converter; confirmed via Render logs
   (`[Noise Suppression] Initialized WebRTC Noise Suppressor (Level 3)`, no passthrough
   warning) that it's genuinely active in production, not degraded. The offline diagnostic
   skipped it entirely. Added `webrtc-noise-gain` to the Modal image and wired the identical
   Level-3 processing into `convert_file_chunked`. **Ruled out**: still sounded correct with
   denoising applied.
5. **Raw input audio quality itself** (room noise, mic quality, browser WebRTC processing) —
   added temporary instrumentation (`_DEBUG_SAVE_RAW_AUDIO` in `worker.py`) to save the first
   30s of actual pre-conversion PCM from a real call to the Modal volume
   (`/root/rvc-models/debug/`), downloaded and both measured objectively and listened to
   directly. Measured: correct 16kHz mono format, normal RMS levels, peak ~93% of full scale
   (hot, close to clipping, worth watching but not proven causal), and a very clean noise
   floor during pauses (mean RMS 7.5/32768) — no obvious room noise/hum contamination.
   User's own listen: "clean but there is 5 percent of noise." **Effectively ruled out** as a
   dramatic explanation — nothing here looks like it would produce a full identity swap.

## In progress: real call audio through the offline pipeline

Extended `main_chunked` with configurable `--input-file`/`--output-file` so the *exact* audio
captured from a real call (`raw_call_audio.wav`, not a generic test clip) could be run through
the same known-correct offline pipeline (chunking + SOLA + noise suppression). First attempt
used the script's `pitch=-1` default (whole-file auto-detect) and produced an unshifted,
recognizably-male result — but this is very likely **a self-inflicted artifact of the
diagnostic, not a new finding**: `_auto_detect_pitch` only examines the first 1 second of
audio with no silence check, and the captured call was ~75% near-silence overall (per the raw
capture's own analysis above) — if the file opens with silence, autocorrelation on near-zero
signal produces an effectively random F0 estimate. Re-running with the explicit
`pitch=12` (what production actually uses, never `-1`) was the next step at the point this
page was last updated — **result not yet confirmed**.

## New diagnostic tooling (reusable going forward)

`modal_deploy/worker.py` gained two lasting additions during this investigation, not just
throwaway debug code:
- **`convert_file_chunked` / `main_chunked`** — replays the live `/ws` handler's exact
  block-accumulate + SOLA-crossfade + (optional) noise-suppression pipeline against any WAV
  file via `modal run modal_deploy/worker.py::main_chunked --input-file <path> --pitch <n>`,
  with no network/SIP/telephony involved. Use this for *any* future "does the live pipeline's
  audio processing itself produce X" question before touching production.
- **`_DEBUG_SAVE_RAW_AUDIO`** (currently `True` in the deployed worker) — saves the first 30s
  of real pre-conversion PCM per live call to the Modal volume. **Must be flipped back to
  `False` and redeployed once this investigation concludes** — not meant to run indefinitely
  (storage, and it's per-call overhead). See [[active-backlog]].

## Side finding: GPU tier was silently stale

While investigating, found `fastapi_app`'s `gpu="L4"` (changed from `T4` at some earlier
point, committed) had never actually been deployed — `modal deploy` and `git push` are
separate actions, and only the git side had happened. Confirmed via `/health` that the
*live* container was still running a Tesla T4 despite the code saying L4. A later deploy (for
the debug-audio capture above) incidentally activated the pending L4 change — the live worker
is now genuinely on an L4. This means all the diagnostic comparisons above happened on
matched T4-vs-T4 hardware (the diagnostics explicitly pin `gpu="T4"` to match what was live at
the time), but *future* live-call testing now runs on different hardware than the diagnostics
do unless that's also updated.

## Status

Open. Five hypotheses ruled out with real evidence; the pipeline itself (as far as it's been
possible to isolate and test offline) appears correct. Immediate next step: confirm whether
the real call's captured audio, converted offline with the *correct* explicit pitch, still
sounds right — if it does, that's five-for-five on "the pipeline is fine," and the remaining
suspects narrow to things only a truly live session exercises (WS transport specifics, session
timing/concurrency) rather than anything about audio content or processing.
