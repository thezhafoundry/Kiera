# Adaptive Per-Call Pitch Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed `RVC_MALE_PITCH_SHIFT` constant with a per-call F0-derived pitch lock so the converted voice always lands on the model's trained pitch center (~208 Hz), per the approved spec `docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md`.

**Architecture:** A pure-numpy `PitchLock` aggregator lives in the Modal worker's `/ws` session. Engines (`trt_pipeline.py` TRT path, `worker.py` ONNX fallback) expose their existing pre-shift F0 track via an optional `f0_sink` out-param. Once ≥2 s of voiced speech is seen, the session locks `shift = 12·log2(target_f0 / median_F0)` (float semitones, clamped ±12) and reports it via the existing `stats` messages; `RVCStreamingConverter` caches it so WS reconnects resume the locked identity instead of re-detecting.

**Tech Stack:** Python 3.10+, numpy, FastAPI WebSockets (Modal worker), `websockets` client (backend). No new dependencies.

## Global Constraints

- Never-raw invariant untouched: on any failure, output is silence/error — never unconverted audio.
- Pitch shift is a **float** semitone value clamped to **±12**; voiced plausibility window is **60–400 Hz**; voiced gate is **2.0 s**; target default **208 Hz**.
- Env defaults: `RVC_ADAPTIVE_PITCH=1` (kill switch `0` restores exact legacy fixed-shift behavior), `RVC_TARGET_F0=208`.
- `pitch_shift == -1` in the WS config keeps the **legacy auto-detect path** and disables adaptation (adaptive requires a concrete prior).
- HTTP `POST /convert` signature and behavior unchanged.
- All tests are plain assert/print style (NO pytest), run **from the repo root** with `.venv\Scripts\python.exe`.
- Every new local module the Modal worker imports MUST be mounted with `.add_local_python_source(...)` in `modal_deploy/modal_defs.py` (Modal does not auto-trace imports — 2026-07-03 `streaming.py` incident).
- `modal deploy` is **USER-AUTHORIZED ONLY** — no task in this plan deploys the live worker. `modal run` (ephemeral, billed, does not touch the deployed worker) is allowed where stated.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

**Deploy-ordering safety (why pushing to main early is safe):** Render auto-deploys the backend on every push, but an old worker ignores the new `adaptive_pitch`/`target_f0` config keys (it only reads known keys), and a new worker treats a missing `adaptive_pitch` as `False`. Both mixed-version directions behave exactly like today.

---

### Task 1: `PitchLock` aggregator + unit tests

**Files:**
- Create: `modal_deploy/pitch_lock.py`
- Test: `modal_deploy/test_pitch_lock.py`

**Interfaces:**
- Consumes: nothing (pure numpy + stdlib, importable without Modal/GPU — same contract as `modal_deploy/streaming.py`).
- Produces: `PitchLock(prior_shift: float, target_f0: float = 208.0, enabled: bool = True, min_voiced_seconds: float = 2.0)` with:
  - `add_block(f0_values, block_seconds: float) -> bool` — returns True iff this call locked
  - properties `locked: bool`, `shift: float`, attributes `voiced_seconds: float`, `locked_median_f0: float | None`, `prior_shift: float`
  - module constants `DEFAULT_TARGET_F0_HZ = 208.0`, `MAX_ABS_SHIFT_SEMITONES = 12.0`, `F0_PLAUSIBLE_MIN_HZ = 60.0`, `F0_PLAUSIBLE_MAX_HZ = 400.0`

- [ ] **Step 1: Write the failing tests**

Create `modal_deploy/test_pitch_lock.py`:

```python
"""Pure-numpy/stdlib tests for modal_deploy/pitch_lock.py.

Same contract as test_streaming.py: importable and runnable in a plain CPython
environment (no modal package, no GPU). Plain assert/print style, no pytest.

Run: python -m modal_deploy.test_pitch_lock
"""
import numpy as np

from modal_deploy.pitch_lock import PitchLock, MAX_ABS_SHIFT_SEMITONES


def _voiced_block(f0_hz: float, n_frames: int = 100) -> np.ndarray:
    return np.full(n_frames, f0_hz, dtype=np.float64)


def test_prior_until_locked():
    print("\n--- PitchLock: shift stays at prior until the voiced gate is reached ---")
    lock = PitchLock(prior_shift=7.0, target_f0=208.0)
    assert lock.shift == 7.0 and not lock.locked
    locked = lock.add_block(_voiced_block(152.4), block_seconds=1.0)  # 1s < 2s gate
    assert not locked and not lock.locked and lock.shift == 7.0
    print("prior-until-locked: SUCCESS")


def test_locks_on_median_and_freezes():
    print("\n--- PitchLock: locks 12*log2(target/median) once >=2s voiced, then freezes ---")
    lock = PitchLock(prior_shift=7.0, target_f0=208.0)
    lock.add_block(_voiced_block(152.4), block_seconds=1.0)
    locked = lock.add_block(_voiced_block(152.4), block_seconds=1.0)
    assert locked and lock.locked
    # 12*log2(208/152.4) = +5.39 st (the 2026-07-13 call-2 numbers)
    assert abs(lock.shift - 5.39) < 0.05, f"expected ~+5.39, got {lock.shift}"
    assert abs(lock.locked_median_f0 - 152.4) < 1e-6
    # After lock: new (different) F0 must never move the shift again
    lock.add_block(_voiced_block(300.0), block_seconds=5.0)
    assert abs(lock.shift - 5.39) < 0.05
    print("median lock + freeze: SUCCESS")


def test_unvoiced_and_implausible_excluded():
    print("\n--- PitchLock: zeros/implausible F0 add neither samples nor voiced credit ---")
    lock = PitchLock(prior_shift=7.0, target_f0=208.0, min_voiced_seconds=1.0)
    lock.add_block(np.zeros(100), block_seconds=10.0)           # all unvoiced
    lock.add_block(_voiced_block(30.0), block_seconds=10.0)     # below 60 Hz window
    lock.add_block(_voiced_block(500.0), block_seconds=10.0)    # above 400 Hz window
    assert not lock.locked and lock.voiced_seconds == 0.0
    # half-voiced block only credits half its duration
    half = np.concatenate([np.zeros(50), _voiced_block(150.0, 50)])
    lock.add_block(half, block_seconds=1.0)
    assert not lock.locked and abs(lock.voiced_seconds - 0.5) < 1e-9
    print("voiced-only filtering: SUCCESS")


def test_clamp():
    print("\n--- PitchLock: computed shift clamps to +/-12 st ---")
    lock = PitchLock(prior_shift=0.0, target_f0=208.0, min_voiced_seconds=0.5)
    lock.add_block(_voiced_block(60.0), block_seconds=1.0)  # 12*log2(208/60)=+21.5 -> clamp
    assert lock.locked and lock.shift == MAX_ABS_SHIFT_SEMITONES
    print("clamp: SUCCESS")


def test_disabled_is_inert():
    print("\n--- PitchLock: enabled=False never accumulates or locks ---")
    lock = PitchLock(prior_shift=7.0, target_f0=208.0, enabled=False)
    lock.add_block(_voiced_block(152.4), block_seconds=60.0)
    assert not lock.locked and lock.shift == 7.0 and lock.voiced_seconds == 0.0
    print("disabled inert: SUCCESS")


def main():
    print("Running modal_deploy/pitch_lock.py verification tests...")
    test_prior_until_locked()
    test_locks_on_median_and_freezes()
    test_unvoiced_and_implausible_excluded()
    test_clamp()
    test_disabled_is_inert()
    print("\nAll pitch-lock tests completed successfully!")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run (repo root): `.venv\Scripts\python.exe -m modal_deploy.test_pitch_lock`
Expected: FAIL with `ModuleNotFoundError: No module named 'modal_deploy.pitch_lock'`

- [ ] **Step 3: Implement `modal_deploy/pitch_lock.py`**

```python
"""Per-session adaptive pitch lock (spec: docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md).

Accumulates the engine's raw (pre-shift) F0 track across streaming blocks and,
once enough voiced speech has been seen, locks a semitone shift that lands the
speaker's median F0 on the model's trained center. Deliberately imports only
numpy + stdlib (no Modal, no GPU) -- same contract as streaming.py -- so it is
unit-testable in a plain CPython environment.

Why this is NOT the reverted 2026-07-03 auto-detect:
- median over >= min_voiced_seconds of voiced frames, not a one-shot 1s window;
- voiced-only (60-400 Hz plausibility window): silence contributes nothing;
- locks once per session and never moves again;
- the locked value is reported to the client (stats "locked_pitch") so a WS
  reconnect RESUMES it instead of re-detecting mid-call.
"""
import math

import numpy as np

F0_PLAUSIBLE_MIN_HZ = 60.0
F0_PLAUSIBLE_MAX_HZ = 400.0
DEFAULT_TARGET_F0_HZ = 208.0
DEFAULT_MIN_VOICED_SECONDS = 2.0
MAX_ABS_SHIFT_SEMITONES = 12.0


class PitchLock:
    """Lock-once-per-session pitch shift derived from measured voiced F0.

    `shift` is `prior_shift` until locked, then the locked value forever.
    """

    def __init__(
        self,
        prior_shift: float,
        target_f0: float = DEFAULT_TARGET_F0_HZ,
        enabled: bool = True,
        min_voiced_seconds: float = DEFAULT_MIN_VOICED_SECONDS,
    ):
        self.prior_shift = float(prior_shift)
        self.target_f0 = float(target_f0)
        self.enabled = bool(enabled)
        self.min_voiced_seconds = float(min_voiced_seconds)
        self.voiced_seconds = 0.0
        self.locked_median_f0 = None
        self._locked_shift = None
        self._voiced_f0 = []

    @property
    def locked(self) -> bool:
        return self._locked_shift is not None

    @property
    def shift(self) -> float:
        return self._locked_shift if self._locked_shift is not None else self.prior_shift

    def add_block(self, f0_values, block_seconds: float) -> bool:
        """Feed one block's raw (pre-shift) F0 track (Hz; unvoiced frames == 0).

        `block_seconds` is the duration of NEW audio in the block (the fresh
        320 ms slice, not the +context window) -- voiced credit is
        voiced_fraction * block_seconds, which keeps the 2 s gate honest even
        though consecutive infer windows re-analyze overlapping context audio.
        Returns True iff this call caused the lock.
        """
        if not self.enabled or self.locked:
            return False
        arr = np.asarray(f0_values, dtype=np.float64).reshape(-1)
        if arr.size == 0:
            return False
        voiced = arr[(arr >= F0_PLAUSIBLE_MIN_HZ) & (arr <= F0_PLAUSIBLE_MAX_HZ)]
        if voiced.size == 0:
            return False
        self._voiced_f0.extend(voiced.tolist())
        self.voiced_seconds += (voiced.size / arr.size) * float(block_seconds)
        if self.voiced_seconds < self.min_voiced_seconds:
            return False
        median_f0 = float(np.median(self._voiced_f0))
        shift = 12.0 * math.log2(self.target_f0 / median_f0)
        self._locked_shift = float(
            np.clip(shift, -MAX_ABS_SHIFT_SEMITONES, MAX_ABS_SHIFT_SEMITONES)
        )
        self.locked_median_f0 = median_f0
        self._voiced_f0 = []  # lock is final; free the accumulator
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m modal_deploy.test_pitch_lock`
Expected: all 5 tests print SUCCESS, ends with `All pitch-lock tests completed successfully!`

- [ ] **Step 5: Commit**

```bash
git add modal_deploy/pitch_lock.py modal_deploy/test_pitch_lock.py
git commit -m "feat(worker): PitchLock aggregator for adaptive per-call pitch

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Engine F0 exposure (`f0_sink`) + Modal mount

**Files:**
- Modify: `modal_deploy/modal_defs.py:61-74` (mount `pitch_lock` into both images)
- Modify: `modal_deploy/trt_pipeline.py:176-203` (`_f0` returns pre-shift track; `convert_block` gains `f0_sink`)
- Modify: `modal_deploy/worker.py:251-259` (`run_conversion` gains `f0_sink`), `modal_deploy/worker.py:318-324` (capture point), `modal_deploy/worker.py:377-401` (`RVCEngine.convert_block` passthrough)

**Interfaces:**
- Consumes: nothing new.
- Produces: `RVCEngine.convert_block(pcm_int16, pitch: float = 0, index_rate=0.75, rms_mix_rate=0.75, protect=0.33, f0_sink: list | None = None) -> bytes`. When `f0_sink` is a list, exactly one `np.float32` array of the block's **pre-shift** F0 (Hz, unvoiced == 0) is appended per call. Same `f0_sink` kwarg on `TRTVoicePipeline.convert_block` and `RVCEngine.run_conversion` (kwarg-only, default `None` — HTTP `/convert` unaffected).

- [ ] **Step 1: Mount `pitch_lock` in `modal_defs.py`**

In `modal_deploy/modal_defs.py`, change both final image chains:

```python
image = (
    _build_base
    .add_local_dir("RVC", remote_path="/root/rvc", ignore=_RVC_IGNORE)
    .add_local_python_source("streaming")
    .add_local_python_source("pitch_lock")
    .add_local_python_source("modal_defs")
)

trt_image = (
    _trt_build_base
    .add_local_dir("RVC", remote_path="/root/rvc", ignore=_RVC_IGNORE)
    .add_local_python_source("streaming")
    .add_local_python_source("trt_pipeline")
    .add_local_python_source("pitch_lock")
    .add_local_python_source("modal_defs")
)
```

- [ ] **Step 2: Expose pre-shift F0 in `trt_pipeline.py`**

Replace `_f0` (lines 176–199) with (changes: interp moved BEFORE the shift — linear interp commutes with the scalar factor; `f0_raw` captured on the GEN_FRAMES grid; float `pitch_shift`; third return value):

```python
    def _f0(self, audio_f32: np.ndarray, pitch_shift: float, filter_radius: int):
        """audio [PADDED_IN] float32 -> (pitch int64 [GEN_FRAMES], pitchf f32 [GEN_FRAMES],
        f0_raw f32 [GEN_FRAMES] -- the PRE-shift F0 track in Hz, unvoiced == 0)."""
        import torch
        with torch.no_grad():
            x = torch.from_numpy(audio_f32).float().to(self.device)[None, :]
            mel = self.mel(x, center=True)                 # [1, 128, MEL_FRAMES]
        mel_np = mel.cpu().numpy().astype(np.float32)
        pad = MEL_FRAMES_PADDED - mel_np.shape[-1]
        if pad > 0:
            mel_np = np.pad(mel_np, ((0, 0), (0, 0), (0, pad)), mode="constant")
        hidden = self.s_rmvpe.run(None, {"mel": mel_np})[0]
        f0 = decode_f0(hidden)                             # [MEL_FRAMES] Hz
        if filter_radius >= 2:
            from scipy.signal import medfilt
            voiced = f0 > 0
            ks = filter_radius if filter_radius % 2 == 1 else 3
            f0_f = medfilt(f0, kernel_size=ks)
            f0 = np.where(voiced & (f0_f > 0), f0_f, f0)
        # resample MEL_FRAMES(273) f0 points onto the GEN_FRAMES(270) grid BEFORE
        # applying the shift: linear interp commutes with the scalar 2**(shift/12)
        # factor, and capturing f0_raw here hands PitchLock the pre-shift track.
        f0 = np.interp(np.linspace(0, 1, GEN_FRAMES), np.linspace(0, 1, len(f0)), f0)
        f0_raw = f0.astype(np.float32)
        f0 = f0 * (2.0 ** (pitch_shift / 12.0))
        pitchf = f0.astype(np.float32)
        pitch = f0_to_coarse(f0)
        return pitch, pitchf, f0_raw
```

In `convert_block` (line 201), change the signature and the `_f0` call site (line 236):

```python
    def convert_block(self, pcm_int16, pitch_shift: float = 0, index_rate: float = 0.75,
                      rms_mix_rate: float = 0.75, protect: float = 0.33,
                      filter_radius: int = 3, f0_sink: list = None) -> np.ndarray:
```

```python
        # ---- Engine 3: RMVPE F0 ----
        pitch, pitchf, f0_raw = self._f0(audio, pitch_shift, filter_radius)
        if f0_sink is not None:
            f0_sink.append(f0_raw)
        t_rmvpe = time.perf_counter()
```

- [ ] **Step 3: Expose pre-shift F0 in `worker.py`'s ONNX fallback and dispatcher**

`run_conversion` signature (line 251–259) — add the kwarg at the end and loosen `pitch` to float:

```python
    def run_conversion(
        self,
        audio_bytes: bytes,
        pitch: float = -1,
        index_rate: float = 0.75,
        filter_radius: int = 3,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
        f0_sink: list = None,
    ) -> bytes:
```

At the capture point (currently line 324), insert the sink append BEFORE the shift multiply:

```python
        if f0_sink is not None:
            f0_sink.append(pitchf.astype(np.float32, copy=True))
        pitchf = pitchf * (2 ** (pitch / 12))
```

`RVCEngine.convert_block` (lines 377–401) — pass the sink through both paths:

```python
    def convert_block(
        self,
        pcm_int16,                      # np.int16 array, 16 kHz, <= CANONICAL_IN samples
        pitch: float = 0,
        index_rate: float = 0.75,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
        f0_sink: list = None,
    ) -> bytes:
        """Single streaming-block conversion. TRT path when loaded, else the
        existing PyTorch run_conversion via WAV bytes. Returns 48 kHz int16 bytes,
        ~3x the input duration either way. When `f0_sink` is a list, the block's
        pre-shift F0 track (Hz, unvoiced == 0) is appended to it."""
        if self.trt_pipe is not None:
            out = self.trt_pipe.convert_block(
                pcm_int16, pitch, index_rate, rms_mix_rate, protect,
                f0_sink=f0_sink,
            )
            return out.tobytes()
        # PyTorch fallback: wrap in WAV and call run_conversion
        try:
            from modal_deploy import streaming as _st
        except ImportError:
            import streaming as _st
        return self.run_conversion(
            _st.pcm16_to_wav_bytes(pcm_int16), pitch, index_rate, 3,
            rms_mix_rate, protect, f0_sink=f0_sink,
        )
```

- [ ] **Step 4: Verify nothing local broke**

Run:
```
.venv\Scripts\python.exe -m modal_deploy.test_streaming
.venv\Scripts\python.exe -m modal_deploy.test_pitch_lock
.venv\Scripts\python.exe -m py_compile modal_deploy/trt_pipeline.py modal_deploy/worker.py modal_deploy/modal_defs.py
```
Expected: both test modules end with their success line; `py_compile` exits 0 with no output. (The GPU paths themselves are exercised on Modal in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add modal_deploy/modal_defs.py modal_deploy/trt_pipeline.py modal_deploy/worker.py
git commit -m "feat(worker): engines expose pre-shift F0 via f0_sink; mount pitch_lock

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `/ws` session integration (lock, stats, log)

**Files:**
- Modify: `modal_deploy/worker.py` — top-of-file imports (next to the existing `streaming` import), config parse (`ws_stream`, lines ~559-563), session state (~573-578), conversion call (~619-653), stats payload (~672-679)

**Interfaces:**
- Consumes: `PitchLock` (Task 1), `engine.convert_block(..., f0_sink=...)` (Task 2), existing `st.SAMPLE_RATE_IN`.
- Produces: WS protocol — config accepts `"adaptive_pitch": bool` (default False) and `"target_f0": float` (default 208.0); `stats` messages gain `"locked_pitch": float` (rounded to 2 dp) once locked. Worker log line prefix `[AdaptivePitch]` on lock.

- [ ] **Step 1: Import `pitch_lock` in `worker.py`**

Find the existing streaming import near the top of `worker.py` (`from modal_deploy import streaming as st` inside a try/except ImportError) and add the same pattern directly below it:

```python
try:
    from modal_deploy import pitch_lock as pl
except ImportError:   # inside container: modal_deploy package not present
    import pitch_lock as pl
```

- [ ] **Step 2: Parse the new config fields in `ws_stream`**

Replace lines 560–563:

```python
            cfg_pitch = float(cfg.get("pitch_shift", -1))
            index_rate = float(cfg.get("index_rate", 0.75))
            rms_mix_rate = float(cfg.get("rms_mix_rate", 0.75))
            protect = float(cfg.get("protect", 0.33))
            adaptive_cfg = bool(cfg.get("adaptive_pitch", False))
            target_f0 = float(cfg.get("target_f0", pl.DEFAULT_TARGET_F0_HZ))
```

- [ ] **Step 3: Create the per-session lock**

Directly after `session_pitch = None if cfg_pitch == -1 else cfg_pitch` (line 578), add:

```python
            # Adaptive per-call pitch lock (spec 2026-07-13): needs a concrete
            # prior — pitch_shift == -1 keeps the legacy auto-detect path and
            # disables adaptation.
            lock = pl.PitchLock(
                prior_shift=cfg_pitch if cfg_pitch != -1 else 0.0,
                target_f0=target_f0,
                enabled=adaptive_cfg and cfg_pitch != -1,
            )
```

- [ ] **Step 4: Drive each block's pitch from the lock and feed it back**

Replace the non-silent conversion branch (currently lines 619–653, from `else:` after the silence bypass through the `except` block's `continue`) with:

```python
                    else:
                        # Resolve the session pitch once from the first non-silent audio.
                        if session_pitch is None:
                            probe_wav = st.pcm16_to_wav_bytes(infer_input)
                            session_pitch = await asyncio.to_thread(
                                engine._auto_detect_pitch, probe_wav
                            )
                        pitch_for_block = lock.shift if lock.enabled else session_pitch
                        f0_sink = [] if (lock.enabled and not lock.locked) else None
                        try:
                            t_wait_start = time.perf_counter()
                            async with _gpu_lock:  # single-tenant GPU
                                t_compute_start = time.perf_counter()
                                out_bytes = await asyncio.to_thread(
                                    engine.convert_block,
                                    infer_input,
                                    pitch_for_block,
                                    index_rate,
                                    rms_mix_rate,
                                    protect,
                                    f0_sink=f0_sink,
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
                            # Inference failed: report, emit NOTHING (never raw),
                            # keep the session alive, hold the pending tail as-is.
                            traceback.print_exc()
                            await ws.send_json({"type": "error", "message": str(e)})
                            continue

                        if f0_sink:
                            if lock.add_block(
                                np.concatenate(f0_sink),
                                block_seconds=len(block) / st.SAMPLE_RATE_IN,
                            ):
                                print(
                                    f"[AdaptivePitch] locked shift={lock.shift:+.2f} st "
                                    f"(median F0={lock.locked_median_f0:.1f}Hz → target "
                                    f"{target_f0:.0f}Hz, {lock.voiced_seconds:.1f}s voiced, "
                                    f"prior {lock.prior_shift:+.1f})"
                                )
```

(The lines after this — `out = np.frombuffer(out_bytes, ...)` and the `trim_context` call — are unchanged and stay below the inserted `if f0_sink:` block.)

- [ ] **Step 5: Report the lock in stats**

After the `stats_payload` dict is built (currently lines 672–677) and before `stats_payload.update(block_timing)`, add:

```python
                    if lock.locked:
                        stats_payload["locked_pitch"] = round(lock.shift, 2)
```

- [ ] **Step 6: Verify locally**

Run:
```
.venv\Scripts\python.exe -m py_compile modal_deploy/worker.py
.venv\Scripts\python.exe -m modal_deploy.test_pitch_lock
```
Expected: exit 0; tests pass. (End-to-end `/ws` behavior is exercised on Modal in Task 6.)

- [ ] **Step 7: Commit**

```bash
git add modal_deploy/worker.py
git commit -m "feat(worker): per-session adaptive pitch lock in /ws (stats locked_pitch)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Backend converter — config fields + locked-pitch resume

**Files:**
- Modify: `backend/converters/rvc_stream.py:59-110` (constructor + `_config_payload`), `backend/converters/rvc_stream.py:300-320` (`_handle_incoming` stats branch)
- Test: `backend/test_pipeline.py` (new test + register in `main()`)

**Interfaces:**
- Consumes: worker stats `"locked_pitch"` (Task 3).
- Produces: `RVCStreamingConverter(..., pitch_shift: float = -1, ..., adaptive_pitch: bool = False, target_f0: float = 208.0)`; `_config_payload()` JSON gains `"adaptive_pitch"` and `"target_f0"`. After a `locked_pitch` stats message on an adaptive converter: `self.pitch_shift == locked value`, `self.adaptive_pitch == False` (so every reconnect handshake resumes the locked identity).

- [ ] **Step 1: Write the failing test**

Add to `backend/test_pipeline.py` (after `test_rvc_streaming_converter_buffer_cap_drop_oldest`):

```python
async def test_rvc_streaming_adaptive_config_and_locked_pitch_resume():
    print("\n--- Testing RVCStreamingConverter adaptive-pitch config + locked_pitch resume ---")
    import json as _json

    converter = RVCStreamingConverter(
        ws_url="ws://127.0.0.1:1/ws",  # never actually connected in this test
        pitch_shift=7,
        adaptive_pitch=True,
        target_f0=208.0,
    )
    payload = _json.loads(converter._config_payload())
    assert payload["adaptive_pitch"] is True
    assert payload["target_f0"] == 208.0
    assert payload["pitch_shift"] == 7

    # Server reports the per-call lock via stats: the converter must adopt it so
    # the NEXT (reconnect) config resumes the locked identity instead of
    # re-adapting — the 2026-07-03 auto-detect revert was exactly about
    # re-detection on reconnect changing identity mid-call.
    await converter._handle_incoming(None, _json.dumps(
        {"type": "stats", "infer_ms": 55.0, "locked_pitch": 5.39}
    ))
    assert converter.pitch_shift == 5.39
    assert converter.adaptive_pitch is False
    payload = _json.loads(converter._config_payload())
    assert payload["pitch_shift"] == 5.39
    assert payload["adaptive_pitch"] is False

    # Non-adaptive converters ignore locked_pitch (defensive; server won't send it).
    fixed = RVCStreamingConverter(ws_url="ws://127.0.0.1:1/ws", pitch_shift=7)
    await fixed._handle_incoming(None, _json.dumps(
        {"type": "stats", "infer_ms": 55.0, "locked_pitch": 3.0}
    ))
    assert fixed.pitch_shift == 7
    print("RVCStreamingConverter adaptive config + locked_pitch resume: SUCCESS")
```

Register it in `main()` at the bottom of `backend/test_pipeline.py`, after the `test_rvc_streaming_converter_buffer_cap_drop_oldest()` line:

```python
    await test_rvc_streaming_adaptive_config_and_locked_pitch_resume()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m backend.test_pipeline`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'adaptive_pitch'`

- [ ] **Step 3: Implement in `rvc_stream.py`**

Constructor (lines 59–80) — loosen `pitch_shift` to float, add the two kwargs and store them:

```python
    def __init__(
        self,
        endpoint_url: str = "",
        ws_url: str = "",
        api_key: str = "",
        pitch_shift: float = -1,
        index_rate: float = 0.75,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
        connect_timeout: float = 10.0,
        adaptive_pitch: bool = False,
        target_f0: float = 208.0,
    ):
```

and after `self.pitch_shift = pitch_shift`:

```python
        self.adaptive_pitch = adaptive_pitch
        self.target_f0 = target_f0
```

`_config_payload` (lines 97–104):

```python
    def _config_payload(self) -> str:
        return json.dumps({
            "type": "config",
            "pitch_shift": self.pitch_shift,
            "index_rate": self.index_rate,
            "rms_mix_rate": self.rms_mix_rate,
            "protect": self.protect,
            "adaptive_pitch": self.adaptive_pitch,
            "target_f0": self.target_f0,
        })
```

`_handle_incoming` stats branch (line 313) — insert BEFORE the `on_stats` call:

```python
        if msg_type == "stats":
            locked = data.get("locked_pitch")
            if locked is not None and self.adaptive_pitch:
                # Adopt the server's per-call locked shift so any WS reconnect
                # RESUMES this identity (concrete pitch + adaptive_pitch=False in
                # the next _config_payload) instead of re-detecting mid-call.
                self.pitch_shift = float(locked)
                self.adaptive_pitch = False
                logger.info(
                    "[RVCStreamingConverter] adaptive pitch locked at %+.2f st — "
                    "reconnects will resume this value", self.pitch_shift,
                )
            if self.on_stats is not None:
```

(Note: `locked_pitch` deliberately stays in the dict passed to `on_stats`, so it shows up in the pipeline's `[Worker][LatencySummary]` lines for post-call forensics.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m backend.test_pipeline`
Expected: all tests pass, including the new one; ends with `All automated verification tests completed successfully!`

- [ ] **Step 5: Commit**

```bash
git add backend/converters/rvc_stream.py backend/test_pipeline.py
git commit -m "feat(backend): adaptive-pitch config fields + locked_pitch reconnect resume

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Backend env wiring (`main.py`)

**Files:**
- Modify: `backend/main.py:80` (env block, directly after `RVC_MALE_PITCH_SHIFT`), `backend/main.py:377-390` (`_do_start_bot`)

**Interfaces:**
- Consumes: `RVCStreamingConverter(..., adaptive_pitch=, target_f0=)` (Task 4).
- Produces: module-level `RVC_ADAPTIVE_PITCH: bool` (env default `"1"`), `RVC_TARGET_F0: float` (env default `"208"`).

- [ ] **Step 1: Add the env vars**

Directly after the `RVC_MALE_PITCH_SHIFT` line (line 80):

```python
# Adaptive per-call pitch lock (spec: docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md).
# The fixed RVC_MALE_PITCH_SHIFT above goes stale whenever the agent's natural
# delivery shifts (2026-07-13: live agent F0 152-158Hz vs the 137-138Hz that +7
# was calibrated on -> output landed 1.5-2 st above the model center = the
# documented wrong-identity voice). When enabled, the GPU worker measures the
# live voiced F0 (median over >=2s of voiced RMVPE frames) and locks
# shift = 12*log2(RVC_TARGET_F0 / median) once per call; the gender-toggle
# shift becomes only the pre-lock prior. Set to 0 to disable (exact legacy
# fixed-shift behavior).
RVC_ADAPTIVE_PITCH = os.getenv("RVC_ADAPTIVE_PITCH", "1") == "1"
# The trained model's F0 center in Hz that the adaptive lock targets
# (mi-test: ~208, measured 2026-07-08 from a known-good output).
RVC_TARGET_F0 = float(os.getenv("RVC_TARGET_F0", "208"))
```

- [ ] **Step 2: Wire into `_do_start_bot`**

Replace the print at line 378:

```python
    print(f"[Server] Agent gender: {agent_gender} → pitch prior={pitch_shift} "
          f"(adaptive={'on' if RVC_ADAPTIVE_PITCH else 'off'}, target_f0={RVC_TARGET_F0:.0f}Hz)")
```

Add the two kwargs to the `RVCStreamingConverter(...)` construction (after `protect=RVC_PROTECT,`):

```python
            adaptive_pitch=RVC_ADAPTIVE_PITCH,
            target_f0=RVC_TARGET_F0,
```

- [ ] **Step 3: Verify**

Run: `.venv\Scripts\python.exe -m backend.test_pipeline`
Expected: all tests pass (this file's import of `backend.main` isn't part of the tests, so also run `.venv\Scripts\python.exe -m py_compile backend/main.py` — expected exit 0).

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat(backend): RVC_ADAPTIVE_PITCH / RVC_TARGET_F0 env wiring

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Offline adaptive replay + Modal A/B verification

**Files:**
- Modify: `modal_deploy/worker.py:763-833` (`convert_file_chunked`), `modal_deploy/worker.py:836-865` (`main_chunked`)
- Create: `scripts/f0_median.py`

**Interfaces:**
- Consumes: `PitchLock`, `convert_block(..., f0_sink=)`, `st.SAMPLE_RATE_IN`.
- Produces: `main_chunked` CLI flags `--adaptive 1` and `--target-f0 208.0`; `scripts/f0_median.py <file.wav>` prints `median voiced F0 = NNN.N Hz`.

- [ ] **Step 1: Add adaptive mode to `convert_file_chunked`**

Change the signature (line 763):

```python
def convert_file_chunked(
    audio_bytes: bytes,
    pitch: float = -1,
    use_trt: int = 0,
    index_rate: float = 0.75,
    rms_mix_rate: float = 0.75,
    protect: float = 0.33,
    adaptive: int = 0,
    target_f0: float = 208.0,
) -> bytes:
```

BEFORE the existing `pitch == -1` auto-detect block (lines 790–793), add the guard (it must precede auto-detect, which would otherwise replace `-1` with a concrete value and make the guard unreachable):

```python
    if adaptive and pitch == -1:
        raise ValueError(
            "--adaptive needs an explicit --pitch prior (e.g. --pitch 7); "
            "-1 keeps the legacy whole-file auto-detect and disables adaptation"
        )
```

AFTER that auto-detect block (so `pitch` is concrete either way), create the lock:

```python
    lock = pl.PitchLock(prior_shift=pitch, target_f0=target_f0, enabled=bool(adaptive))
```

Replace the non-silent branch of the block loop (lines 809–823) with (mirrors `ws_stream` exactly):

```python
        else:
            pitch_for_block = lock.shift if lock.enabled else pitch
            f0_sink = [] if (lock.enabled and not lock.locked) else None
            t0 = time.perf_counter()
            out_bytes = test_engine.convert_block(
                infer_input,
                pitch=pitch_for_block,
                index_rate=index_rate,
                rms_mix_rate=rms_mix_rate,
                protect=protect,
                f0_sink=f0_sink,
            )
            infer_ms = (time.perf_counter() - t0) * 1000
            print(f"[Timing] convert_block: {infer_ms:.1f}ms ({test_engine.engine_kind})")
            if f0_sink:
                if lock.add_block(
                    np.concatenate(f0_sink),
                    block_seconds=len(block) / st.SAMPLE_RATE_IN,
                ):
                    print(
                        f"[AdaptivePitch] locked shift={lock.shift:+.2f} st "
                        f"(median F0={lock.locked_median_f0:.1f}Hz → target {target_f0:.0f}Hz, "
                        f"{lock.voiced_seconds:.1f}s voiced, prior {lock.prior_shift:+.1f})"
                    )
            out = np.frombuffer(out_bytes, dtype=np.int16)
            out = st.trim_context(
                out, context_len, len(infer_input), overlap_keep=st.SOLA_CROSSFADE_SAMPLES
            )
```

- [ ] **Step 2: Plumb the flags through `main_chunked`**

Signature (line 837):

```python
def main_chunked(
    pitch: float = -1,
    use_trt: int = 0,
    input_file: str = r"D:\Kiera\male_test.wav",
    output_file: str = "",
    index_rate: float = 0.75,
    rms_mix_rate: float = 0.75,
    protect: float = 0.33,
    adaptive: int = 0,
    target_f0: float = 208.0,
):
```

Extend the header print (line 848) to include ` | adaptive={adaptive} target_f0={target_f0}`, and add both kwargs to the `convert_file_chunked.remote(...)` call:

```python
        adaptive=adaptive,
        target_f0=target_f0,
```

- [ ] **Step 3: Create `scripts/f0_median.py`**

```python
"""Median voiced F0 (autocorrelation) of a WAV -- the acceptance check for the
adaptive pitch lock (spec 2026-07-13): converted output should land near the
model center (~208 Hz).

Usage: python scripts/f0_median.py <file.wav> [more.wav ...]
"""
import sys
import wave

import numpy as np


def median_f0(path: str, fmin: float = 60.0, fmax: float = 400.0) -> float:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
    frame, hop = int(0.040 * sr), int(0.020 * sr)
    f0s = []
    for i in range(0, len(x) - frame, hop):
        fr = x[i:i + frame]
        fr = fr - fr.mean()
        if np.sqrt(np.mean(fr ** 2)) < 0.01:
            continue
        ac = np.correlate(fr, fr, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        ac /= ac[0]
        lo, hi = int(sr / fmax), min(int(sr / fmin), len(ac) - 1)
        k = int(np.argmax(ac[lo:hi])) + lo
        if ac[k] >= 0.5:
            f0s.append(sr / k)
    if not f0s:
        raise SystemExit(f"{path}: no voiced frames found")
    return float(np.median(f0s))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for p in sys.argv[1:]:
        print(f"{p}: median voiced F0 = {median_f0(p):.1f} Hz")
```

- [ ] **Step 4: Verify locally and commit the tooling**

Run: `.venv\Scripts\python.exe -m py_compile modal_deploy/worker.py scripts/f0_median.py`
Expected: exit 0.

```bash
git add modal_deploy/worker.py scripts/f0_median.py
git commit -m "feat(worker): adaptive replay mode for main_chunked + f0_median check

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Run the offline A/B on Modal (ephemeral GPU — billed, does NOT touch the deployed worker)**

From the repo root:

```
.venv\Scripts\modal.exe volume get rvc-models debug/call_20260713-180042_in16k.wav call2_in16k.wav
.venv\Scripts\modal.exe run modal_deploy/worker.py::main_chunked --input-file call2_in16k.wav --pitch 7 --adaptive 1 --use-trt 1 --output-file call2_adaptive.wav
.venv\Scripts\python.exe scripts\f0_median.py call2_adaptive.wav
```

Expected:
- A `[AdaptivePitch] locked shift=+5.xx st (median F0=15x.xHz → target 208Hz, ...)` line early in the block loop (the live call measured median input F0 ≈152 Hz; RMVPE may differ slightly from that autocorrelation estimate — accept a lock anywhere in **+4.5 to +6.0**).
- `call2_adaptive.wav: median voiced F0 = 2xx.x Hz` in **200–216 Hz** (the fixed +7 live call measured 226 Hz).

Also run the fixed-shift control to confirm the A/B delta on identical input:

```
.venv\Scripts\modal.exe run modal_deploy/worker.py::main_chunked --input-file call2_in16k.wav --pitch 7 --adaptive 0 --use-trt 1 --output-file call2_fixed.wav
.venv\Scripts\python.exe scripts\f0_median.py call2_fixed.wav
```

Expected: median F0 ≈ 220–232 Hz (clearly above target; matches the live complaint).

Then repeat the adaptive run on the FIRST 2026-07-13 capture (spec requires both; this one had the worse input — median F0 ≈158 Hz, expect a lock around **+4.0 to +5.5** and the same 200–216 Hz output):

```
.venv\Scripts\modal.exe volume get rvc-models debug/call_20260713-154945_in16k.wav call1_in16k.wav
.venv\Scripts\modal.exe run modal_deploy/worker.py::main_chunked --input-file call1_in16k.wav --pitch 7 --adaptive 1 --use-trt 1 --output-file call1_adaptive.wav
.venv\Scripts\python.exe scripts\f0_median.py call1_adaptive.wav
```

If either adaptive run does NOT land 200–216 Hz or prints no `[AdaptivePitch]` line, STOP — do not proceed to Task 7; debug with the systematic-debugging skill.

(*.wav files are gitignored; nothing to commit for this step.)

---

### Task 7: Documentation

**Files:**
- Modify: `README.md` (env block around lines 59–66), `CLAUDE.md` (Environment section env list), `.agents/context/subsystem-notes.md` (Modal RVC GPU worker section), `.agents/projects/active-backlog.md` (rollout gate entry)

**Interfaces:** none (docs only).

- [ ] **Step 1: README env block**

In the `# RVC Serverless GPU` env block (after the `RVC_KEEPWARM` line), add:

```
RVC_ADAPTIVE_PITCH=1 # per-call F0-derived pitch lock; 0 = legacy fixed RVC_MALE_PITCH_SHIFT only
RVC_TARGET_F0=208 # Hz center of the trained model's pitch range the adaptive lock targets
```

- [ ] **Step 2: CLAUDE.md env list**

In the Environment section's env-name list, add `RVC_ADAPTIVE_PITCH`, `RVC_TARGET_F0` after `RVC_KEEPWARM`.

- [ ] **Step 3: Subsystem notes**

Append to the "Modal RVC GPU worker (`modal_deploy/worker.py`)" section of `.agents/context/subsystem-notes.md`:

```markdown
- **Adaptive per-call pitch lock (added 2026-07-13, spec
  `docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md`).** The fixed
  `RVC_MALE_PITCH_SHIFT` went stale when the agent's live F0 moved (152-158Hz on
  2026-07-13 vs the 137-138Hz that +7 was calibrated on → output 1.5-2 st above the
  model center = wrong identity). `ws_stream` now owns a `PitchLock`
  (`modal_deploy/pitch_lock.py`): engines expose their PRE-shift F0 track via the
  `f0_sink` kwarg on `convert_block`, the session accumulates voiced frames
  (60-400Hz window) and, at ≥2s voiced, locks `12·log2(target_f0/median)` (float,
  clamped ±12) for the rest of the session. The locked value rides the existing
  `stats` messages as `locked_pitch`; `RVCStreamingConverter` adopts it and flips
  its own `adaptive_pitch` off, so a WS **reconnect resumes the locked identity**
  — the exact failure that got `_auto_detect_pitch` reverted on 2026-07-03 cannot
  recur (that legacy path still exists behind `pitch_shift == -1`, unused live).
  Kill switch: `RVC_ADAPTIVE_PITCH=0` on Render (backend env; default on). Offline
  replay: `modal run modal_deploy/worker.py::main_chunked --pitch 7 --adaptive 1`.
  Trap: `pitch_lock.py` must stay numpy+stdlib-only (it imports into the container
  via `add_local_python_source("pitch_lock")` in `modal_defs.py` — same mount rule
  as streaming.py).
```

- [ ] **Step 4: Backlog entry**

Add an entry to `.agents/projects/active-backlog.md`, matching the file's existing entry format, with this content:

> **Adaptive pitch lock — live rollout (USER-RUN).** Code merged; offline A/B passed (output median F0 on target vs 226Hz fixed). Remaining: (1) user runs `modal deploy modal_deploy/worker.py`; (2) one field call; verify `[AdaptivePitch] locked ...` in Modal logs, `locked_pitch` in the Render `[Worker][LatencySummary]` lines, and debug-WAV output median F0 ≈208Hz via `scripts/f0_median.py`. Render env needs nothing (defaults on); set `RVC_ADAPTIVE_PITCH=0` to roll back without redeploying Modal.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md .agents/context/subsystem-notes.md .agents/projects/active-backlog.md
git commit -m "docs: adaptive pitch lock env vars, subsystem notes, rollout gate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Rollout (USER-RUN — not an executable task)

1. Push to `main` (Render auto-deploys the backend; safe against the old worker — see Deploy-ordering safety above).
2. **User** runs `modal deploy modal_deploy/worker.py` when ready.
3. One field call; verify per the backlog entry (lock log line, `locked_pitch` in LatencySummary, debug-WAV median F0 ≈208 Hz).
4. Rollback path: set `RVC_ADAPTIVE_PITCH=0` on Render (no Modal redeploy needed).
