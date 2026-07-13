# Adaptive Per-Call Pitch Lock — Design

**Date:** 2026-07-13
**Status:** Approved design, pending implementation plan
**Replaces:** fixed `RVC_MALE_PITCH_SHIFT` constant as the sole pitch mechanism (constant is retained as the pre-lock prior and fallback).

## Problem

RVC identity depends on the input landing in the model's trained pitch range (mi-test
centers ~208 Hz). The pitch shift is currently a fixed per-gender constant
(`RVC_MALE_PITCH_SHIFT`, live value +7) calibrated against a measured agent F0 of
~137–138 Hz. On 2026-07-13, two live calls measured agent F0 at 152–158 Hz — the same
agent, a different day's delivery — so the fixed +7 landed the converted output
1.5–2 semitones above the model center, producing the documented wrong-identity voice
(calls `call_20260713-154945`, `call_20260713-180042`; baseline comparison
`call_20260711-160847` at 138.5 Hz). Any fixed constant silently goes stale whenever
the agent's natural delivery shifts.

The previous attempt at adaptivity (GPU `_auto_detect_pitch`, reverted 2026-07-03)
failed for reasons this design avoids by construction: it ran a one-shot
autocorrelation on 1 second of possibly-silent audio, collapsed the result to a binary
12-or-0 at a 145 Hz threshold, and re-ran from scratch on every WS reconnect (mid-call
identity flips).

## Decisions (made 2026-07-13 with user)

1. **Lock once per call.** Adapt at the start of each call, then freeze — no mid-call
   drift. Each call re-adapts to that day's delivery.
2. **All agents.** The gender toggle remains the pre-lock prior (+7 male / 0 female);
   after lock everyone gets the F0-derived shift. A female agent already near 208 Hz
   naturally computes ~0.
3. **GPU-side placement.** The worker aggregates the F0 its engines already compute;
   no new estimator, no new protocol round-trips.

## Behavior

- A call starts with the prior shift from the config handshake (unchanged from today).
- As blocks are converted, the session collects the engine's **pre-shift voiced-frame
  F0 values** (RMVPE on the TRT path, PM on the ONNX fallback — both already computed
  per block).
- When ≥ 2 s of voiced frames has accumulated (≥ 200 frames at 10 ms hop), the session
  locks:

  `shift = clamp(12 · log2(target_f0 / median(voiced_f0)), −12, +12)` (float semitones)

  and applies it to every subsequent block for the rest of the session. One step
  change, early in the call, then frozen.
- Example (2026-07-13 call 2): median F0 152.4 Hz → lock ≈ +5.4 instead of the fixed
  +7; output median lands ~208 Hz instead of 226 Hz.

## Protocol & interface changes

- **Config payload** (`RVCStreamingConverter._config_payload`, `backend/converters/rvc_stream.py`)
  gains `"adaptive_pitch": bool` and `"target_f0": float`. Existing fields unchanged.
- **Stats payload** (worker → client, already sent per block) gains `"locked_pitch": float`
  once the lock has occurred. Additive; existing consumers unaffected.
- **Reconnect resume:** `RVCStreamingConverter` caches `locked_pitch` from stats. On WS
  reconnect it sends `"pitch_shift": <locked>, "adaptive_pitch": false` — a reconnected
  session resumes the locked identity and never re-detects. This structurally removes
  the 2026-07-03 reconnect-re-detection failure mode.
- **Engine seam:** the streaming block-conversion dispatcher in `worker.py` (the method
  `ws_stream` calls per block, which routes to `trt_pipeline.py::convert_block` on TRT or
  the base ONNX path) additionally returns the block's pre-shift voiced F0 values
  (possibly empty array). How each engine exposes them internally is an implementation
  detail; the HTTP `/convert` path (`run_conversion`'s public signature) is unchanged.
  The `ws_stream` session loop owns the aggregator and lock state, next to the existing
  `session_pitch` (policy lives in the session, engines only measure).
- `pitch_shift` becomes float end-to-end (both engines already apply it as
  `2 ** (pitch / 12)`, so this is a type loosening, not a math change). Fractional
  semitones center exactly instead of rounding up to ~3 % off.

## Configuration

| Env (backend) | Default | Meaning |
|---|---|---|
| `RVC_TARGET_F0` | `208` | Model's trained F0 center, sent as `target_f0` in config. |
| `RVC_ADAPTIVE_PITCH` | `1` | `0` disables adaptation entirely → exact current fixed-constant behavior. |
| `RVC_MALE_PITCH_SHIFT` | `12` (live: 7) | Unchanged: the pre-lock prior for male agents and the fallback if lock never occurs. |

## Guards (why this can't repeat the 2026-07-03 failure)

- Only voiced frames count: `pitchf > 0`, additionally clamped to a 60–400 Hz
  plausibility window. Silence contributes nothing — a call opening with silence locks
  later, never on garbage.
- Median, not mean; ≥ 2 s of voiced material required before any decision.
- Computed shift clamped to ±12 semitones.
- If a session never reaches 2 s of voiced speech, the prior stays in effect for the
  whole call — the failure mode is "same as today", never worse.
- Kill switch env restores current behavior with zero code paths changed.
- Lock event is logged on the worker (`median F0, voiced seconds, resulting shift`)
  and surfaced via stats for post-call forensics (Render/Modal logs, debug WAVs).

## Non-goals

- Continuous mid-call tracking (explicitly rejected — audible identity drift).
- Cross-call persistence of the locked value (each call re-locks fresh).
- Multi-agent / concurrent-session support (out of scope for the MVP worker).
- Changing the silence gate, SOLA, block geometry, or anything else in the audio path.

## Testing & verification

1. **Unit tests** for the aggregator: lock timing (frame counting), median math,
   voiced-only filtering, plausibility/shift clamps, never-locks fallback. Alongside
   `modal_deploy/test_streaming.py` (run from repo root).
2. **Offline A/B** via `main_chunked` (extended to exercise the adaptive session
   logic): replay `call_20260713-154945_in16k.wav` and `call_20260713-180042_in16k.wav`;
   output median F0 must land ≈ 208 Hz (vs 233/226 Hz live tonight).
3. **Field verification:** one live call after deploy; check the debug WAV's output F0
   and the lock log line.

## Rollout notes

- Requires `modal deploy` to take effect on the live worker (committing/pushing does
  not — see subsystem notes). Deploy is user-authorized, not automatic.
- The TRT migration's own listen gates (C4 A/B WAVs, C5 listen test) are still
  pending; the offline A/B in this work doubles as evidence for both and should be run
  against the TRT path specifically.
