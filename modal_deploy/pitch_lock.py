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
        target_f0 = float(target_f0)
        if target_f0 <= 0 or not math.isfinite(target_f0):
            print(
                f"[PitchLock] invalid target_f0={target_f0!r} (must be positive "
                f"and finite) -- falling back to DEFAULT_TARGET_F0_HZ={DEFAULT_TARGET_F0_HZ}"
            )
            target_f0 = DEFAULT_TARGET_F0_HZ
        self.target_f0 = target_f0
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
