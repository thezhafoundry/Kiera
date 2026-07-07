"""Pure (numpy-only) streaming DSP for the RVC WebSocket path.

This module holds the CPU-side building blocks of the `/ws` streaming engine:
block accumulation, proportional context trimming, SOLA align + crossfade, and
PCM<->WAV helpers. It deliberately imports **only** numpy + the stdlib so that
these functions can be unit-tested (Task 5) and imported WITHOUT installing Modal
or having a GPU present. `modal_deploy/worker.py` imports and drives them.

All PCM is mono int16. Input audio is 16 kHz; converted output is 48 kHz.
"""

import io
import wave

import numpy as np


# ---- Parameter table (from the streaming-rebuild plan) ----

SAMPLE_RATE_IN = 16000          # agent mic / inference input rate
SAMPLE_RATE_OUT = 48000         # RVC / published track rate (3x input)

# TRT Phase 2 (2026-07-07): block shrunk 1000ms→320ms to reduce mouth-to-ear latency
# now that TRT median is 66ms (21× real-time) and the playout buffer gate has passed.
# SOLA crossfade stays at 80ms (25% overhead per block at 320ms — up from 8% at 1000ms;
# watch for time-compression artefacts in C5 listen test).
# NOTE: BLOCK_MS + CONTEXT_MS must equal trt_pipeline.CANONICAL_IN / SAMPLE_RATE_IN * 1000
# (now 320+400=720ms = 11520 samples) -- changing either without updating the
# TRT static shapes requires re-exporting all three ONNX models.
BLOCK_MS = 320               # new audio processed per inference block (was 1000)
CONTEXT_MS = 400              # prior input prepended as left context (unchanged)

BLOCK_SAMPLES_IN = SAMPLE_RATE_IN * BLOCK_MS // 1000        # 5120  (was 16000)
CONTEXT_SAMPLES_IN = SAMPLE_RATE_IN * CONTEXT_MS // 1000    # 6400  (unchanged)

SOLA_CROSSFADE_SAMPLES = SAMPLE_RATE_OUT * 80 // 1000       # 3840 (80 ms @ 48 kHz)
SOLA_SEARCH_SAMPLES = SAMPLE_RATE_OUT * 10 // 1000          # 480  (10 ms @ 48 kHz)

SILENCE_RMS_THRESHOLD = 150     # block RMS (int16) below this -> silence bypass


def pcm16_to_wav_bytes(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE_IN) -> bytes:
    """Wrap a mono int16 PCM array in a WAV container.

    `engine.run_conversion` / `engine._auto_detect_pitch` consume WAV bytes, so
    the streaming handler wraps each inference block with this helper.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


def block_rms(block: np.ndarray) -> float:
    """RMS of an int16 block, computed in float to avoid overflow."""
    if len(block) == 0:
        return 0.0
    x = np.asarray(block, dtype=np.float64)
    return float(np.sqrt(np.mean(x * x)))


def trim_context(
    out: np.ndarray,
    context_len_in: int,
    total_len_in: int,
    overlap_keep: int = 0,
) -> np.ndarray:
    """Trim the converted left-context off the head of an output block.

    RVC does not guarantee output length is exactly 3x input (internal resampling
    / F0 processing shift sample counts), so the context is sliced *proportionally*
    to the real output length rather than at a fixed 3x offset. Same insight as the
    proportional carry-over slice in `backend/pipeline.py`.

    `overlap_keep` retains that many samples of the context tail (default 0 = trim
    the full context, matching pipeline.py). The streaming path keeps a crossfade's
    worth of overlap so consecutive block outputs share OVERLAPPING content for the
    SOLA crossfade to align and merge — without it, contiguous blocks would be
    crossfaded over mismatched audio and lose one crossfade per block (time
    compression). See `sola_crossfade` and the /ws handler.
    """
    out = np.asarray(out)
    if total_len_in <= 0 or context_len_in <= 0:
        return out
    slice_len = int(round(len(out) * (context_len_in / total_len_in)))
    slice_len -= overlap_keep       # retain overlap for the SOLA crossfade
    slice_len -= slice_len % 2      # keep even (16-bit sample alignment parity)
    slice_len = max(0, min(slice_len, len(out)))
    return out[slice_len:]


def sola_crossfade(
    pending_tail: np.ndarray,
    block_out: np.ndarray,
    crossfade: int = SOLA_CROSSFADE_SAMPLES,
    search: int = SOLA_SEARCH_SAMPLES,
) -> tuple[np.ndarray, np.ndarray]:
    """SOLA-align and crossfade a new output block onto the previous tail.

    Pure function: (pending_tail, block_out) -> (emit, new_pending_tail), all
    mono int16 numpy arrays at 48 kHz.

    - `pending_tail` is the last `crossfade` samples held back from the previously
      emitted output (empty on the first block).
    - Within the new block's first `crossfade + search` samples, find the offset
      `k in [0, search)` whose window best correlates (normalized cross-correlation)
      with `pending_tail`, overlap-add the tail and `block_out[k:k+crossfade]` with a
      raised-cosine (Hann) ramp, then emit `crossfaded + block_out[k+crossfade : -crossfade]`
      and hold `block_out[-crossfade:]` as the next pending tail.
    """
    block_out = np.asarray(block_out)
    L = crossfade

    # First block (no tail yet): emit everything except the held-back tail.
    if pending_tail is None or len(pending_tail) == 0:
        if len(block_out) <= L:
            return np.zeros(0, dtype=np.int16), np.asarray(block_out, dtype=np.int16)
        return (
            np.asarray(block_out[:-L], dtype=np.int16),
            np.asarray(block_out[-L:], dtype=np.int16),
        )

    tail = np.asarray(pending_tail, dtype=np.float64)
    L = min(L, len(tail))  # safety: never correlate more than we held

    # Candidate search region: first L + search samples of the new block.
    seg_limit = min(len(block_out), L + search)
    seg = np.asarray(block_out[:seg_limit], dtype=np.float64)
    n_offsets = min(search, max(1, seg_limit - L))

    best_k = 0
    best_score = -np.inf
    for k in range(n_offsets):
        window = seg[k:k + L]
        denom = np.sqrt(np.dot(window, window)) + 1e-8
        score = float(np.dot(tail[:L], window) / denom)
        if score > best_score:
            best_score = score
            best_k = k

    # Raised-cosine (Hann) crossfade ramps.
    ramp = np.arange(L, dtype=np.float64) / L
    fade_in = 0.5 * (1.0 - np.cos(np.pi * ramp))   # 0 -> 1
    fade_out = 1.0 - fade_in                        # 1 -> 0

    window = np.asarray(block_out[best_k:best_k + L], dtype=np.float64)
    crossfaded = tail[:L] * fade_out + window * fade_in

    mid_start = best_k + L
    mid_end = len(block_out) - L
    if mid_end > mid_start:
        middle = np.asarray(block_out[mid_start:mid_end], dtype=np.float64)
    else:
        middle = np.zeros(0, dtype=np.float64)

    emit = np.concatenate([crossfaded, middle])
    emit = np.clip(np.round(emit), -32768, 32767).astype(np.int16)

    if len(block_out) >= L:
        new_tail = np.asarray(block_out[-L:], dtype=np.int16)
    else:
        new_tail = np.asarray(block_out, dtype=np.int16)
    return emit, new_tail


class BlockAccumulator:
    """Accumulates 16 kHz int16 PCM and yields inference blocks on demand.

    Each popped block is `context (<=CONTEXT) + block (BLOCK)` of new audio. The
    buffer retains only `context_samples` of history behind the read cursor so
    memory stays bounded across a long session.
    """

    def __init__(
        self,
        block_samples: int = BLOCK_SAMPLES_IN,
        context_samples: int = CONTEXT_SAMPLES_IN,
    ) -> None:
        self.block_samples = block_samples
        self.context_samples = context_samples
        self._buf = np.zeros(0, dtype=np.int16)
        self._next = 0  # index in _buf where the next block begins

    def push(self, pcm: np.ndarray) -> None:
        pcm = np.asarray(pcm, dtype=np.int16)
        if len(pcm):
            self._buf = np.concatenate([self._buf, pcm])

    def pop_block(self):
        """Return (infer_input, context_len, block) or None if not enough new audio.

        - infer_input: context + block, 16 kHz int16 (fed to run_conversion)
        - context_len: number of context samples in infer_input (for trim_context)
        - block: just the NEW block samples (for the silence-RMS check)
        """
        if len(self._buf) - self._next < self.block_samples:
            return None

        block_start = self._next
        block_end = block_start + self.block_samples
        ctx_start = max(0, block_start - self.context_samples)

        context = self._buf[ctx_start:block_start]
        block = np.array(self._buf[block_start:block_end], dtype=np.int16)  # copy
        if len(context):
            infer_input = np.concatenate([context, block])
        else:
            infer_input = block.copy()
        context_len = len(context)

        self._next = block_end

        # Bound memory: keep only context_samples of history behind the cursor.
        trim_from = max(0, self._next - self.context_samples)
        if trim_from > 0:
            self._buf = np.array(self._buf[trim_from:], dtype=np.int16)
            self._next -= trim_from

        return infer_input, context_len, block
