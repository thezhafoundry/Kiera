"""Streaming presence EQ for the converted 48 kHz output.

The PSTN leg (G.711) hard-caps the call at ~3.4 kHz, so perceived clarity on the
lead's phone depends almost entirely on how much 1.2-3.4 kHz consonant/formant
energy survives. PresenceEQ applies a gentle boost to exactly that band before
the audio is published, leaving everything below ~1 kHz untouched.

Implemented as a linear-phase FIR (frequency-sampling design, Hamming-windowed)
rather than an IIR biquad because scipy isn't a dependency: the FIR convolution
vectorizes with numpy at C speed, and streaming continuity is exact — the filter
keeps the last (taps-1) input samples as state, so chunked processing is
byte-identical to a single pass (no clicks at chunk boundaries). Group delay is
(taps-1)/2 samples ≈ 1.3 ms at 48 kHz — negligible against the playout buffer.
"""

import numpy as np


class PresenceEQ:
    def __init__(
        self,
        gain_db: float = 4.0,
        f_lo: float = 1200.0,
        f_hi: float = 3400.0,
        sample_rate: int = 48000,
        taps: int = 127,
    ):
        if taps % 2 == 0:
            raise ValueError("taps must be odd (linear-phase type I FIR)")
        self._taps = taps

        # Desired magnitude: unity everywhere, `gain_db` inside [f_lo, f_hi],
        # raised-cosine transitions wide enough for the kernel to resolve
        # (~sample_rate/taps Hz) so windowing doesn't smear the band edges.
        n_fft = 4096
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
        gain = 10.0 ** (gain_db / 20.0)
        tw = 2.0 * sample_rate / taps  # transition width, Hz
        desired = np.ones_like(freqs)
        for i, f in enumerate(freqs):
            if f_lo <= f <= f_hi:
                desired[i] = gain
            elif f_lo - tw < f < f_lo:
                desired[i] = 1.0 + (gain - 1.0) * 0.5 * (1 - np.cos(np.pi * (f - (f_lo - tw)) / tw))
            elif f_hi < f < f_hi + tw:
                desired[i] = 1.0 + (gain - 1.0) * 0.5 * (1 + np.cos(np.pi * (f - f_hi) / tw))

        # Zero-phase impulse response (symmetric about sample 0), re-centred to
        # `taps` causal coefficients and windowed.
        impulse = np.fft.irfft(desired)
        kernel = np.roll(impulse, taps // 2)[:taps] * np.hamming(taps)
        # Pin unity gain at DC so the pass-through region is exact.
        self._kernel = (kernel / kernel.sum()).astype(np.float64)

        # Last (taps-1) input samples; zeros = silence preceding the stream.
        self._tail = np.zeros(taps - 1, dtype=np.float64)

    def process(self, chunk: bytes) -> bytes:
        """Filter one chunk of 16-bit mono PCM; returns the same byte length."""
        if not chunk:
            return chunk
        x = np.frombuffer(chunk, dtype=np.int16).astype(np.float64)
        buf = np.concatenate((self._tail, x))
        y = np.convolve(buf, self._kernel, mode="valid")
        self._tail = buf[-(self._taps - 1):]
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()
