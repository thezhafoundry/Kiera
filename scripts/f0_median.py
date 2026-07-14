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
