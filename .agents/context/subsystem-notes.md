# Subsystem Notes & Load-Bearing Gotchas

<!-- One section per subsystem. Capture the WHY and the traps that are not obvious
     from reading the code — this is what the wiki/codebase cannot tell you. -->

## Streaming pipeline (`backend/pipeline.py`, streaming rebuild 2026-07-02)
The old VAD-chunked, semaphore'd, reorder-buffer design (`_conversion_consumer`,
`_run_playout`, adaptive standing playout buffer) was removed entirely, not just tuned —
`webrtcvad` is no longer imported anywhere in this file. The converter is now driven as one
long-lived duplex stream for the life of the worker's active pipeline
(`_run_conversion_stream`); frames arrive back in the order they were sent, so there's
nothing left to reorder. Load-bearing gotchas in the new design:
- **`contextlib.aclosing(gen)` around the conversion generator is not optional.**
  `_run_conversion_stream` wraps `converter.convert_stream(...)` in
  `async with contextlib.aclosing(gen):` specifically because a bare `async for chunk in gen`
  that exits via `break`, an exception, or task cancellation does **not** reliably call the
  generator's `aclose()` — and for `RVCStreamingConverter`, `aclose()` is what tears down the
  pump/connection background tasks and the WS socket (see `convert_stream`'s `finally:
  await self._teardown()` in `backend/converters/rvc_stream.py`). Skip `aclosing` and a
  cancelled/abandoned stream can leak a live WS connection and background tasks per call.
  This was a real cross-task requirement surfaced during the rebuild, not a hypothetical.
- **`is_ready` is a cached property backed by a one-shot background probe, deliberately not a
  live re-probe.** `VoiceConversionWorker.start_readiness_probe()` kicks off exactly one
  background task per worker that calls `wait_until_ready()` once and caches the result in
  `self._ready`; the `is_ready` property just reads that cached bool. This matters because
  Twilio polls `/api/call/wait` roughly every 3 seconds while a caller is on hold — if
  `is_ready` opened a fresh probe connection to the converter's backend on every poll instead
  of reading a cache, that's a new WS handshake every 3s for the whole hold duration, and
  worse, `RVCStreamingConverter.wait_ready()` is a *separate* short-lived probe connection
  from the long-lived session, so a synchronous re-probe would also contend with (or be
  confused for) the real per-call session under the 1-concurrent-session MVP limit.
- **SOLA crossfade seam-testing trap** (`modal_deploy/streaming.py::sola_crossfade`, found
  during Task 5 review): the Hann fade-in ramp's `fade_in[0] == 0` means the very first sample
  of any crossfaded seam is structurally continuous (equal to the tail's own last sample)
  **regardless of which offset the correlation search picks** — a naive test that only checks
  "the seam has no big jump" will pass even with a broken correlation search that always picks
  offset 0. `modal_deploy/test_streaming.py` instead checks full-block reconstruction accuracy
  against a known-continuous source, which actually exercises the search. Keep this in mind
  before writing a new SOLA-adjacent test — boundary continuity alone proves nothing about
  whether the alignment search works.

## Modal RVC GPU worker (`modal_deploy/worker.py`)
- Cold start is much slower than the code comments assume: measured live at ~75s with
  no `/health` response at all before `{"status":"ready"}` (see LATENCY.md §4.1), not the
  8-30s originally assumed. This is still true post-rebuild (same Modal worker, same
  cold-start problem) — the difference now is what a cold/unready GPU *does* to a call: the
  old per-chunk 2000ms conversion budget/raw-fallback machinery in `_do_start_bot` is gone
  entirely; instead the caller (`backend/main.py`) blocks the whole call behind the
  fail-closed warm gate (`worker.wait_until_ready` / `is_ready`, see "Streaming pipeline"
  above and [[stack-and-rules]]) until the GPU is actually ready.
- **1-concurrent-session MVP**: `/ws` enforces single-tenancy with module-level
  `_session_active`/`_session_lock` state (`modal_deploy/worker.py`) — a second WS connection
  gets `{"type":"busy"}` and is closed immediately, no queueing. This is a deliberate scope
  limit for this rebuild (multi-call concurrency is explicitly out of scope), not a bug to
  silently "fix" by adding a queue.
- **Two distinct kinds of readiness connection** — don't conflate them: `wait_ready(timeout)`
  (`RVCStreamingConverter`, `backend/converters/rvc_stream.py`) opens a short-lived standalone
  probe connection purely to confirm the server can hand back `{"type":"ready"}`, then closes;
  the actual call audio flows over a separate, later, long-lived `/ws` connection opened by
  `convert_stream`'s `_connection_loop`. Because the server is single-tenant, a probe and a
  real session can't both be open at once — `wait_ready` is meant to complete and close before
  the real session connects.
- `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's `logs/mi-test/*.index` is empty — looks like a hang from the caller's side but
  is a missing-model error, only visible in `modal app logs rvc-worker`.
- **Region mismatch (open as of 2026-07-02):** the Modal function is pinned to
  `region="ap-southeast"` on the assumption Render/Twilio are nearby, but the deployed
  Render service is actually in Oregon (us-west) — every call currently pays a
  transpacific round trip on top of inference. Fixing this (repin Modal to a US region,
  or move Render) is tracked in [[active-backlog]].

## Render deployment
- `autoDeploy: commit` means **every push to `main` redeploys immediately**, tearing down
  the LiveKit worker and any in-flight `VoiceConversionWorker` mid-call. This was
  confirmed live on 2026-07-02: two redeploys within ~4 minutes during an active test call
  produced symptoms indistinguishable from "Modal not connecting." When iterating on
  pipeline code, either avoid pushing during a live test call or expect to re-warm Modal
  (`POST /api/warmup`) after every deploy.

## Windows dev environment
- `webrtc-noise-gain` (used by `WebRTCNoiseSuppressor`) has no prebuilt Windows wheel and
  needs MSVC build tools; without them the import fails and the suppressor silently
  degrades to passthrough (logged as a warning). This failure mode is silent and tests
  still pass, so a Windows dev environment can look identical to a fully-working one while
  actually running degraded audio processing — check startup logs for the warning, don't
  assume from green tests.
- `webrtcvad` is no longer imported or used anywhere in `backend/pipeline.py` post-rebuild
  (VAD-based chunking was deleted, not just made optional) — the old "without it, chunking
  falls back to fixed max-length" note no longer applies to anything. It's effectively an
  unused dependency now regardless of platform.
