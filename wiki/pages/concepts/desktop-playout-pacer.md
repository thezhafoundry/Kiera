---
title: Desktop bridge playout pacer (VB-CABLE / WhatsApp path)
type: concept
sources: [decisions-log, active-backlog]
updated: 2026-08-02
---

> **Current live values (verify against `backend/desktop_audio.py` before citing
> elsewhere — this file has a documented history of going stale):**
> `PLAYOUT_CUSHION_BYTES` = 0.35s (as of commit `5029194`, **genuinely untested at that
> exact value** as of this writing), `PLAYOUT_MAX_BYTES` = 5s hard cap,
> `PLAYOUT_CATCHUP_THRESHOLD_BYTES` = 0.75s, `PLAYOUT_CATCHUP_TARGET_BYTES` = 0.4s,
> `PLAYOUT_CATCHUP_RATE` = 1.15x (added same day, commit `a9b2e64`).

This is the desktop voice-changer's own playout pacer
([backend/desktop_audio.py](../../../backend/desktop_audio.py)'s
`DesktopAudioBridge.run_playout_consumer`) — a **separate implementation** from the
LiveKit/PSTN path's buffer described in [[adaptive-playout-buffer]]
(`backend/pipeline.py`). They share the same design lineage and the same class of bugs
(see below), but are two distinct code paths serving two distinct products: this one
feeds a browser → VB-CABLE/BlackHole → a desktop calling app like WhatsApp, not a LiveKit
room.

## Why it exists

Same root cause as the LiveKit path: Modal's converter delivers audio in bursts, not a
steady trickle (measured 2026-07-27: 0.22s of audio, an ~8s stall, then 6.2s dumped inside
one second). Forwarded unpaced, that's audible as a fraction of a second of speech
followed by silence. The pacer exists to convert that burstiness into **delay**, which is
tolerable, rather than **corrupted timing**, which isn't.

## Mechanism

1. **Cushion fill** — hold the first `PLAYOUT_CUSHION_BYTES` of converted audio before
   writing anything, absorbing the converter's initial burst.
2. **Steady drain** — after that, write `PLAYOUT_DRAIN_BYTES` (0.1s) per pacing step,
   scheduled via a `next_publish_time` that advances by exactly one real-time chunk
   duration per write — i.e. strictly 1x playback speed.
3. **Dry-side self-correction** — if the buffer runs empty (converter genuinely fell
   behind), the next chunk publishes immediately and the schedule re-anchors from `now`,
   rather than trying to "catch up" on a stale schedule (which would push audio out
   faster than real time — exactly the bug this whole pacer exists to prevent).
4. **Backlog-side catch-up (added 2026-08-02, see
   [[desktop-pacer-permanent-backlog-bug]])** — the mechanism dry-side self-correction
   didn't have an equivalent for: once held backlog exceeds `PLAYOUT_CATCHUP_THRESHOLD_BYTES`,
   the pacer drains at `PLAYOUT_CATCHUP_RATE` (1.15x, not 1x) until back at/below
   `PLAYOUT_CATCHUP_TARGET_BYTES`, with hysteresis between the two thresholds to avoid
   flapping on/off every iteration. Deliberately mild — fast enough to actually recover
   within a few seconds, slow enough to stay under the audible "sped up" threshold that
   motivated strict 1x pacing in the first place.
5. **Hard cap** — beyond `PLAYOUT_MAX_BYTES` (5s), oldest audio is dropped rather than
   letting delay grow unboundedly.
6. **Teardown flush** — genuine backlog is the normal steady state under real-time
   pacing (the converter produces far faster than 1x), so on session end the remaining
   buffer is flushed unpaced rather than truncated, *except* when the converter stream is
   still actively producing and the client is still connected (mid-call cancellation) —
   dumping backlog unpaced there would be exactly the audible time-compression this pacer
   exists to prevent.

## Cushion sizing history (2026-08-02, all same day)

0.25s (caused a confirmed live breakup — buffer repeatedly hit zero between converter
bursts) → 0.5s (fixed cleanly, confirmed via Windows Sound Recorder, no breakup) → 0.05s
(untested experiment probing the latency floor) → **0.35s** (current, an explicitly
untested midpoint chosen to trade back some of 0.5s's smoothness margin for lower
latency). Each step is one commit; see `.agents/decisions/log.md`'s 2026-08-02 entries
for the full trail and reasoning at each step. **Do not assume 0.35s is validated** —
re-verify "Playout drops" stays 0 and "Playout buffer" doesn't hit zero on a real call
before trusting it.

## Live diagnostics

The desktop page's own UI (`frontend/desktop/index.html`) surfaces this pacer's state
directly: "Playout buffer" (live, current held bytes as ms), "Playout drops" (cumulative
oldest-dropped count from the 5s hard cap), and — added 2026-08-02 — a full "Live latency
breakdown" panel (network RTT, block-accumulation wait, per-stage GPU inference, playout
buffer) plus a "Last call summary" shown on Stop with session averages. See
[[desktop-latency-panel-total-omitted-accumulation]] for a bug in that panel's own math
found and fixed the same day it shipped.

## Known related bugs

- [[desktop-pacer-permanent-backlog-bug]] — strict-1x-only pacing had no way to recover
  from backlog once it existed; fixed 2026-08-02.
- [[playout-buffer-gulp-drain-oscillation]] — the LiveKit path's analogous but distinct
  bug (gulp-then-catch-up via a different mechanism); worth reading for contrast, not the
  same code or the same fix.
