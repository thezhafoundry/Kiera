---
title: Playout buffer overshoots target and gulp-drains during long continuous speech
type: issue
status: open
sources: [subsystem-notes, active-backlog]
updated: 2026-07-14
---

Found while investigating a 2026-07-14 field-call complaint: "if I talk a big sentence
the voice is getting blurred at the last." Distinct from and apparently unrelated to
[[adaptive-pitch-lock-rollout]], which shipped the same week — GPU inference stayed fast
throughout the call in question (56-64ms per 320ms block), ruling out slow inference.

## Evidence

`backend/pipeline.py`'s per-block `[Worker][LatencySummary]` telemetry from a 22:23 IST
call showed the playout buffer (`_playout_buffer`, see [[adaptive-playout-buffer]])
repeatedly overshooting its 0.25s (24000-byte) target by 6-7x before dropping back near
zero — three separate times within one single unbroken 7-second voiced utterance:
```
30720 → 60774 → 90570 → 121196 → 121196 → 181486 → [flush]
30296 → 60718 → 60718 → 121864 → 152122 → 152122 → [flush]
60306 → 90876 → 90876 → 121286 → 151660 →           [flush]
```
121-182KB is 1.3-1.9 seconds of buffered audio sitting well past a 0.25s target.

## Hypothesis

`_run_playout_consumer` doesn't drain steadily — after its initial fill, every wake grabs
the *entire* current buffer in one `_publish_frames` call and relies on LiveKit's
`capture_frame` backpressure (an internal queue only ~200ms deep) to pace that lump back
out in real time. Because the queue has room, the first ~200ms of a large gulp is likely
accepted faster than real-time before pacing engages — a small burst of time-compressed
audio each cycle — while the buffer quietly refills underneath for the next one. Repeating
every ~1.3-1.9s during one long sentence would plausibly read as progressive "blur" rather
than a hard dropout, matching the report, but this hasn't been confirmed by ear against
this specific mechanism yet.

## Status

Not yet fixed. Two candidate directions, neither chosen: drain the buffer in small steady
increments instead of one gulp per wake, or re-tune the target/consumer pacing so a
backlog this size can't accumulate in the first place. See [[active-backlog]].
