---
title: Adaptive per-call pitch lock replaces the fixed shift constant
type: issue
status: open
sources: [decisions-log, subsystem-notes, active-backlog]
updated: 2026-07-16
---

Follow-on to [[voice-identity-mismatch-investigation]]'s pitch-overshoot fix. That fix
(`RVC_MALE_PITCH_SHIFT=7`, 2026-07-08) was a single constant calibrated against one agent's
~137Hz voice at one point in time — and on 2026-07-13 it went stale: the same agent's live
F0 had drifted to 152-158Hz, which the fixed `+7` shift pushed 1.5-2 semitones above the
mi-test model's ~208Hz trained center, reproducing the original wrong-identity symptom.

## Design

Spec: `docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md`. Instead of a
constant, the Modal worker's `/ws` session now measures the agent's own voiced F0 in
real time (RMVPE on the TRT path, `PMF0Predictor.compute_f0_uv` masked to voiced-only on
the ONNX fallback) and, once at least 2 seconds of voiced speech has been seen, locks a
shift — `12·log2(target_f0/median_f0)`, float, clamped ±12 — for the rest of that call.
The gender-toggle constant becomes only the *prior* used before the lock engages. The
locked value rides the existing per-block stats message back to the backend, which adopts
it, so a WebSocket reconnect mid-call resumes the locked identity instead of re-measuring
— this is the specific design choice that avoids the failure mode that got the *previous*
attempt at automatic pitch detection (GPU auto-detect, `pitch_shift=-1`) reverted on
2026-07-03: that one re-ran its (unreliable) detection on every reconnect and could
audibly flip identity mid-call.

`RVC_ADAPTIVE_PITCH=0` (Render env) reverts to the exact legacy fixed-shift behavior
without touching the Modal worker.

## Field verification (2026-07-14)

Deployed and confirmed working via `modal app logs rvc-worker` on two live calls:
```
[AdaptivePitch] locked shift=+3.33 st (median F0=171.6Hz → target 208Hz, 2.0s voiced, prior +7.0)
[AdaptivePitch] locked shift=+5.67 st (median F0=149.9Hz → target 208Hz, 2.1s voiced, prior +7.0)
```
Both are exact against the formula (`12·log2(208/171.6)=3.33`, `12·log2(208/149.9)=5.67`),
confirming the mechanism works as designed.

## Open follow-ups (why this isn't fully closed)

1. **The abrupt prior→locked jump has been replaced by a one-second interpolation** in
   Modal v11 and is covered by pure pitch-lock tests. This reduces the discontinuity without
   continuously chasing the speaker's pitch, but it still needs one live staff listen test;
   unit tests cannot prove perceptual naturalness.
2. **`RVC_TARGET_F0=208`** is a single 2026-07-08 reference-output measurement, not
   re-derived from the model's actual training data. If identity complaints persist once
   the separate input-muffling regression (see below) is ruled out, this value itself is
   worth re-validating.
3. **A likely-unrelated input-clarity regression was found the same session**: the last
   three field calls (07-13 x2, 07-14) measured input spectral centroid back down to
   250-360Hz, worse than the original pre-fix baseline this project already solved once
   (see [[voice-identity-mismatch-investigation]]'s reopened section) — probably a stale
   cached `app.js` on the agent's browser, not a pitch-lock issue at all, but it stacks
   with (1) and (2) to make "does this sound like the trained voice" hard to judge in
   isolation until it's ruled out.
