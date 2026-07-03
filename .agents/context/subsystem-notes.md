# Subsystem Notes & Load-Bearing Gotchas

<!-- One section per subsystem. Capture the WHY and the traps that are not obvious
     from reading the code — this is what the wiki/codebase cannot tell you. -->

## Streaming pipeline (`backend/pipeline.py`, streaming rebuild 2026-07-02, playout buffer reintroduced 2026-07-03)
The old VAD-chunked, semaphore'd, reorder-buffer design (`_conversion_consumer`,
`_run_playout`) was removed entirely in the 2026-07-02 rebuild, not just tuned —
`webrtcvad` is no longer imported anywhere in this file. The converter is still driven as
one long-lived duplex stream for the life of the worker's active pipeline
(`_run_conversion_stream`); frames arrive back **in order**, so there's nothing to reorder.
**However**, a standing playout buffer was reintroduced on 2026-07-03 (see the new
"Playout buffer" subsection below) — don't assume "no chunking/no buffering anywhere" from
the 2026-07-02 framing above; that only ever applied to the *input* side (no VAD chunking
of the agent's mic audio) and briefly to the *output* side too, until the 2026-07-03 product
decision that latency isn't a priority reversed that specific piece. Load-bearing gotchas in
the duplex-stream design itself (still current):
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

### Playout buffer (`backend/pipeline.py`, reintroduced 2026-07-03)
2026-07-03 product decision: call **latency is explicitly not a priority** for this app —
voice **continuity/quality** is. This directly reverses part of the 2026-07-02 rebuild's
implicit latency-first design, so don't assume the newer decision is a regression of the
older one; it's a deliberate tradeoff change, see [[log]]. Same decision also drove
`modal_deploy/streaming.py`'s `BLOCK_MS`/`CONTEXT_MS` going from 320/160 to 1000/400 — bigger
inference blocks give HuBERT/pitch tracking more context and cut the SOLA crossfade seam
rate (fixed 80ms crossfade per block, so ~3x fewer seams per second of audio at the new
size), at the cost of more per-block delay, which this buffer is what absorbs.
- `_run_conversion_stream`'s old one-shot 100ms jitter fill (`_JITTER_TARGET_BYTES`) only
  smoothed the *start* of a call — once drained, any converter block slower than its
  real-time budget produced a silence gap, because LiveKit's own `AudioSource` queue
  (`queue_size_ms=200`) is far too small to cover it. Confirmed in production: ~460-590ms to
  convert a 320ms block even after the FAISS caching fix below (a ~1.5-1.8x real-time
  factor), which reliably starved the old design.
- Replaced with a producer/consumer split: `_run_conversion_stream` only appends converted
  audio to `self._playout_buffer` (bounded, `_PLAYOUT_BUFFER_TARGET_BYTES` ~3s /
  `_PLAYOUT_BUFFER_MAX_BYTES` ~5s cap, drop-oldest on overflow — same policy as
  `RVCStreamingConverter`'s reconnect buffer, `backend/converters/rvc_stream.py`,
  `_MAX_BUFFER_BYTES`). A separate `_run_playout_consumer` task drains it into
  `_publish_frames` at a steady pace, decoupled from how bursty/delayed the converter's
  arrival timing actually is. A slow block now grows delay instead of producing "part by
  part" audio.
- **`rtc.AudioFrame.data` is an int16-typed `memoryview` — `len()` on it returns *sample*
  count, not byte count.** (`len(frame.data)` on a 960-byte/480-sample frame returns 480,
  not 960.) Wrap it as `bytes(frame.data)` first if you need the real byte length — this is
  exactly what `_run_audio_pipeline` already does before handing frames to the noise
  suppressor. Tripped up a first draft of the playout-buffer tests in
  `backend/test_pipeline.py` (a byte-count assertion silently checked half the real total).

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
- **Region mismatch: RESOLVED as of 2026-07-03 (was open 2026-07-02).** The Modal function
  is pinned `region="ap-southeast"`; the Render service is now confirmed live in
  **Singapore** (`srv-d932m4cvikkc73belt1g`, verified via Render API), colocated with Modal.
  The old "Render is in Oregon" claim was stale — the Render service ID also changed
  (from `srv-d92lh7navr4c738i03a0`), consistent with a migration having happened. Don't
  re-open this in [[active-backlog]] without re-checking Render's current region first.
- **FAISS index re-read from disk on every conversion call — fixed 2026-07-03.**
  `RVC/infer/modules/vc/pipeline.py` (vendored, not Keira's own code) calls
  `faiss.read_index(file_index)` then `index.reconstruct_n(0, index.ntotal)`
  **unconditionally on every `vc_single()`/`pipeline()` call** — fine for the original
  WebUI's one-shot-per-file use case, catastrophic for streaming, where it was running once
  per ~480ms audio block. Confirmed in production `[Timing]` logs: ~1.4-2.0s of the ~3.0-3.8s
  total per-block conversion time was this alone (a 221MB index file). Fixed *without*
  touching the vendored file: `worker.py` monkeypatches `faiss.read_index` at container
  startup with an `lru_cache`-backed wrapper that also caches the `reconstruct_n` result
  (overrides the returned index object's `reconstruct_n` method to return the precomputed
  array). The existing GPU warm-up pass in `RVCEngine.startup()` naturally primes the cache
  before any real caller connects. Post-fix: `npy` time dropped to ~0.05s, total
  `run_conversion` to ~0.46-0.59s per block. If you ever see the old ~1.4-2.0s `npy` number
  again, suspect the monkeypatch didn't survive a `worker.py` refactor, not a new bottleneck.
- **`max_containers` was never set on `fastapi_app` — fixed 2026-07-03.** The in-process
  `_session_active`/`_session_lock` single-tenancy gate (above) only enforces "1 session"
  *inside a single already-running container* — it does nothing to stop Modal's autoscaler
  from booting an additional (paid) GPU container for a connection attempt that arrives
  while an existing container is still cold-starting (~75s) or mid-call. Confirmed live: 4
  simultaneous `rvc-worker` containers were running at once in the Modal dashboard, each a
  full GPU replica (~1.7-1.8GB loaded model each) — traced to WS reconnects/retries during
  active test-calling landing on different containers rather than queuing against one. Fixed
  by adding `max_containers=1` to the `@app.function(...)` decorator on `fastapi_app`
  (`worker.py`) so extra connection attempts queue instead of spinning up parallel GPUs.
- **Gender/pitch auto-detection is unreliable — reverted 2026-07-03.** `_auto_detect_pitch`'s
  autocorrelation F0 estimate (male/female boundary at 145Hz) was intended to replace the
  manual UI gender toggle (`a0f3c42`, "more accurate" at the time) but confirmed misdetecting
  a known-male agent as female **twice** in production logs the same day (F0=222Hz/166Hz,
  both classified Female, pitch_shift=0 applied instead of the correct +12) — feeding audio
  outside the trained model's pitch range produces a "wrong identity" sounding voice, not
  just a pitch error. It's also re-run from scratch on every WS reconnect (the detected
  pitch is never reported back to/persisted by the client), so a single call could even
  change identity mid-call. `backend/main.py`'s `_do_start_bot` now drives `pitch_shift`
  from the UI's `agentGender` toggle again (`12 if male else 0`) instead of `-1` (GPU
  auto-detect). The GPU-side `_auto_detect_pitch` code itself is untouched/still selectable
  via `pitch=-1` — just no longer what the live call path uses.

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
