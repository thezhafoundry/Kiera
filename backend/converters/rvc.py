import httpx
import struct
from typing import AsyncIterator
from .base import VoiceConverter


def _build_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    num_samples = len(pcm_bytes) // 2
    num_channels = 1
    bits_per_sample = 16
    block_align = num_channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    data_size = num_samples * block_align

    header = bytearray(44)
    header[0:4] = b'RIFF'
    header[4:8] = struct.pack('<I', 36 + data_size)
    header[8:12] = b'WAVE'
    header[12:16] = b'fmt '
    header[16:20] = struct.pack('<I', 16)
    header[20:22] = struct.pack('<H', 1)
    header[22:24] = struct.pack('<H', num_channels)
    header[24:28] = struct.pack('<I', sample_rate)
    header[28:32] = struct.pack('<I', byte_rate)
    header[32:34] = struct.pack('<H', block_align)
    header[34:36] = struct.pack('<H', bits_per_sample)
    header[36:40] = b'data'
    header[40:44] = struct.pack('<I', data_size)

    return bytes(header) + pcm_bytes


class RVCVoiceConverter(VoiceConverter):
    """
    Voice converter using RVC v2 running on a serverless GPU (Modal/RunPod).

    Sends raw 16kHz mono PCM audio (wrapped as WAV) to a remote RVC inference
    endpoint via HTTP, receives converted 48kHz mono PCM back. The endpoint
    handles model loading, pitch extraction, and inference internally.

    NOTE: Requires a deployed RVC inference endpoint and a trained .pth voice model.
    This converter is scaffolding until the endpoint is live — it will fall back to
    passthrough (via the pipeline's timeout) if the endpoint is unreachable.
    """

    def __init__(
        self,
        endpoint_url: str,
        api_key: str = "",
        pitch_shift: int = 0,
        budget_ms: float = 5000.0,
    ):
        self.endpoint_url = endpoint_url.rstrip("/")
        self.api_key = api_key
        self.pitch_shift = pitch_shift

        timeout = httpx.Timeout(budget_ms / 1000.0 + 2.0, connect=5.0)
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        self.client = httpx.AsyncClient(timeout=timeout, limits=limits)

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        pcm_buffer = bytearray()
        async for chunk in in_audio:
            pcm_buffer.extend(chunk)

        if len(pcm_buffer) < 640:
            yield bytes(pcm_buffer)
            return

        wav_bytes = _build_wav(bytes(pcm_buffer), sample_rate=16000)

        headers = {"Content-Type": "audio/wav"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = await self.client.post(
            self.endpoint_url,
            content=wav_bytes,
            headers=headers,
            params={"pitch_shift": self.pitch_shift},
        )
        resp.raise_for_status()

        server_time = resp.headers.get("X-Server-Time-Ms", "unknown")
        print(f"[RVC Client] Server processing time: {server_time} ms")

        converted_pcm = resp.content
        if len(converted_pcm) > 0:
            yield converted_pcm
        else:
            yield bytes(pcm_buffer)


    async def close(self) -> None:
        await self.client.aclose()
