# Per-Block Latency Attribution + End-of-Call Log Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Break the opaque per-block `infer_ms` the Modal TRT worker reports today into its four model stages (HuBERT, FAISS index mixing, RMVPE F0, generator) plus GPU-lock queueing time, propagate all of it to the backend over the existing `/ws` "stats" message, and print every block's full breakdown plus aggregate (avg/median/p95/max) stats to the Render process's stdout once a call ends.

**Architecture:** `TRTVoicePipeline.convert_block` (`modal_deploy/trt_pipeline.py`) already runs the 4 model stages sequentially and separately from FAISS mixing — add `time.perf_counter()` brackets around each and stash the result in `self.last_block_timing`. `worker.py`'s `/ws` handler (`modal_deploy/worker.py`) already measures a single `infer_ms` around the GPU-locked `convert_block` call — split that into `lock_wait_ms` (waiting for the single-tenant GPU lock) and true compute time, and merge `engine.last_block_timing` into the existing `{"type":"stats",...}` JSON message it already sends per block. `RVCStreamingConverter._handle_incoming` (`backend/converters/rvc_stream.py`) already forwards every key in that message generically to `on_stats` — **no change needed there**. `VoiceConversionWorker._on_converter_stats` (`backend/pipeline.py`) already receives that dict per block — append it (plus the current playout-buffer occupancy) to a per-call list instead of discarding all but the latest value, and print the full list plus aggregates from a new `_log_call_latency_summary()` method called at the top of `stop()`, which is the one code path that runs for every call teardown (natural hangup via `run_worker_task`'s `finally`, and the explicit `/api/call/end` endpoint).

**Tech Stack:** Python 3 stdlib only (`time.perf_counter`, `statistics`) — no new dependencies.

## Global Constraints

- Never block the asyncio event loop — no new synchronous work is added on the event-loop side; `convert_block` already runs inside `asyncio.to_thread` (`worker.py:627`) and that is not changed.
- Preserve the existing `{"type":"stats","infer_ms":...,"block_ms":...}` message shape — `infer_ms` and `block_ms` keys and semantics are unchanged; new keys are additive only, so older backend versions talking to a newer worker (or vice versa) don't break.
- Preserve the frontend-facing `{"pipeline_latency_ms":..,"is_fallback":..}` data-channel payload exactly as-is (`_publish_latency_metric`) — this feature is server-log-only, not a UI change.
- Never introduce a raw-audio-fallback path or otherwise touch the fail-closed conversion invariant (`.agents/context/stack-and-rules.md`) — this plan only adds timing instrumentation and logging around existing calls.
- No new external pip dependencies. Use `statistics` (stdlib).
- Follow existing code style: `print(f"[Worker] ...")`-style prefixed logging in `backend/pipeline.py`, `print(f"[TRT] ...")`/`[Timing]` style in `modal_deploy/`.

---

### Task 1: Per-stage timing inside the TRT inference pipeline

**Files:**
- Modify: `modal_deploy/trt_pipeline.py:9` (imports), `modal_deploy/trt_pipeline.py:164` (`__init__`), `modal_deploy/trt_pipeline.py:199-259` (`convert_block`)
- Verify (manual, GPU-dependent — see Step 4): `modal_deploy/worker.py`'s `main_chunked` offline diagnostic entrypoint

**Interfaces:**
- Produces: `TRTVoicePipeline.last_block_timing: dict` — set (overwritten) at the end of every `convert_block()` call, with keys `hubert_ms`, `index_ms`, `rmvpe_ms`, `generator_ms`, `postproc_ms`, `total_ms` (all `float`, milliseconds, rounded to 2 decimals). Consumed by Task 2.
- Consumes: nothing new — `convert_block`'s existing signature and return value (`np.ndarray`, int16 @ 48kHz) are unchanged.

This class requires real `onnxruntime`/TensorRT sessions and a real torch `MelSpectrogram` module to instantiate (`__init__` does `import onnxruntime as ort` and `_f0` does `import torch`), so it cannot be unit-tested in this repo's GPU-free environment — `modal_deploy/test_trt_pipeline.py`'s own docstring scopes it to "pure-NumPy helpers... No GPU, no Modal." This task is therefore verified manually against a live Modal deployment in Step 4, not via a new pytest test. Do not attempt to mock `onnxruntime.InferenceSession`/torch's `MelSpectrogram` into a fake — that produces a test that passes regardless of whether the real timing brackets are placed correctly, which is worse than no test.

- [ ] **Step 1: Add the `time` import**

In `modal_deploy/trt_pipeline.py`, line 9 currently reads:
```python
import numpy as np
```
Change to:
```python
import numpy as np
import time
```

- [ ] **Step 2: Initialize `last_block_timing` in `__init__`**

Line 164 currently reads:
```python
        self._rng = np.random.default_rng(0)   # rnd noise; seeded = reproducible tests
```
Add immediately after it:
```python
        self._rng = np.random.default_rng(0)   # rnd noise; seeded = reproducible tests
        self.last_block_timing: dict = {}
```

- [ ] **Step 3: Bracket each stage in `convert_block` and record the breakdown**

Replace the entire method body currently at `modal_deploy/trt_pipeline.py:199-259`:

```python
    def convert_block(self, pcm_int16, pitch_shift: int = 0, index_rate: float = 0.75,
                      rms_mix_rate: float = 0.75, protect: float = 0.33,
                      filter_radius: int = 3) -> np.ndarray:
        """Convert one streaming block. Returns int16 @ 48 kHz, length exactly 3*len(pcm_int16).

        Records a per-stage timing breakdown (ms) in self.last_block_timing after
        every call: hubert_ms, index_ms, rmvpe_ms, generator_ms, postproc_ms, total_ms.
        """
        n_in = len(pcm_int16)
        audio, zpad = pad_to_canonical(pcm_int16)

        t_start = time.perf_counter()

        # ---- Engine 1: HuBERT / ContentVec ----
        feats = self.s_hubert.run(None, {"audio": audio[None, :]})[0]   # [1, 170, 768]
        feats_raw_pre = feats.copy()
        t_hubert = time.perf_counter()

        # ---- FAISS index mixing (CPU, cannot be TRT) ----
        if self.index is not None and self.big_npy is not None and index_rate > 0:
            npy = feats[0].astype(np.float32)
            score, ix = self.index.search(npy, k=8)
            weight = np.square(1.0 / np.maximum(score, 1e-9))
            weight /= weight.sum(axis=1, keepdims=True)
            mixed = np.sum(self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1)
            feats = (index_rate * mixed + (1 - index_rate) * npy)[None].astype(np.float32)
        else:
            if index_rate > 0:
                print("[TRT] FAISS index not loaded -- index mixing skipped (raw HuBERT feats only)")

        feats = np.repeat(feats, 2, axis=1)          # [1, 340, 768]
        feats_raw = np.repeat(feats_raw_pre, 2, axis=1)
        t_index = time.perf_counter()

        # ---- Engine 3: RMVPE F0 ----
        pitch, pitchf = self._f0(audio, pitch_shift, filter_radius)
        t_rmvpe = time.perf_counter()

        # ---- protect (consonant guard) ----
        feats = apply_protect(feats, feats_raw, pitchf, protect)

        # ---- Engine 2: generator ----
        # sine_noise: audio-rate N(0,1) noise for SineGen unvoiced segments.
        # Generated here (numpy RNG) rather than inside the ONNX graph so TRT
        # Myelin never sees a RandomNormal op. Shape [1, OUT_PADDED_48K, 1].
        sine_noise = self._rng.standard_normal((1, OUT_PADDED_48K, 1)).astype(np.float32)
        out = self.s_gen.run(None, {
            "phone": feats.astype(np.float32),
            "phone_lengths": np.array([GEN_FRAMES], dtype=np.int64),
            "pitch": pitch[None, :],
            "pitchf": pitchf[None, :],
            "sid": np.array([0], dtype=np.int64),
            "rnd": self._rng.standard_normal((1, 192, GEN_FRAMES)).astype(np.float32),
            "sine_noise": sine_noise,
        })[0].reshape(-1)                                   # [OUT_PADDED_48K] float32
        t_generator = time.perf_counter()

        # ---- trim fixed pad, then the zero-fill's share, then rms mix ----
        out = out[T_PAD_TGT: T_PAD_TGT + OUT_48K]
        out = out[3 * zpad:]
        # Pad to exactly 3 * n_in to satisfy the streaming contract (offsetting Hubert conv boundary loss)
        target_len = 3 * n_in
        pad_needed = target_len - len(out)
        if pad_needed > 0:
            out = np.pad(out, (0, pad_needed), mode="edge")
        if rms_mix_rate < 1.0:
            src = np.asarray(pcm_int16, dtype=np.float32) / 32768.0
            out = change_rms(src, out, rms_mix_rate)
        out = np.clip(out * 32767.0, -32768, 32767).astype(np.int16)
        assert len(out) == 3 * n_in, f"contract violation: {len(out)} != 3*{n_in}"

        t_end = time.perf_counter()
        self.last_block_timing = {
            "hubert_ms": round((t_hubert - t_start) * 1000.0, 2),
            "index_ms": round((t_index - t_hubert) * 1000.0, 2),
            "rmvpe_ms": round((t_rmvpe - t_index) * 1000.0, 2),
            "generator_ms": round((t_generator - t_rmvpe) * 1000.0, 2),
            "postproc_ms": round((t_end - t_generator) * 1000.0, 2),
            "total_ms": round((t_end - t_start) * 1000.0, 2),
        }
        return out
```

Note this is a pure instrumentation change: no line of actual DSP/inference logic was altered, only `perf_counter()` calls and the trailing dict assignment were added.

- [ ] **Step 4: Manual verification against a real Modal deployment**

This cannot run in CI (no GPU/TensorRT locally). After deploying (Task 5 covers the actual `modal deploy`), run the existing offline diagnostic tool against a short WAV to confirm the new timing dict populates sanely:
```bash
modal run modal_deploy/worker.py::main_chunked --input-file <path-to-a-short-test.wav> --pitch 7
```
Expected: the run completes and produces converted audio as before (unchanged output). This step alone won't print `last_block_timing` (that's wired to the `/ws` handler's stats message in Task 2, not `main_chunked`) — it only proves `convert_block` still functions correctly with the added timing code. Confirm no exception is raised and the output WAV plays/sounds the same as a pre-change baseline.

- [ ] **Step 5: Commit**

```bash
git add modal_deploy/trt_pipeline.py
git commit -m "feat(voice): capture per-stage TRT inference timing in convert_block"
```

---

### Task 2: Split GPU-lock wait time and forward the stage breakdown over `/ws`

**Files:**
- Modify: `modal_deploy/worker.py:606-664`

**Interfaces:**
- Consumes: `TRTVoicePipeline.last_block_timing` (Task 1's output — `dict` with `hubert_ms`/`index_ms`/`rmvpe_ms`/`generator_ms`/`postproc_ms`/`total_ms`, may be empty `{}` if not yet set). **Attribute location matters:** the `/ws` handler's `engine` is an `RVCEngine`, whose `convert_block` (worker.py:388-392) *delegates* to `self.trt_pipe.convert_block` — so the timing dict lives on `engine.trt_pipe`, NOT on `engine`. `engine.trt_pipe` is `None` on the ONNX-CUDA fallback path (worker.py:204-205), in which case there is no stage breakdown and the stats message just carries the existing fields plus `lock_wait_ms`.
- Produces: the `/ws` "stats" message now has the shape `{"type":"stats","infer_ms":float,"block_ms":int,"lock_wait_ms":float, **last_block_timing}` — consumed by Task 3 (`RVCStreamingConverter` needs no change; it already forwards every key).

- [ ] **Step 1: Default the new fields on the silence-bypass path and split lock-wait from compute time**

In `modal_deploy/worker.py`, the block starting at line 606 currently reads:
```python
                    infer_input, context_len, block = popped

                    infer_ms = 0.0
                    if st.block_rms(block) < st.SILENCE_RMS_THRESHOLD:
```
Change to:
```python
                    infer_input, context_len, block = popped

                    infer_ms = 0.0
                    lock_wait_ms = 0.0
                    block_timing: dict = {}
                    if st.block_rms(block) < st.SILENCE_RMS_THRESHOLD:
```

- [ ] **Step 2: Split lock-wait vs. compute time and capture the stage breakdown**

Immediately below (currently `modal_deploy/worker.py:624-635`):
```python
                        try:
                            t0 = time.perf_counter()
                            async with _gpu_lock:  # single-tenant GPU
                                out_bytes = await asyncio.to_thread(
                                    engine.convert_block,
                                    infer_input,
                                    session_pitch,
                                    index_rate,
                                    rms_mix_rate,
                                    protect,
                                )
                            infer_ms = (time.perf_counter() - t0) * 1000.0
                        except Exception as e:
```
Replace with:
```python
                        try:
                            t_wait_start = time.perf_counter()
                            async with _gpu_lock:  # single-tenant GPU
                                t_compute_start = time.perf_counter()
                                out_bytes = await asyncio.to_thread(
                                    engine.convert_block,
                                    infer_input,
                                    session_pitch,
                                    index_rate,
                                    rms_mix_rate,
                                    protect,
                                )
                            t_compute_end = time.perf_counter()
                            infer_ms = (t_compute_end - t_wait_start) * 1000.0
                            lock_wait_ms = (t_compute_start - t_wait_start) * 1000.0
                            # Per-stage breakdown lives on the TRT pipeline object
                            # (engine.trt_pipe), not on RVCEngine itself — engine's
                            # convert_block only delegates there. trt_pipe is None on
                            # the ONNX-CUDA fallback path: no breakdown, empty dict.
                            block_timing = dict(
                                getattr(getattr(engine, "trt_pipe", None), "last_block_timing", None) or {}
                            )
                        except Exception as e:
```
(`t0`/`time.perf_counter() - t0` is renamed to `t_wait_start`/`infer_ms` computed from the same start point, so `infer_ms`'s meaning — total wall time including lock wait — is **unchanged**, preserving backward compatibility with anything already trending on it. The nested `getattr` chain: `getattr(None, "last_block_timing", None)` never raises, so a missing/None `trt_pipe` degrades to `{}` rather than crashing the stats send.)

- [ ] **Step 3: Merge the new fields into the outgoing stats message**

`modal_deploy/worker.py:660-664` currently reads:
```python
                    await ws.send_json({
                        "type": "stats",
                        "infer_ms": round(infer_ms, 2),
                        "block_ms": st.BLOCK_MS,
                    })
```
Replace with:
```python
                    stats_payload = {
                        "type": "stats",
                        "infer_ms": round(infer_ms, 2),
                        "block_ms": st.BLOCK_MS,
                        "lock_wait_ms": round(lock_wait_ms, 2),
                    }
                    stats_payload.update(block_timing)
                    await ws.send_json(stats_payload)
```

- [ ] **Step 4: Manual verification against a real Modal deployment**

After `modal deploy modal_deploy/worker.py`, place a real (or test) call and tail the Modal container logs (`modal app logs rvc-worker`) or a client-side debug print of the WS messages to confirm a `"stats"` message during real (non-silent) speech now contains all of: `infer_ms`, `block_ms`, `lock_wait_ms`, `hubert_ms`, `index_ms`, `rmvpe_ms`, `generator_ms`, `postproc_ms`, `total_ms`. Confirm `hubert_ms + index_ms + rmvpe_ms + generator_ms + postproc_ms ≈ total_ms` (should match within rounding) and `total_ms <= infer_ms` (compute time is a subset of the lock-held wall time). This is a manual step for the same reason as Task 1 Step 4 — no GPU locally.

- [ ] **Step 5: Commit**

```bash
git add modal_deploy/worker.py
git commit -m "feat(voice): split GPU-lock wait from compute time, forward TRT stage breakdown over /ws"
```

---

### Task 3: Accumulate every block's stats on the backend worker

**Files:**
- Modify: `backend/pipeline.py:64-119` (`__init__`), `backend/pipeline.py:368-376` (`_on_converter_stats`)
- Test: `backend/test_pipeline.py`

**Interfaces:**
- Consumes: the expanded stats `dict` from Task 2, delivered via `RVCStreamingConverter.on_stats` (unchanged wiring — `backend/pipeline.py:117-119`).
- Produces: `VoiceConversionWorker._call_block_stats: list[dict]` — every stats dict received this call, each with an added `playout_buffer_bytes` key (`int` or `None` if the playout buffer hasn't been created yet). Consumed by Task 4.

- [ ] **Step 1: Write the failing test**

Add to `backend/test_pipeline.py` (after `test_worker_readiness_probe_dedup`, before `test_playout_buffer_smooths_bursty_converter_output`):

```python
async def test_on_converter_stats_accumulates_full_breakdown():
    print("\n--- Testing VoiceConversionWorker._on_converter_stats accumulation ---")

    class _StatsCapableConverter:
        """Only needs an on_stats attribute to exist for VoiceConversionWorker's
        hasattr() check to wire it up -- mirrors RVCStreamingConverter's shape
        without needing a real WS connection."""
        on_stats = None

    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=_StatsCapableConverter(),
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    assert worker._call_block_stats == [], "should start empty"

    # Before _run_conversion_stream ever runs, _playout_buffer doesn't exist yet --
    # playout_buffer_bytes must degrade to None rather than raising.
    worker._on_converter_stats({"infer_ms": 12.3, "block_ms": 320})
    assert worker._call_block_stats[-1]["playout_buffer_bytes"] is None, (
        "expected playout_buffer_bytes=None before the playout buffer is created"
    )

    # Once a playout buffer exists, its current length must be captured alongside
    # whatever fields the server sent (here, the full TRT stage breakdown).
    worker._playout_buffer = bytearray(b"\x00" * 4096)
    worker._on_converter_stats({
        "infer_ms": 58.1, "block_ms": 320, "lock_wait_ms": 3.5,
        "hubert_ms": 10.0, "index_ms": 1.0, "rmvpe_ms": 20.0,
        "generator_ms": 25.0, "postproc_ms": 2.1, "total_ms": 58.1,
    })

    assert len(worker._call_block_stats) == 2, f"expected 2 recorded blocks, got {len(worker._call_block_stats)}"
    second = worker._call_block_stats[1]
    assert second["playout_buffer_bytes"] == 4096, f"expected 4096, got {second['playout_buffer_bytes']}"
    assert second["hubert_ms"] == 10.0 and second["generator_ms"] == 25.0, (
        "expected the full server-reported stage breakdown to be preserved verbatim"
    )
    # infer_ms/block_ms latency-badge behavior must be untouched.
    assert worker._latest_latency_ms == 58.1 + 320, (
        f"existing pipeline_latency_ms computation regressed: {worker._latest_latency_ms}"
    )
    print(f"Accumulated {len(worker._call_block_stats)} block stats rows with playout_buffer_bytes: OK")
    print("_on_converter_stats accumulation test: SUCCESS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m backend.test_pipeline`
Expected: `AttributeError: 'VoiceConversionWorker' object has no attribute '_call_block_stats'` (the test isn't wired into `main()` yet either — do that now so it actually executes):

Add to `main()` in `backend/test_pipeline.py`, right after the `await test_worker_readiness_probe_dedup()` line:
```python
    await test_on_converter_stats_accumulates_full_breakdown()
```

Run again: `python -m backend.test_pipeline` — expect the same `AttributeError`.

- [ ] **Step 3: Add `_call_block_stats` and update `_on_converter_stats`**

In `backend/pipeline.py`, line 105 currently reads:
```python
        self._last_metric_publish_at: float = 0.0
```
Add immediately after it:
```python
        self._last_metric_publish_at: float = 0.0

        # Every stats dict the converter has reported this call, in arrival order,
        # each annotated with playout-buffer occupancy at receipt time. Printed in
        # full (plus aggregates) by _log_call_latency_summary() once the call ends
        # (see stop()) -- this is diagnostic-only, never read during a live call.
        self._call_block_stats: list = []
```

Then replace `_on_converter_stats` (currently `backend/pipeline.py:368-376`):
```python
    def _on_converter_stats(self, stats: dict):
        """Registered as converter.on_stats. Called (synchronously, from whatever
        task is driving the converter's receive loop) whenever the backend reports
        timing for a processed block. This is the latency source of truth for
        converters that support it (RVCStreamingConverter)."""
        infer_ms = stats.get("infer_ms") or 0.0
        block_ms = stats.get("block_ms") or 0.0
        self._latest_latency_ms = float(infer_ms) + float(block_ms)
        self._publish_latency_metric()
```
with:
```python
    def _on_converter_stats(self, stats: dict):
        """Registered as converter.on_stats. Called (synchronously, from whatever
        task is driving the converter's receive loop) whenever the backend reports
        timing for a processed block. This is the latency source of truth for
        converters that support it (RVCStreamingConverter)."""
        infer_ms = stats.get("infer_ms") or 0.0
        block_ms = stats.get("block_ms") or 0.0
        self._latest_latency_ms = float(infer_ms) + float(block_ms)
        self._publish_latency_metric()

        # _playout_buffer only exists once _run_conversion_stream has started
        # (see its class docstring) -- degrade to None rather than raising if a
        # stats message somehow arrives before that.
        playout_buffer = getattr(self, "_playout_buffer", None)
        self._call_block_stats.append({
            **stats,
            "playout_buffer_bytes": len(playout_buffer) if playout_buffer is not None else None,
        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m backend.test_pipeline`
Expected: `_on_converter_stats accumulation test: SUCCESS` printed, and the whole suite ends with `All automated verification tests completed successfully!`.

- [ ] **Step 5: Commit**

```bash
git add backend/pipeline.py backend/test_pipeline.py
git commit -m "feat(voice): accumulate full per-block converter stats + playout occupancy"
```

---

### Task 4: Print the full latency breakdown to Render logs when a call ends

**Files:**
- Modify: `backend/pipeline.py:1-13` (imports), `backend/pipeline.py:221-224` (`stop`)
- Test: `backend/test_pipeline.py`

**Interfaces:**
- Consumes: `VoiceConversionWorker._call_block_stats` (Task 3's output).
- Produces: `VoiceConversionWorker._log_call_latency_summary() -> None` — prints every block's stats plus aggregate avg/median/p95/max per numeric field to stdout. Called automatically by `stop()`.

`stop()` is the single method that runs for every path a call can end on: the natural end (`run_worker_task`'s `finally` block in `backend/main.py:424-427`, which fires when `worker.running` goes false or the task raises) and the explicit end call endpoint (`/api/call/end`, `backend/main.py:857-860`), plus the two other `active_workers` cleanup sites (`backend/main.py:497-500`, `574-585`). Hooking `stop()` covers all of them without duplicating the call site.

**`stop()` legitimately runs TWICE on the `/api/call/end` path**: the endpoint awaits `worker.stop()` directly (main.py:859), which sets `running = False`; within ~1s `run_worker_task`'s keepalive loop (`while worker.running: await asyncio.sleep(1.0)`, main.py:420-421) exits and its `finally` calls `worker.stop()` **again** (main.py:426). The summary must therefore be idempotent — printed at most once per worker via a `_latency_summary_logged` flag — or every explicitly-ended call would log the whole breakdown twice.

- [ ] **Step 1: Write the failing tests**

Add to `backend/test_pipeline.py`, right after the test added in Task 3:

```python
async def test_log_call_latency_summary_prints_header_rows_and_aggregates():
    print("\n--- Testing _log_call_latency_summary output ---")
    import io
    import contextlib

    def make_worker():
        return VoiceConversionWorker(
            room_url="ws://unused",
            token="unused",
            converter=DummyVoiceConverter(),
            suppressor=WebRTCNoiseSuppressor(ns_level=3),
        )

    # No stats recorded at all: must not raise, must say so clearly.
    worker = make_worker()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        worker._log_call_latency_summary()
    assert "No converter stats recorded" in buf.getvalue()
    print("Empty-call case prints a clear no-data line: OK")

    # Populate synthetic per-block rows spanning the full stage breakdown.
    # Fresh worker: the summary is once-per-call (idempotence check below), so
    # reusing the one that already logged the empty-call line would print nothing.
    worker = make_worker()
    worker._call_block_stats = [
        {"infer_ms": 60.0, "block_ms": 320, "lock_wait_ms": 1.0, "hubert_ms": 10.0,
         "index_ms": 1.0, "rmvpe_ms": 20.0, "generator_ms": 27.0, "postproc_ms": 2.0,
         "total_ms": 60.0, "playout_buffer_bytes": 5000},
        {"infer_ms": 80.0, "block_ms": 320, "lock_wait_ms": 5.0, "hubert_ms": 12.0,
         "index_ms": 1.5, "rmvpe_ms": 25.0, "generator_ms": 39.0, "postproc_ms": 2.5,
         "total_ms": 80.0, "playout_buffer_bytes": 6000},
        {"infer_ms": 0.0, "block_ms": 320},  # a silence-bypassed block: no stage keys
    ]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        worker._log_call_latency_summary()
    out = buf.getvalue()

    assert "3 block(s) this call" in out, "expected a header naming the block count"
    assert "block 0:" in out and "block 1:" in out and "block 2:" in out, (
        "expected one printed line per block"
    )
    assert "hubert_ms=10.0" in out, "expected raw per-block field values to be printed verbatim"
    # infer_ms aggregate across all 3 blocks (60, 80, 0): avg=46.67, median=60, max=80
    assert "infer_ms: avg=46.67 median=60.00 p95=80.00 max=80.00 n=3" in out, (
        f"unexpected infer_ms aggregate line, got:\n{out}"
    )
    # hubert_ms aggregate only over the 2 blocks that have it (silence-bypassed block excluded)
    assert "hubert_ms: avg=11.00 median=11.00 p95=12.00 max=12.00 n=2" in out, (
        f"unexpected hubert_ms aggregate line (should exclude the silence-bypassed block), got:\n{out}"
    )
    print("Populated-call case prints per-block rows and correct aggregates: OK")

    # stop() runs TWICE on the /api/call/end path (the endpoint calls it directly,
    # then run_worker_task's finally calls it again once worker.running goes false
    # -- backend/main.py:424-427 + 857-860). The summary must print once per call,
    # not once per stop() invocation.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        worker._log_call_latency_summary()
    assert buf.getvalue() == "", (
        "expected a second _log_call_latency_summary call to print nothing "
        f"(once-per-call idempotence guard), got:\n{buf.getvalue()}"
    )
    print("Second call on the same worker prints nothing (once per call): OK")
    print("_log_call_latency_summary test: SUCCESS")


async def test_stop_logs_summary_before_teardown():
    print("\n--- Testing stop() invokes the latency summary ---")

    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=DummyVoiceConverter(),
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    called = []
    worker._log_call_latency_summary = lambda: called.append(True)

    await worker.stop()

    assert called == [True], "expected stop() to call _log_call_latency_summary exactly once"
    print("stop() invokes _log_call_latency_summary: OK")
    print("stop() latency summary wiring test: SUCCESS")
```

- [ ] **Step 2: Run tests to verify they fail**

Add both new tests to `main()` in `backend/test_pipeline.py`, right after `await test_on_converter_stats_accumulates_full_breakdown()`:
```python
    await test_log_call_latency_summary_prints_header_rows_and_aggregates()
    await test_stop_logs_summary_before_teardown()
```
Run: `python -m backend.test_pipeline`
Expected: `AttributeError: 'VoiceConversionWorker' object has no attribute '_log_call_latency_summary'`.

- [ ] **Step 3: Add the `statistics` import**

`backend/pipeline.py:1-8` currently reads:
```python
import asyncio
import collections
import contextlib
import json
import os
import time
import traceback
from typing import AsyncIterator, Optional
```
Change to:
```python
import asyncio
import collections
import contextlib
import json
import os
import statistics
import time
import traceback
from typing import AsyncIterator, Optional
```

- [ ] **Step 4: Add the once-per-call flag and implement `_log_call_latency_summary`**

First, in `__init__`, extend the block Task 3 added. It currently ends:
```python
        # Every stats dict the converter has reported this call, in arrival order,
        # each annotated with playout-buffer occupancy at receipt time. Printed in
        # full (plus aggregates) by _log_call_latency_summary() once the call ends
        # (see stop()) -- this is diagnostic-only, never read during a live call.
        self._call_block_stats: list = []
```
Add immediately after:
```python
        # stop() can legitimately run twice per call (/api/call/end calls it, then
        # run_worker_task's finally calls it again once running goes false) -- this
        # flag makes the end-of-call summary print at most once per worker.
        self._latency_summary_logged: bool = False
```

Then add this new method to `VoiceConversionWorker` in `backend/pipeline.py`, directly after `_on_converter_stats` (which Task 3 just extended):

```python
    def _log_call_latency_summary(self):
        """Prints every block's converter-reported latency breakdown, plus
        aggregate avg/median/p95/max per numeric field, to stdout -- Render's
        autoDeploy service runs with PYTHONUNBUFFERED=1 (see
        .agents/context/subsystem-notes.md), so this reliably lands in Render
        logs. Called from stop(), which can run twice per call (see
        _latency_summary_logged) -- prints at most once per worker.
        No-op-with-a-note if the converter never reported any stats (e.g.
        DummyVoiceConverter, or a call that ended before any block was
        converted)."""
        if self._latency_summary_logged:
            return
        self._latency_summary_logged = True

        stats = self._call_block_stats
        if not stats:
            print("[Worker][LatencySummary] No converter stats recorded for this call.")
            return

        print(f"[Worker][LatencySummary] ==== {len(stats)} block(s) this call ====")
        for i, row in enumerate(stats):
            fields = ", ".join(f"{k}={v}" for k, v in row.items())
            print(f"[Worker][LatencySummary] block {i}: {fields}")

        numeric_fields = [
            "infer_ms", "block_ms", "lock_wait_ms", "hubert_ms", "index_ms",
            "rmvpe_ms", "generator_ms", "postproc_ms", "total_ms",
            "playout_buffer_bytes",
        ]
        for field in numeric_fields:
            values = [row[field] for row in stats if isinstance(row.get(field), (int, float))]
            if not values:
                continue
            values_sorted = sorted(values)
            p95_index = min(len(values_sorted) - 1, int(round(0.95 * (len(values_sorted) - 1))))
            print(
                f"[Worker][LatencySummary] {field}: "
                f"avg={statistics.mean(values):.2f} "
                f"median={statistics.median(values):.2f} "
                f"p95={values_sorted[p95_index]:.2f} "
                f"max={max(values):.2f} "
                f"n={len(values)}"
            )
```

- [ ] **Step 5: Wire it into `stop()`**

`backend/pipeline.py:221-224` currently reads:
```python
    async def stop(self):
        """Disconnects the worker and stops all background tasks."""
        self.running = False
        self.stop_pipeline()
```
Change to:
```python
    async def stop(self):
        """Disconnects the worker and stops all background tasks."""
        self._log_call_latency_summary()
        self.running = False
        self.stop_pipeline()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m backend.test_pipeline`
Expected: `_log_call_latency_summary test: SUCCESS`, `stop() latency summary wiring test: SUCCESS`, and the full suite still ends with `All automated verification tests completed successfully!`.

- [ ] **Step 7: Commit**

```bash
git add backend/pipeline.py backend/test_pipeline.py
git commit -m "feat(voice): print full per-block latency breakdown to Render logs on call end"
```

---

### Task 5: Deploy and confirm end-to-end on a live call

**Files:** none (deploy + manual verification only)

**Interfaces:** none — this task validates Tasks 1-4 together against real infrastructure.

- [ ] **Step 1: Deploy the Modal worker**

```bash
modal deploy modal_deploy/worker.py
```
Expected: deploy succeeds, `/health` reports `{"status":"ready",...}` after warm-up (see `.agents/context/subsystem-notes.md` for expected cold-start timing, ~75s+one-time TRT engine warmup if the volume cache was purged).

- [ ] **Step 2: Deploy/restart the backend**

Push the `backend/pipeline.py` changes to `main` (Render's `autoDeploy: commit` picks it up automatically — **do not push while a live test call is in progress**, see `.agents/context/stack-and-rules.md`'s "Don't push to `main` mid-call" invariant).

- [ ] **Step 3: Place one test call and inspect Render logs**

Run a normal test call end-to-end (per `LATENCY.md` §3's Spawn Bot / Join as Agent / Join as Listener flow, or a real outbound/inbound call), then end it (hang up, or `POST /api/call/end`). Pull Render logs for that time window (Render dashboard, or Render MCP per `.agents/context/subsystem-notes.md`'s "Call-analysis 3-point capture" method) and confirm:
- One `[Worker][LatencySummary] ==== N block(s) this call ====` line appears exactly once, after the call ended (not mid-call).
- Each `block i:` line includes `infer_ms`, `block_ms`, `lock_wait_ms`, and (for non-silence blocks) `hubert_ms`/`index_ms`/`rmvpe_ms`/`generator_ms`/`postproc_ms`/`total_ms`/`playout_buffer_bytes`.
- The aggregate lines below it (`infer_ms: avg=... median=... p95=... max=... n=...`, etc.) are present for every field that had at least one value.

- [ ] **Step 4: Sanity-check the numbers against the known budget**

Cross-reference against `LATENCY.md`'s C3/Phase-2D benchmark figures (median ~54-66ms TRT inference) and `.agents/context/subsystem-notes.md`'s TRT section — `generator_ms` should be the largest single stage (it's the actual synthesis network), `index_ms` should be small (CPU FAISS lookup on a small `k=8` search), and `lock_wait_ms` should be near-zero for a single active call (it only grows under queued/concurrent load, which the 1-concurrent-session MVP shouldn't produce). If any stage is wildly larger than expected, that's the actionable signal this whole feature exists to surface — note it in `.agents/context/subsystem-notes.md`'s TRT section rather than silently reacting to it here.

- [ ] **Step 5: No commit** — this task is deploy + observation only; nothing here produces a code change (unless Step 4 turns up something actionable, in which case that's new, separately-scoped follow-up work).

---

## Self-Review

**Spec coverage:**
- "prepare implementation plan" → this document.
- "print all the values in the render logs after the call got ended" → Task 4 (`_log_call_latency_summary`, wired into `stop()`, prints every block's full dict, not just aggregates).
- The underlying gap identified in conversation (no per-stage model timing, GPU-lock wait conflated with compute, no playout-buffer-occupancy correlation) → Tasks 1-3.

**Placeholder scan:** no TBD/TODO, every step has literal before/after code, every test has real assertions with computed expected values (aggregates hand-calculated above, not asserted loosely).

**Type consistency:** `TRTVoicePipeline.last_block_timing` (Task 1) → read via `getattr(getattr(engine, "trt_pipe", None), "last_block_timing", None) or {}` in `worker.py` (Task 2 — the dict lives on `engine.trt_pipe`, since `RVCEngine.convert_block` at worker.py:388-392 delegates there; reading it off `engine` itself would silently always return `{}`) → merged into the `"stats"` WS message → received as `stats: dict` in `_on_converter_stats` (Task 3, already generic) → stored verbatim in `_call_block_stats` → iterated by field name in `_log_call_latency_summary` (Task 4). Field names (`hubert_ms`, `index_ms`, `rmvpe_ms`, `generator_ms`, `postproc_ms`, `total_ms`, `lock_wait_ms`, `infer_ms`, `block_ms`, `playout_buffer_bytes`) are identical at every hop — verified against each task's code.

**Review-pass fixes (2026-07-11, checked against live code):**
1. **Wrong attribute owner for the stage breakdown** — first draft read `engine.last_block_timing`, but the `/ws` handler's `engine` is `RVCEngine` and Task 1 sets the dict on `TRTVoicePipeline` (= `engine.trt_pipe`, `None` on the ONNX-CUDA fallback). Would have silently shipped an empty breakdown on every block. Fixed in Task 2 Step 2 with the nested-`getattr` read.
2. **Double summary print on explicit call end** — `worker.stop()` runs twice on the `/api/call/end` path (endpoint call at main.py:859, then `run_worker_task`'s `finally` at main.py:426 once `running` goes false). Fixed with the `_latency_summary_logged` once-per-worker guard (Task 4), plus an idempotence assertion in the summary test.
