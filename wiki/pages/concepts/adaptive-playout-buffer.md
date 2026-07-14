---
title: Standing playout buffer
type: concept
sources: [subsystem-notes, decisions-log]
updated: 2026-07-14
---

> **Current live value: 0.25s target (commit `b38070c`, "restore 320ms block geometry,
> 80ms SOLA, 0.25s cushion"), ~5s cap.** History: ~3s (2026-07-03) → 1.25s (TRT phase 1,
> 2026-07-07) → 0.25s (TRT phase 2, 2026-07-07) → reverted/re-landed at 0.25s again by
> `b38070c` (date of that commit not confirmed from this page alone). This number has
> already been wrong once in this file — always verify `_PLAYOUT_BUFFER_TARGET_BYTES` in
> `backend/pipeline.py` directly before citing it elsewhere. Render auto-deploys on every
> push to `main`, so whatever's on `main` is live on the deployed backend.
>
> **Open bug against the current 0.25s design, found 2026-07-14:** see
> [[playout-buffer-gulp-drain-oscillation]] — the consumer's gulp-then-catch-up drain
> pattern lets the buffer overshoot its own 0.25s target by 6-7x during sustained speech,
> a plausible cause of a "voice gets blurred on long sentences" field report.

**This page describes the buffer's mechanism, which hasn't changed since 2026-07-03 —
only its target/cap constants have moved.** The pre-2026-07-02 adaptive buffer this page
used to document (`_run_playout`, P95-based adaptive sizing, `_REORDER_WAIT_S`
reorder-wait) was removed entirely in the 2026-07-02 streaming rebuild, replaced with a
one-shot 100ms jitter fill, which was itself replaced by the bounded standing buffer
described here on 2026-07-03. Five distinct designs in one buffer's history — see
[[buffering-history]] for the full migration path before assuming any of them is still
current.

**Why it exists (2026-07-03):** the 2026-07-02 rebuild's one-shot 100ms jitter buffer only
smoothed the *start* of a call — once drained, any converter block slower than its
real-time budget produced a silence gap (LiveKit's own `AudioSource` queue is only 200ms).
This was confirmed as the direct cause of "part by part" broken-up call audio — see
[[part-by-part-audio-investigation]]. Rather than re-chase real-time performance further,
the product decision was made that call latency isn't a priority for this app — voice
continuity is — so the fix trades delay for smoothness instead of trying to eliminate the
delay.

**Design**: `_run_conversion_stream` in
[backend/pipeline.py](../../../backend/pipeline.py) is a producer that only appends
converted audio to `self._playout_buffer` (a plain bounded `bytearray`, not a sequence-
numbered reorder structure — the underlying stream is already strictly ordered, there's
nothing to reorder). A separate `_run_playout_consumer` task drains it into
`_publish_frames` at a steady pace:
1. **Filling** — wait until at least `_PLAYOUT_BUFFER_TARGET_BYTES` (0.25s of 48kHz 16-bit
   mono as of `b38070c`; was ~3s from 2026-07-03, then 1.25s under TRT phase 1) has
   accumulated before the first publish.
2. **Draining** — after that, publish whatever has accumulated since the last publish,
   continuously; `_publish_frames`' own `capture_frame` backpressure already paces real
   playback correctly, so the consumer just has to never be starved by one slow block.

**Bounded, not adaptive**: unlike the old P95-based design, the target (0.25s) and cap
(~5s, `_PLAYOUT_BUFFER_MAX_BYTES`) are fixed constants, not recomputed per session. Beyond
the cap, the **oldest** buffered audio is dropped (same policy as
`RVCStreamingConverter`'s reconnect buffer, `backend/converters/rvc_stream.py`,
`_MAX_BUFFER_BYTES`) rather than growing delay unboundedly — if this ever triggers in
production, it means the sustained real-time factor is worse than the buffer's cushion can
absorb, which is a signal to look at inference speed (GPU tier, ONNX/TensorRT), not to
just raise the cap further.

**Known gotcha found writing tests for this**: `rtc.AudioFrame.data` is an int16-typed
`memoryview` — `len()` on it returns sample count, not byte count. Wrap it as
`bytes(frame.data)` first for a real byte length.

**Not yet manually verified**: this design has passed automated unit tests
(`backend/test_pipeline.py`) but has not yet had LATENCY.md's spectral latency test run
against it, nor final confirmation from a live call that "part by part" audio is actually
gone. See [[active-backlog]].

**Why the target could shrink**: the original ~3s target was sized to absorb a slow
non-TRT inference path; the TRT migration's benchmarks measured well under 70ms inference
per block on a live L4 ([[tensorrt-migration]]), which is why the buffer no longer needs
as deep a cushion. That headroom is real (confirmed again 2026-07-14: 56-64ms/block during
a live call), but a small *target* isn't the whole story for smoothness — see
[[playout-buffer-gulp-drain-oscillation]] for a drain-pattern bug that can still let the
buffer balloon well past 0.25s even when inference itself is fast.
