import numpy as np
from typing import AsyncIterator
from .base import VoiceConverter

class DummyVoiceConverter(VoiceConverter):
    """
    A mock voice converter that performs real-time audio manipulation
    (ring modulation for a robotic voice effect) in pure Python/numpy.
    Useful for local testing without an API key.

    Input is raw 16kHz mono PCM (matching the pipeline's producer). Output is
    upsampled 3x to 48kHz mono PCM after ring modulation, so it satisfies the
    same contract real streaming converters do (RVCStreamingConverter yields
    48kHz) — the pipeline no longer resamples anything on its own.
    """

    def __init__(self, carrier_frequency: float = 120.0, sample_rate: int = 16000):
        self.carrier_frequency = carrier_frequency
        self.sample_rate = sample_rate
        self.sample_index = 0

    async def wait_ready(self, timeout: float) -> bool:
        """No real backend to warm up — always ready immediately."""
        return True

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in in_audio:
            if not chunk:
                yield b""
                continue

            # Convert bytes to numpy array (int16 PCM)
            audio_np = np.frombuffer(chunk, dtype=np.int16).copy()

            # Apply ring modulation: y[n] = x[n] * sin(2 * pi * f_c * n / f_s)
            t = (self.sample_index + np.arange(len(audio_np))) / self.sample_rate
            carrier = np.sin(2 * np.pi * self.carrier_frequency * t)

            # Modulate and scale back to int16 range
            modulated_np = (audio_np * carrier).astype(np.int16)

            # Update sample index for phase continuity
            self.sample_index += len(audio_np)
            if self.sample_index > 1000000:
                self.sample_index %= 16000  # Wrap around to prevent integer overflow

            # Upsample 16kHz -> 48kHz (3x) by repeating samples, matching the
            # rate real streaming converters output at.
            yield np.repeat(modulated_np, 3).tobytes()
