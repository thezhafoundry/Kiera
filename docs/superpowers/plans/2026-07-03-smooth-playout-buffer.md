# Smooth Playout Buffer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the remaining "part by part" choppiness in converted call audio by trading latency (explicitly not a priority per product decision on 2026-07-03) for continuity: bigger, higher-quality inference blocks, and a real standing playout buffer instead of the current one-shot 100ms fill.

**Architecture:** Two independent, additive changes. (1) `modal_deploy/streaming.py`'s `BLOCK_MS`/`CONTEXT_MS` control how much audio the RVC model sees per inference call — larger blocks mean fewer SOLA crossfade seams per second of audio and more stable HuBERT/pitch context, at the cost of more delay per block (acceptable now). (2) `backend/pipeline.py`'s `_run_conversion_stream` currently publishes converted audio to LiveKit in the same loop that receives it from the converter, with only a one-time 100ms fill before playout starts — once that fill drains, any gap in converter output becomes a gap in what the lead hears, because LiveKit's own `AudioSource` queue (`queue_size_ms=200`) is far too small to cover a block that takes longer than its real-time budget (confirmed in production: ~460-590ms per 320ms block even after the FAISS caching fix in the prior plan). This plan replaces that with a producer/consumer split: the `async for` loop from the converter only appends to a bounded, standing buffer; a separate task drains that buffer into `_publish_frames` continuously, so bursty/delayed converter output turns into growing delay, not silence.

**Tech Stack:** Python 3.10 `asyncio` (Lock, Event, Task), no new dependencies.

## Global Constraints

- Do not touch vendored `RVC/` code — this plan only touches `modal_deploy/streaming.py` and `backend/pipeline.py`.
- Buffer cap: target ~3s initial cushion, ~5s hard cap before drop-oldest kicks in (per 2026-07-03 product decision: "a few seconds is fine"). Mirror the drop-oldest-on-overflow policy already used by `RVCStreamingConverter`'s reconnect buffer (`backend/converters/rvc_stream.py`, `_MAX_BUFFER_BYTES`) rather than inventing a new overflow policy.
- Existing tests (`modal_deploy/test_streaming.py`, `backend/test_pipeline.py`) must keep passing unmodified except where a task explicitly adds to them.
- `backend/test_pipeline.py` has no pytest — it's a plain script of `async def test_*()` functions called in sequence from `main()` at the bottom, run via `python -m backend.test_pipeline`. New tests must follow that exact style (see `test_rvc_streaming_converter_buffer_cap_drop_oldest` for the established white-box pattern: drive real async behavior, then assert on internal buffer state).

---

### Task 1: Larger inference blocks for quality

**Files:**
- Modify: `modal_deploy/streaming.py:23-24`

**Interfaces:**
- Produces: `BLOCK_MS = 1000`, `CONTEXT_MS = 400` (previously `320`/`160`). All derived constants (`BLOCK_SAMPLES_IN`, `CONTEXT_SAMPLES_IN`) are computed from these, so nothing else needs to change.

- [ ] **Step 1: Confirm no other file hardcodes the old values**

Run: `grep -rn "BLOCK_MS\|CONTEXT_MS\|BLOCK_SAMPLES_IN\|CONTEXT_SAMPLES_IN" modal_deploy/ backend/ --include=*.py`
Expected: only `modal_deploy/streaming.py` itself (definitions) — no hits in `worker.py`, `test_streaming.py`, or anywhere in `backend/`. (Already verified during planning; re-run to be sure nothing changed since.)

- [ ] **Step 2: Change the constants**

In `modal_deploy/streaming.py`, replace lines 23-24:

```python
BLOCK_MS = 320                  # NEW audio processed per inference block
CONTEXT_MS = 160                # prior input prepended as left context
```

with:

```python
# Latency is not a product priority for this app (2026-07-03 decision) --
# voice QUALITY is. Bigger blocks give HuBERT/pitch tracking more context per
# inference call and cut the SOLA crossfade seam rate (crossfade is a fixed
# 80ms per block, so a 1000ms block has ~3x fewer seams per second of audio
# than the old 320ms one). The added per-block delay is absorbed by the
# playout buffer in backend/pipeline.py, not exposed to the lead as latency
# they can't tolerate -- it's exposed as buffered delay instead.
BLOCK_MS = 1000                 # NEW audio processed per inference block
CONTEXT_MS = 400                # prior input prepended as left context
```

- [ ] **Step 3: Run the existing streaming unit tests**

Run: `cd modal_deploy && python -m pytest test_streaming.py -v`
Expected: all tests PASS unchanged (they test `sola_crossfade`, `trim_context`, `BlockAccumulator` generically, not tied to the specific `BLOCK_MS`/`CONTEXT_MS` values).

- [ ] **Step 4: Commit**

```bash
git add modal_deploy/streaming.py
git commit -m "Increase RVC streaming block size for quality (latency is not a priority)"
```

---

### Task 2: Standing playout buffer (replace one-shot jitter fill)

**Files:**
- Modify: `backend/pipeline.py:31-47` (class constants), `backend/pipeline.py:154-160` (stale comment), `backend/pipeline.py:409-462` (`_run_conversion_stream`)
- Test: `backend/test_pipeline.py` (append a new test function + register it in `main()`)

**Interfaces:**
- Consumes: `VoiceConversionWorker.converter` (any object with an async `convert_stream(in_audio) -> AsyncIterator[bytes]`, per `backend/converters/base.py`), `VoiceConversionWorker._publish_frames(bytes) -> None` (unchanged signature, already exists at `pipeline.py:463`).
- Produces: `VoiceConversionWorker._PLAYOUT_BUFFER_TARGET_BYTES` (class attr, int), `VoiceConversionWorker._PLAYOUT_BUFFER_MAX_BYTES` (class attr, int), `VoiceConversionWorker._run_playout_consumer()` (new method, no args, drains `self._playout_buffer`). Both new class attributes can be overridden per-instance (e.g. `worker._PLAYOUT_BUFFER_TARGET_BYTES = 640` in tests) exactly like the existing `_JITTER_TARGET_BYTES` pattern they replace.

- [ ] **Step 1: Write the failing test**

Append to `backend/test_pipeline.py` (needs `VoiceConversionWorker` and `WebRTCNoiseSuppressor`, both already imported at the top of this file for `test_worker_readiness_probe_dedup`):

```python
async def test_playout_buffer_smooths_bursty_converter_output():
    print("\n--- Testing standing playout buffer (absorbs bursty/delayed converter output) ---")

    class _FakeAudioSource:
        """Stands in for rtc.AudioSource: records every published frame's byte
        length without needing a real LiveKit connection."""
        def __init__(self):
            self.published_frame_sizes = []

        async def capture_frame(self, frame):
            self.published_frame_sizes.append(len(frame.data))

    class _BurstyConverter:
        """Yields audio in an intentionally bursty pattern: a big delayed chunk
        after a gap, then several small on-time chunks -- shaped like the real
        GPU-behind-real-time symptom this buffer exists to absorb. Every chunk
        length is a multiple of 960 bytes (_publish_frames' frame size) so
        published totals are exact -- _publish_frames zero-pads a trailing
        partial frame, which would make byte-count assertions fuzzy otherwise."""
        async def convert_stream(self, in_audio):
            # One big "late block" chunk (simulates a slow GPU block arriving
            # all at once) -- bigger than the test's target cushion below.
            yield b"\x00\x01" * 2400  # 4800 bytes
            await asyncio.sleep(0.01)
            for _ in range(5):
                yield b"\x00\x01" * 480  # 960 bytes each
                await asyncio.sleep(0.01)

    converter = _BurstyConverter()
    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=converter,
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    worker.audio_source = _FakeAudioSource()
    # Small test-scale cushion/cap so the test runs fast and deterministically
    # -- same override pattern the buffer-cap test above uses on
    # RVCStreamingConverter's _MAX_BUFFER_BYTES, applied here to the new
    # playout buffer's class constants instead.
    worker._PLAYOUT_BUFFER_TARGET_BYTES = 3000
    worker._PLAYOUT_BUFFER_MAX_BYTES = 8000

    conversion_task = asyncio.create_task(worker._run_conversion_stream())
    try:
        deadline = time.monotonic() + 3.0
        total_expected = 4800 + 5 * 960
        while (sum(worker.audio_source.published_frame_sizes) < total_expected
               and time.monotonic() < deadline):
            await asyncio.sleep(0.02)

        published_total = sum(worker.audio_source.published_frame_sizes)
        assert published_total == total_expected, (
            f"expected all {total_expected} converted bytes to eventually reach "
            f"capture_frame (buffer must never silently drop data below its cap), "
            f"got {published_total}"
        )
        print(f"All {total_expected} bytes from a bursty converter reached capture_frame: OK")

        # The first publish must not happen until the target cushion has
        # accumulated -- proves this isn't just re-publishing each chunk
        # immediately as it arrives (that would be the old one-shot-only
        # behavior, not a standing buffer).
        assert worker.audio_source.published_frame_sizes[0] >= worker._PLAYOUT_BUFFER_TARGET_BYTES, (
            "expected the first publish to wait for the target cushion to fill, "
            f"got a first publish of only {worker.audio_source.published_frame_sizes[0]} bytes "
            f"(target was {worker._PLAYOUT_BUFFER_TARGET_BYTES})"
        )
        print(
            f"First publish waited for the {worker._PLAYOUT_BUFFER_TARGET_BYTES}-byte "
            f"cushion (got {worker.audio_source.published_frame_sizes[0]} bytes): OK"
        )
    finally:
        conversion_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conversion_task

    print("Standing playout buffer test: SUCCESS")


async def test_playout_buffer_drops_oldest_over_cap():
    print("\n--- Testing standing playout buffer cap (bounded, drop-oldest) ---")

    class _UnusedAudioSource:
        """The target cushion below (50000 bytes) is set deliberately higher
        than everything the converter ever yields (20000 bytes), so playout
        never starts and capture_frame is never called -- this test is purely
        about the producer-side overflow/drop-oldest trim, independent of how
        fast (or slow) playout itself is. Exists only so VoiceConversionWorker
        has a valid self.audio_source to reference if that assumption ever
        stops holding."""
        async def capture_frame(self, frame):
            pass

    class _SlowTrickleConverter:
        async def convert_stream(self, in_audio):
            for i in range(20):
                yield bytes([i % 256]) * 1000  # 1000 bytes per chunk, 20000 total
                await asyncio.sleep(0.01)

    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=_SlowTrickleConverter(),
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    worker.audio_source = _UnusedAudioSource()
    worker._PLAYOUT_BUFFER_TARGET_BYTES = 50000  # higher than total fed: publish never starts
    worker._PLAYOUT_BUFFER_MAX_BYTES = 5000

    conversion_task = asyncio.create_task(worker._run_conversion_stream())
    try:
        await asyncio.sleep(0.5)  # let all 20 chunks (20000 bytes) arrive and overflow the 5000 cap
        assert len(worker._playout_buffer) == worker._PLAYOUT_BUFFER_MAX_BYTES, (
            f"expected playout buffer to sit exactly at the {worker._PLAYOUT_BUFFER_MAX_BYTES}-byte "
            f"cap, got {len(worker._playout_buffer)}"
        )
        # Drop-oldest means the buffer's tail must be the newest bytes fed
        # (value 19, the last chunk's fill byte), not the oldest (value 0).
        assert worker._playout_buffer[-1] == 19, (
            f"expected the newest fed byte (19) to survive at the tail, got {worker._playout_buffer[-1]}"
        )
        assert worker._playout_buffer[0] != 0, (
            "expected the oldest fed bytes (value 0) to have been dropped, but they're still at the head"
        )
        print(
            f"Playout buffer capped at {worker._PLAYOUT_BUFFER_MAX_BYTES} bytes, "
            "oldest bytes dropped, newest survive: OK"
        )
    finally:
        conversion_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conversion_task

    print("Standing playout buffer cap/drop-oldest test: SUCCESS")
```

Register both in `main()` (`backend/test_pipeline.py`, currently ending at line 581 with `await test_worker_readiness_probe_dedup()`):

```python
    await test_worker_readiness_probe_dedup()
    await test_playout_buffer_smooths_bursty_converter_output()
    await test_playout_buffer_drops_oldest_over_cap()
    print("\nAll automated verification tests completed successfully!")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m backend.test_pipeline`
Expected: FAIL — `AttributeError: 'VoiceConversionWorker' object has no attribute '_playout_buffer'` (or similar; `_PLAYOUT_BUFFER_TARGET_BYTES`/`_PLAYOUT_BUFFER_MAX_BYTES` don't exist yet either, but since they're read via instance-attribute override before any class default exists, Python will raise on whichever is accessed first inside `_run_conversion_stream`).

- [ ] **Step 3: Replace the one-shot jitter buffer with the standing buffer**

In `backend/pipeline.py`, replace the `_JITTER_TARGET_BYTES` class constant block (lines 31-35):

```python
    # One-time jitter-buffer fill target before playout starts: hold converted
    # output until this many bytes (100ms of 48kHz 16-bit mono) have accumulated,
    # then publish continuously. After the initial fill, capture_frame's own
    # backpressure paces playback — see _publish_frames / AudioSource queue_size_ms.
    _JITTER_TARGET_BYTES = int(48000 * 2 * 0.1)
```

with:

```python
    # Standing playout buffer (2026-07-03: latency is explicitly not a product
    # priority here, voice continuity is). Converted output is never published
    # directly off the converter's arrival timing -- it always goes through
    # this buffer first, drained by a separate real-time-paced consumer task
    # (_run_playout_consumer). This absorbs the case where one inference block
    # takes longer than its real-time budget (confirmed in production: ~460-
    # 590ms to convert a 320ms block even after the FAISS index caching fix)
    # by growing the lead's delay instead of producing a silence gap.
    #
    # Target: initial cushion before playout starts (~3s of 48kHz 16-bit mono).
    _PLAYOUT_BUFFER_TARGET_BYTES = int(48000 * 2 * 3.0)
    # Cap: beyond this, drop the OLDEST buffered audio rather than let delay
    # grow unbounded -- same policy as RVCStreamingConverter's reconnect
    # buffer (backend/converters/rvc_stream.py, _MAX_BUFFER_BYTES).
    _PLAYOUT_BUFFER_MAX_BYTES = int(48000 * 2 * 5.0)
```

Then update the stale comment at `backend/pipeline.py:154-160` (in `start()`, above the `AudioSource` construction) — replace:

```python
        # Publish the converted audio output track
        # We use 48kHz because the converter (RVC-style engines) outputs 48kHz PCM.
        # queue_size_ms bounds LiveKit's internal playout buffer. Lowered from the
        # old 400ms to 200ms: the new consumer has its own small (100ms, one-shot)
        # jitter buffer upstream of this, so a large LiveKit-side buffer on top of
        # that would just add latency without absorbing any additional jitter —
        # 200ms is enough headroom for capture_frame's pacing without letting
        # converted audio pile up mid-call.
```

with:

```python
        # Publish the converted audio output track
        # We use 48kHz because the converter (RVC-style engines) outputs 48kHz PCM.
        # queue_size_ms bounds LiveKit's OWN internal playout queue, which is
        # deliberately small (200ms) -- it's just a thin real-time pacing buffer
        # fed continuously by _run_playout_consumer's own much larger standing
        # buffer (_PLAYOUT_BUFFER_TARGET_BYTES/_MAX_BYTES, several seconds).
        # Growing this LiveKit-side queue wouldn't help: it's fed by our own
        # consumer at a steady pace already, so 200ms is enough headroom for
        # capture_frame's pacing without letting audio pile up in TWO places.
```

Finally, replace `_run_conversion_stream` (lines 409-462) — the `async for converted_chunk in gen:` body changes from directly publishing to appending into the standing buffer, and a new `_run_playout_consumer` method does the draining:

```python
    async def _run_conversion_stream(self):
        """Drives the converter as ONE long-lived duplex stream for the life of the
        worker's active pipeline: feeds 20ms frames in via _frame_pairs and appends
        whatever comes back, in arrival order, into a standing playout buffer that
        _run_playout_consumer drains at a steady real-time pace -- see the class
        docstring above _PLAYOUT_BUFFER_TARGET_BYTES for why. Never falls back to
        raw audio: on outage the converter simply stops yielding and the buffer
        just stops refilling until it reconnects and resumes (see
        RVCStreamingConverter's own bounded reconnect-buffer/backoff logic in
        converters/rvc_stream.py).
        """
        gen = self.converter.convert_stream(self._frame_pairs())
        self._last_chunk_at = time.monotonic()
        watchdog_task = asyncio.create_task(self._holding_watchdog())

        self._playout_buffer = bytearray()
        self._playout_buffer_lock = asyncio.Lock()
        self._playout_ready = asyncio.Event()
        consumer_task = asyncio.create_task(self._run_playout_consumer())

        try:
            # contextlib.aclosing guarantees the async generator is properly closed
            # (triggering the converter's internal teardown) whenever we leave this
            # block — whether by exhausting it, an exception, or this task being
            # cancelled. A bare `async for ... break` would not reliably do this.
            async with contextlib.aclosing(gen):
                async for converted_chunk in gen:
                    self._last_chunk_at = time.monotonic()
                    if self._is_holding:
                        self._is_holding = False
                        self._publish_latency_metric(force=True)

                    if not self._use_stats_latency:
                        estimate = self._estimate_fallback_latency_ms(converted_chunk)
                        if estimate is not None:
                            self._latest_latency_ms = estimate
                            self._publish_latency_metric()

                    if not converted_chunk:
                        continue

                    async with self._playout_buffer_lock:
                        self._playout_buffer.extend(converted_chunk)
                        overflow = len(self._playout_buffer) - self._PLAYOUT_BUFFER_MAX_BYTES
                        if overflow > 0:
                            # Sustained overload: even the cushion can't keep up.
                            # Drop the OLDEST audio rather than let delay grow
                            # unbounded -- logged because it means the block size
                            # / GPU tier genuinely can't sustain this call's real-
                            # time factor, not something a bigger buffer fixes.
                            del self._playout_buffer[:overflow]
                            print(
                                f"[Worker] Playout buffer over {self._PLAYOUT_BUFFER_MAX_BYTES}-byte "
                                f"cap — dropped {overflow} oldest bytes"
                            )
                        self._playout_ready.set()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Worker Error in conversion stream] {e}")
            traceback.print_exc()
        finally:
            consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer_task
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task

    async def _run_playout_consumer(self):
        """Drains _playout_buffer into LiveKit at a steady pace, decoupled from
        how bursty or delayed the converter's actual output is. Waits for the
        initial cushion (_PLAYOUT_BUFFER_TARGET_BYTES) before the first publish,
        then continuously publishes whatever has accumulated since the last
        publish — _publish_frames' own capture_frame backpressure already paces
        real playback correctly; this just guarantees it's never starved by one
        slow block, because the buffer (not the converter's arrival timing)
        decides what's available to publish next."""
        filled = False
        try:
            while True:
                async with self._playout_buffer_lock:
                    if not filled and len(self._playout_buffer) < self._PLAYOUT_BUFFER_TARGET_BYTES:
                        chunk = b""
                    else:
                        filled = True
                        chunk = bytes(self._playout_buffer)
                        self._playout_buffer.clear()
                    self._playout_ready.clear()
                if chunk:
                    await self._publish_frames(chunk)
                else:
                    await self._playout_ready.wait()
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m backend.test_pipeline`
Expected: all tests PASS, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/pipeline.py backend/test_pipeline.py
git commit -m "Replace one-shot jitter buffer with a standing playout buffer (absorbs slow blocks as delay, not silence)"
```

---

### Task 3: Deploy and verify against a real call

**Files:**
- None (deployment + manual verification only — Tasks 1-2 already contain all code changes)

- [ ] **Step 1: Deploy the updated Modal worker (Task 1's change)**

Run: `modal deploy modal_deploy/worker.py`
Expected: deploy succeeds (same as the prior FAISS-caching plan's Task 2 Step 1).

- [ ] **Step 2: Push the backend change (Task 2's change) to trigger the Render auto-deploy**

```bash
git push
```
Expected: Render redeploys automatically (`autoDeploy: commit`, per `.agents/context/subsystem-notes.md`) — re-warm Modal afterward (`POST /api/warmup`) before the next test call, same as prior deploys in this session.

- [ ] **Step 3: Place a test call and listen for continuity, not speed**

Expected: some added delay between the agent speaking and the lead hearing it (this is the intended tradeoff — no longer a bug), but no more "part by part" gaps or stutter. It should sound like a delayed-but-smooth voice, not a broken one.

- [ ] **Step 4: If it's still choppy, report exactly what you hear — don't stack a third speculative change**

If gaps persist even after this, the real-time factor is worse than this buffer's ~3-5s cushion can absorb over the length of a real call, which means the next step is measuring the actual sustained real-time factor from production `[Timing]` logs (block conversion time vs. `BLOCK_MS`) rather than guessing at a bigger cap.

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers the "quality via bigger blocks" half of the goal; Task 2 covers the "no disruption via a real buffer" half; Task 3 verifies both against a live call and defines what "still broken" looks like next, matching this session's established debugging discipline (evidence over guessing).
- **No placeholders:** every step has complete, runnable code and exact commands with expected output.
- **Type/name consistency:** `_PLAYOUT_BUFFER_TARGET_BYTES`, `_PLAYOUT_BUFFER_MAX_BYTES`, `_playout_buffer`, `_playout_buffer_lock`, `_playout_ready`, and `_run_playout_consumer` are used identically between Task 2's implementation step and its two test functions (both override the class constants as instance attributes before starting `_run_conversion_stream`, matching the existing `_MAX_BUFFER_BYTES` override pattern already established in `test_rvc_streaming_converter_buffer_cap_drop_oldest`).
- **Removed, not just added:** Task 2 explicitly removes the old `_JITTER_TARGET_BYTES` constant and its one-shot fill logic (not left dangling alongside the new buffer) and fixes the now-stale `queue_size_ms` comment that referenced it.
