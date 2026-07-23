# Playout Consumer Real-Time Pacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the playout consumer from ever draining backlog faster than real time, so a slow/backlogged Modal round trip surfaces only as growing (bounded) delay, never as time-compressed/"breaking" audio.

**Architecture:** `_run_playout_consumer` (`backend/pipeline.py`) currently drains `_playout_buffer` with no wall-clock pacing of its own — it relies entirely on `_publish_frames`'s LiveKit `capture_frame` backpressure (an ~200ms-deep queue) to throttle output to real time. When the buffer is backlogged (this pipeline's Modal round trip regularly runs 900–1900ms against a 320ms block budget), the consumer's loop can push multiple 100ms `_PLAYOUT_DRAIN_BYTES` chunks into that queue faster than they play out before backpressure engages — a burst of time-compressed audio, repeating every drain cycle for as long as backlog persists. Add an explicit, self-correcting real-time pacer (`next_publish_time`) inside the consumer loop: never publish a chunk before its real-time-equivalent deadline. Backlog then only ever grows the buffer (already bounded/drop-oldest at `_PLAYOUT_BUFFER_MAX_BYTES`), never speeds up playback.

**Tech Stack:** Python 3.12, asyncio, LiveKit `rtc` SDK. Tests run via the project's own runner, not pytest: `python -m backend.test_pipeline`.

## Global Constraints

- Never publish raw/unconverted audio — this fix does not touch that invariant, don't introduce a path that could bypass `_playout_buffer`.
- Never block the event loop — all waits here are `await asyncio.sleep(...)` / `await asyncio.Event.wait()`, already the existing pattern.
- Buffering/playout logic in this file has been reverted and reimplemented repeatedly (`.agents/projects/active-backlog.md:44`) — treat this as high-risk, keep the diff minimal, and do not change `_PLAYOUT_BUFFER_TARGET_BYTES`, `_PLAYOUT_BUFFER_MAX_BYTES`, or `_PLAYOUT_DRAIN_BYTES` values as part of this fix.
- Never trigger a Render deploy (`git push` to the deployed branch, or `POST /api/deploy`) without the user's explicit go-ahead — even to verify this fix live. Task 3 below is gated on that.
- Run the full existing suite (`python -m backend.test_pipeline`) before considering any task done — it must stay 100% green.

---

### Task 1: Real-time pacing in `_run_playout_consumer`

**Files:**
- Modify: `backend/pipeline.py:79` (new class constant), `backend/pipeline.py:706-735` (`_run_playout_consumer`)
- Test: `backend/test_pipeline.py` (new test appended after `test_playout_buffer_drops_oldest_over_cap`, plus registration in `main()`)

**Interfaces:**
- Consumes: `VoiceConversionWorker._playout_buffer` (bytearray), `_playout_buffer_lock` (asyncio.Lock), `_playout_ready` (asyncio.Event), `_PLAYOUT_BUFFER_TARGET_BYTES`, `_PLAYOUT_DRAIN_BYTES`, `_publish_frames(audio_payload: bytes) -> None` — all pre-existing, unchanged signatures.
- Produces: new class constant `_PLAYOUT_BYTES_PER_SECOND: int` (bytes/sec of 48kHz 16-bit mono PCM = 96000), used only inside `_run_playout_consumer`.

- [ ] **Step 1: Write the failing test**

Append to `backend/test_pipeline.py`, directly after `test_playout_buffer_drops_oldest_over_cap` (before `test_presence_eq`):

```python
async def test_playout_consumer_paces_drain_to_real_time():
    print("\n--- Testing playout consumer paces backlog drain to real time (no burst) ---")

    class _OneShotBurstConverter:
        """Yields one big chunk immediately -- simulates backlog that has
        already piled up in _playout_buffer (this pipeline's Modal round trip
        regularly runs 900-1900ms against a 320ms block budget) -- then keeps
        the duplex stream open, same as a real in-progress call."""
        async def convert_stream(self, in_audio):
            yield b"\x00\x01" * 48000  # 96000 bytes = 1.0s of 48kHz 16-bit mono PCM
            await asyncio.sleep(30)

    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=_OneShotBurstConverter(),
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    publish_events = []

    async def fake_publish_frames(payload):
        publish_events.append((time.monotonic(), len(payload)))

    worker._publish_frames = fake_publish_frames
    # Small cushion so the first publish starts almost immediately -- the
    # invariant under test is DRAIN pacing, not the initial cushion wait.
    worker._PLAYOUT_BUFFER_TARGET_BYTES = 4000
    worker._PLAYOUT_DRAIN_BYTES = 9600  # matches the real 100ms production default

    conversion_task = asyncio.create_task(worker._run_conversion_stream())
    try:
        total_bytes = 96000
        deadline = time.monotonic() + 5.0
        while sum(n for _, n in publish_events) < total_bytes and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        assert sum(n for _, n in publish_events) == total_bytes, (
            f"expected all {total_bytes} bytes to eventually reach _publish_frames, "
            f"got {sum(n for _, n in publish_events)}"
        )

        first_ts, first_len = publish_events[0]
        last_ts, _ = publish_events[-1]
        elapsed_s = last_ts - first_ts
        # Audio duration published AFTER the first (cushion) call -- that's
        # what real-time pacing governs; the first call is the cushion
        # release and isn't paced against anything before it.
        remaining_audio_s = (total_bytes - first_len) / worker._PLAYOUT_BYTES_PER_SECOND

        print(f"Drained {total_bytes} bytes across {len(publish_events)} publish calls "
              f"in {elapsed_s:.3f}s wall time (audio duration after cushion: {remaining_audio_s:.3f}s)")
        assert elapsed_s >= remaining_audio_s - 0.1, (
            f"backlog drained faster than real time -- got {elapsed_s:.3f}s wall time for "
            f"{remaining_audio_s:.3f}s of audio (a burst of time-compressed audio, the exact "
            f"bug this pacer exists to prevent)"
        )
        assert elapsed_s <= remaining_audio_s + 1.0, (
            f"drain took much longer than real time ({elapsed_s:.3f}s for {remaining_audio_s:.3f}s "
            f"of audio) -- pacing is over-throttling, not just preventing bursts"
        )
    finally:
        conversion_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conversion_task

    print("Playout consumer real-time pacing test: SUCCESS")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -c "import asyncio; from backend.test_pipeline import test_playout_consumer_paces_drain_to_real_time; asyncio.run(test_playout_consumer_paces_drain_to_real_time())"`

Expected: `AssertionError: backlog drained faster than real time -- got 0.0XXs wall time for 0.958s of audio (...)` — the current unpaced consumer drains the whole backlog in a handful of event-loop ticks, nowhere close to real time.

- [ ] **Step 3: Implement the pacer**

In `backend/pipeline.py`, add the new constant right after `_PLAYOUT_DRAIN_BYTES` (line 79):

```python
    _PLAYOUT_DRAIN_BYTES = int(48000 * 2 * 0.10)
    _PLAYOUT_BYTES_PER_SECOND = 48000 * 2
```

Replace the body of `_run_playout_consumer` (lines 706-735) with:

```python
    async def _run_playout_consumer(self):
        """Drains _playout_buffer into LiveKit at a strictly real-time pace,
        decoupled from how bursty or delayed the converter's actual output is.
        Waits for the initial cushion (_PLAYOUT_BUFFER_TARGET_BYTES) before the
        first publish, then continuously publishes whatever has accumulated
        since the last publish -- paced here via next_publish_time rather than
        relying on _publish_frames' capture_frame backpressure alone.
        LiveKit's AudioSource queue has ~200ms of headroom, so a backpressure-
        only design can push several drain-sized chunks into that queue faster
        than real time before backpressure engages -- audible as a burst of
        time-compressed audio while the buffer quietly refills underneath (see
        "Open finding 2026-07-14" in .agents/context/subsystem-notes.md).
        Backlog must only ever show up as growing (bounded, drop-oldest)
        delay, never as speed.

        next_publish_time is self-correcting: if a chunk becomes available
        after its deadline already passed (e.g. the buffer genuinely ran dry
        and we were waiting on _playout_ready), we don't sleep to "catch up" --
        we publish immediately and re-anchor pacing from now. Otherwise a real
        stall would leave a stale schedule that forces the next audio out
        faster than real time once data resumes, which is the same class of
        bug this pacer exists to prevent, just triggered a different way.
        """
        filled = False
        next_publish_time: Optional[float] = None
        try:
            while True:
                async with self._playout_buffer_lock:
                    if not filled:
                        if len(self._playout_buffer) < self._PLAYOUT_BUFFER_TARGET_BYTES:
                            chunk = b""
                        else:
                            chunk = bytes(self._playout_buffer[:self._PLAYOUT_BUFFER_TARGET_BYTES])
                            del self._playout_buffer[:self._PLAYOUT_BUFFER_TARGET_BYTES]
                            filled = True
                    else:
                        chunk = bytes(self._playout_buffer[:self._PLAYOUT_DRAIN_BYTES])
                        del self._playout_buffer[:self._PLAYOUT_DRAIN_BYTES]
                    self._playout_ready.clear()
                if chunk:
                    now = time.monotonic()
                    if next_publish_time is None or now >= next_publish_time:
                        next_publish_time = now
                    else:
                        await asyncio.sleep(next_publish_time - now)
                    await self._publish_frames(chunk)
                    next_publish_time += len(chunk) / self._PLAYOUT_BYTES_PER_SECOND
                else:
                    await self._playout_ready.wait()
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -c "import asyncio; from backend.test_pipeline import test_playout_consumer_paces_drain_to_real_time; asyncio.run(test_playout_consumer_paces_drain_to_real_time())"`

Expected: prints `Drained 96000 bytes across N publish calls in ~0.9-1.0s wall time (audio duration after cushion: 0.958s)` then `Playout consumer real-time pacing test: SUCCESS`, exit code 0.

- [ ] **Step 5: Register the test and run the full suite**

In `backend/test_pipeline.py`'s `main()`, add the new test to the call sequence, directly after `test_playout_buffer_drops_oldest_over_cap()`:

```python
    await test_playout_buffer_smooths_bursty_converter_output()
    await test_playout_buffer_drops_oldest_over_cap()
    await test_playout_consumer_paces_drain_to_real_time()
    await test_presence_eq()
```

Run: `python -m backend.test_pipeline`

Expected: `All automated verification tests completed successfully!` — every existing test (including `test_playout_buffer_smooths_bursty_converter_output`, `test_playout_buffer_drops_oldest_over_cap`, `test_worker_applies_presence_eq`) still passes unchanged, since they all publish well under 1s of audio and stay inside their existing multi-second deadlines even with pacing added.

- [ ] **Step 6: Commit**

```bash
git add backend/pipeline.py backend/test_pipeline.py
git commit -m "fix(pipeline): pace playout consumer drain to real time, never burst backlog"
```

---

### Task 2: Update the second-brain docs this bug is tracked in

**Files:**
- Modify: `.agents/projects/active-backlog.md:29`
- Modify: `.agents/context/subsystem-notes.md` (append to the "Open finding (2026-07-14...)" paragraph at lines 74-90)

**Interfaces:** None (documentation only, no code interfaces).

- [ ] **Step 1: Update the backlog row**

In `.agents/projects/active-backlog.md`, replace line 29:

```markdown
| **Playout buffer overshoots and gulp-drains** during long continuous speech; consumer now drains bounded 100ms chunks. | High | Local fix implemented 2026-07-15; targeted live listen test pending |
```

with:

```markdown
| **Playout buffer overshoots and gulp-drains** during long continuous speech. 2026-07-15 fix (bounded 100ms drain chunks) alone did not resolve it -- 2026-07-16 live call telemetry still showed the same oscillation (playout_buffer_bytes swinging 0 to 500-740ms repeatedly across one call). Root cause: the consumer had no wall-clock pacing of its own, only LiveKit backpressure, which has enough queue headroom to let backlogged chunks through faster than real time. Fixed with an explicit real-time pacer in `_run_playout_consumer` (see `docs/superpowers/plans/2026-07-16-playout-consumer-real-time-pacing.md`). | High | Local fix implemented 2026-07-16; live listen test pending |
```

- [ ] **Step 2: Amend the subsystem-notes open finding**

In `.agents/context/subsystem-notes.md`, append this paragraph directly after the existing "Open finding (2026-07-14...)" paragraph (after line 90, before the `_run_playout_consumer` behavior it describes):

```markdown
- **Update (2026-07-16): the 2026-07-15 bounded-100ms-chunk fix did not resolve this.** A
  fresh call's `[Worker][LatencySummary]` telemetry showed the identical oscillation
  signature (`playout_buffer_bytes` swinging between ~0 and 500-740ms repeatedly across the
  call, `converter_wait_ms`/`network_rtt_ms` consistently 900-1900ms against a 320ms block
  budget). Smaller bounded chunks reduced the size of each burst but didn't remove the
  mechanism: `_run_playout_consumer` still had no wall-clock pacing of its own, only
  LiveKit's `capture_frame` backpressure (~200ms queue headroom), which still let a
  backlogged 100ms chunk through faster than real time. Fixed by adding an explicit,
  self-correcting `next_publish_time` pacer directly in the consumer loop -- see
  `docs/superpowers/plans/2026-07-16-playout-consumer-real-time-pacing.md`. Live listen
  confirmation against this specific fix is still open (Task 3 of that plan).
```

- [ ] **Step 3: Verify the edits landed**

Run: `grep -n "2026-07-16" d:/Kiera/.agents/projects/active-backlog.md d:/Kiera/.agents/context/subsystem-notes.md`

Expected: both files show the new text at the lines edited above.

- [ ] **Step 4: Commit**

```bash
git add .agents/projects/active-backlog.md .agents/context/subsystem-notes.md
git commit -m "docs: record that the 2026-07-15 gulp-drain fix was insufficient, link the real fix"
```

---

### Task 3: Live verification (gated — do not start without the user's explicit go-ahead)

This task deploys to the live Render service and makes a real PSTN test call. Per this project's production-safety norm, do not run any step below until the user explicitly approves the deploy.

**Files:** None (deployment + manual listening test, no code changes).

- [ ] **Step 1: Get explicit user approval to deploy**

Ask the user directly: "Task 1 and 2 are committed. Ready for me to deploy this to Render and make a test call to confirm it by ear?" Do not proceed past this point without a clear yes.

- [ ] **Step 2: Deploy**

Push the commits from Tasks 1-2 to the branch Render auto-deploys from (per `render.yaml`'s `autoDeploy: commit`), or trigger deploy per this project's normal flow. Confirm the new revision is live via `GET https://kiera.onrender.com/api/health`.

- [ ] **Step 3: Warm the GPU and make a long-utterance test call**

`POST /api/warmup` (with the `Authorization: Bearer <KEIRA_CONTROL_TOKEN>` header) to warm Modal before dialing — a cold-start call would confound the listening test. Then place a real test call and speak one continuous, unbroken sentence for at least 20-30 seconds (long enough to have previously triggered breaking in both the 2026-07-14 and 2026-07-16 calls).

- [ ] **Step 4: Confirm by ear and by telemetry**

Listen for breaking/blurring during the long utterance. Separately, pull that call's `[Worker][LatencySummary]` block log (Render logs, or local stdout if run locally) and check `playout_buffer_bytes` across the call — it should now either stay near the 0.25s target or grow smoothly without the previous sharp oscillation pattern.

- [ ] **Step 5: Update the backlog row's status**

If confirmed clean, change `.agents/projects/active-backlog.md`'s row (edited in Task 2) from "live listen test pending" to "Confirmed via live call YYYY-MM-DD" and commit. If still broken, that's new evidence the mechanism has a second contributing cause (most likely the Modal routing-region latency itself, already an open, separate backlog item) — report findings rather than immediately attempting another fix.
