---
title: 28 input frames dropped on a desktop call — root cause not yet confirmed
type: issue
status: open
sources: [decisions-log, active-backlog]
updated: 2026-08-02
---

Found 2026-08-02 while following up on a "still ~2 seconds latency" report after
[[desktop-pacer-permanent-backlog-bug]] had already shipped. The steady-from-start (not
recovering-then-plateauing) pattern of that 2s delay suggested the catch-up mechanism
likely never triggered — a different symptom than the earlier 4s bug, not evidence the
catch-up fix failed. Live worker was confirmed on `baseline` (720ms accumulation) via
`/health` at the time, and the panel's own "Last call summary" showed ~879ms average
total, roughly matching the panel's math — not ~2000ms, meaning the felt delay wasn't
fully explained by what the stats were capturing.

**The real finding on the same screenshot: `Input drops: 28` over a 1:49 call.** This is
upstream of every playout-side fix made so far — frames dropped here
(`backend/desktop_audio.py::receive_input`'s `input_queue.full()` path) never reach the
converter at all. This is lost speech, not just added delay.

## Mechanism traced (not yet root-caused to a specific stall)

`input_queue` (500ms/25-frame cap) only overflows if its consumer
(`RVCStreamingConverter._pump_input`, which drains near-instantly into its own separate
500ms buffer) falls behind — which per code inspection shouldn't happen under normal
operation. That points instead at something stalling `receive_input`'s own loop *between*
successive `websocket.receive()` calls — an event-loop stall, not a downstream
backpressure problem.

Two real, unthreaded/blocking code issues were found during the investigation (both
genuine code-quality problems independent of whether either is the actual cause):

1. **`WebRTCNoiseSuppressor.process_frame`** (`backend/noise/noise_suppressor.py`) makes a
   synchronous native C-extension call with **no `asyncio.to_thread` offload** — runs
   directly on the event loop, called twice per 20ms input frame on the desktop path.
   Confirmed genuinely active on Render (not a Windows-style silent bypass — the
   `webrtc-noise-gain` wheel installs and loads there per deploy logs). This is a direct
   violation of this project's own stated rule ("never block the event loop," CLAUDE.md).
2. **`DESKTOP_INPUT_GAIN` defaults to 3.0, not 1.0** (`backend/main.py`) — the pure-Python
   per-sample gain loop in `receive_input` is confirmed active on every production frame,
   not a dormant code path. Benchmarked locally at ~0.1ms/frame — negligible in isolation,
   likely not the primary cause alone, but adds real cost on top of whatever else runs per
   frame.

Supporting structural evidence, not proof: fresh Render deploy logs show
`Setting WEB_CONCURRENCY=1 by default, based on available CPUs in the instance` — a
single-worker, CPU-constrained free-tier instance has no spare capacity to absorb even a
small blocking stall without it affecting other work (including reading the next
WebSocket frame).

## Status: diagnosing, not yet fixed

Rather than fix on a plausible-but-unconfirmed theory, temporary timing instrumentation
was shipped instead (commit `6a98143`, marked for removal once resolved):
`receive_input` now logs a warning when the gap between successive loop iterations
exceeds 40ms (expected ~20ms — a direct, cause-agnostic event-loop-stall signal) or when
gain+suppressor processing for one frame exceeds 15ms (isolates whether it's specifically
these two calls). **Next step: capture Render logs from a live test call and read the
`[Desktop][Diag]` lines** — not yet done as of this writing. See
`.agents/projects/active-backlog.md` for the live tracking entry.
