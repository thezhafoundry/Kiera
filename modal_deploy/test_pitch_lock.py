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
