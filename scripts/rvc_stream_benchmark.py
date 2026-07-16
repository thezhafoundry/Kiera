"""Benchmark the real, long-lived RVC WebSocket conversion path.

This intentionally starts exactly one conversion session.  RVC's standalone
``wait_ready`` probe opens a separate socket, which is useful for the backend
warm gate but would race the worker's single-session admission during a stream
benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import statistics
import time
from typing import Any, Iterable

from dotenv import load_dotenv

from backend.converters.rvc_stream import RVCStreamingConverter


FRAME_BYTES = 640  # 20 ms of 16 kHz mono int16 PCM
INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 48_000


def percentile(values: Iterable[float], percent: float) -> float:
    """Return a linearly interpolated percentile without a NumPy dependency."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summary(values: Iterable[float]) -> dict[str, float | int]:
    samples = [float(value) for value in values]
    if not samples:
        return {"count": 0, "median": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(samples),
        "median": round(statistics.median(samples), 2),
        "p95": round(percentile(samples, 95), 2),
        "max": round(max(samples), 2),
    }


async def _wait_for_active_session(
    converter: Any,
    stream_task: asyncio.Task,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if converter.is_healthy:
            return
        if stream_task.done():
            try:
                stream_task.result()
            except StopAsyncIteration as exc:
                raise RuntimeError("active RVC session ended before readiness") from exc
            raise RuntimeError("active RVC session ended before readiness")
        await asyncio.sleep(0.01)
    raise RuntimeError(
        f"active RVC session did not become ready within {timeout:.1f}s"
    )


async def run_stream_benchmark(
    converter: Any,
    pcm_16k: bytes,
    *,
    pace_seconds: float = 0.02,
    readiness_timeout: float = 360.0,
    drain_timeout: float = 8.0,
) -> dict[str, Any]:
    """Run one paced input through one active RVC conversion socket."""
    if not pcm_16k or len(pcm_16k) % 2:
        return {"success": False, "error": "input must be non-empty int16 PCM"}

    padded_bytes = ((len(pcm_16k) + FRAME_BYTES - 1) // FRAME_BYTES) * FRAME_BYTES
    expected_output_bytes = padded_bytes * (OUTPUT_SAMPLE_RATE // INPUT_SAMPLE_RATE)
    input_duration_ms = len(pcm_16k) / 2 / INPUT_SAMPLE_RATE * 1000.0
    ready_to_send = asyncio.Event()
    output = bytearray()
    stats: list[dict[str, Any]] = []
    previous_on_stats = getattr(converter, "on_stats", None)

    def capture_stats(payload: dict[str, Any]) -> None:
        stats.append(dict(payload))
        if previous_on_stats is not None:
            previous_on_stats(payload)

    converter.on_stats = capture_stats

    async def input_frames():
        await ready_to_send.wait()
        for offset in range(0, len(pcm_16k), FRAME_BYTES):
            frame = pcm_16k[offset:offset + FRAME_BYTES]
            if len(frame) < FRAME_BYTES:
                frame += b"\x00" * (FRAME_BYTES - len(frame))
            yield frame
            if pace_seconds > 0:
                await asyncio.sleep(pace_seconds)

    stream = converter.convert_stream(input_frames())
    first_chunk_task: asyncio.Task | None = None
    started_at = time.monotonic()
    try:
        async with contextlib.aclosing(stream):
            first_chunk_task = asyncio.create_task(anext(stream))
            try:
                await _wait_for_active_session(
                    converter,
                    first_chunk_task,
                    readiness_timeout,
                )
            except Exception:
                if not first_chunk_task.done():
                    first_chunk_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await first_chunk_task
                first_chunk_task = None
                raise
            active_ready_ms = (time.monotonic() - started_at) * 1000.0
            ready_to_send.set()

            deadline = time.monotonic() + input_duration_ms / 1000.0 + drain_timeout
            while len(output) < expected_output_bytes and time.monotonic() < deadline:
                remaining = max(0.01, deadline - time.monotonic())
                try:
                    if first_chunk_task is not None:
                        chunk = await asyncio.wait_for(
                            first_chunk_task,
                            timeout=remaining,
                        )
                        first_chunk_task = None
                    else:
                        chunk = await asyncio.wait_for(
                            anext(stream),
                            timeout=min(1.5, remaining),
                        )
                except asyncio.TimeoutError:
                    continue
                except StopAsyncIteration:
                    break
                output.extend(chunk)

        output_duration_ms = len(output) / 2 / OUTPUT_SAMPLE_RATE * 1000.0
        numeric = {
            name: _summary(
                payload[name]
                for payload in stats
                if isinstance(payload.get(name), (int, float))
            )
            for name in (
                "infer_ms",
                "converter_wait_ms",
                "network_rtt_ms",
                "hubert_ms",
                "generator_ms",
                "faiss_ms",
                "resample_ms",
            )
        }
        drops = {
            "total": int(getattr(converter, "drop_count", 0)),
            "stale_input": int(getattr(converter, "stale_input_drop_count", 0)),
            "input_overflow": int(getattr(converter, "input_overflow_drop_count", 0)),
            "output": int(getattr(converter, "output_drop_count", 0)),
            "connection_failures": int(
                getattr(converter, "connection_failure_count", 0)
            ),
        }
        return {
            "success": bool(output),
            "profile": str(getattr(converter, "profile", "unknown")),
            "model_version": str(
                getattr(converter, "model_version", "unknown")
            ),
            "use_trt": bool(getattr(converter, "use_trt", False)),
            "block_ms": int(getattr(converter, "block_ms", 0)),
            "context_ms": int(getattr(converter, "context_ms", 0)),
            "sola_ms": int(getattr(converter, "sola_ms", 0)),
            "active_ready_ms": round(active_ready_ms, 2),
            "input_duration_ms": round(input_duration_ms, 2),
            "output_duration_ms": round(output_duration_ms, 2),
            "duration_delta_ms": round(output_duration_ms - input_duration_ms, 2),
            "expected_output_bytes": expected_output_bytes,
            "output_bytes": len(output),
            "stats_count": len(stats),
            **numeric,
            "drops": drops,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        if first_chunk_task is not None and not first_chunk_task.done():
            first_chunk_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first_chunk_task
        converter.on_stats = previous_on_stats
        await converter.close()


def generate_sine(duration_seconds: float, frequency: float = 220.0) -> bytes:
    """Generate safe, synthetic 16 kHz int16 benchmark input."""
    import array

    samples = array.array(
        "h",
        (
            int(10_000 * math.sin(2 * math.pi * frequency * index / INPUT_SAMPLE_RATE))
            for index in range(round(duration_seconds * INPUT_SAMPLE_RATE))
        ),
    )
    return samples.tobytes()


async def _main_async(args: argparse.Namespace) -> int:
    load_dotenv()
    endpoint = os.getenv("RVC_ENDPOINT_URL", "")
    api_key = os.getenv("RVC_API_KEY", "")
    if not endpoint or not api_key:
        print(json.dumps({
            "success": False,
            "error": "RVC_ENDPOINT_URL and RVC_API_KEY are required",
        }))
        return 2

    converter = RVCStreamingConverter(
        endpoint_url=endpoint,
        ws_url=os.getenv("RVC_WS_URL", ""),
        api_key=api_key,
        pitch_shift=float(os.getenv("RVC_MALE_PITCH_SHIFT", "7")),
        index_rate=float(os.getenv("RVC_INDEX_RATE", "0.75")),
        rms_mix_rate=float(os.getenv("RVC_RMS_MIX_RATE", "0.75")),
        protect=float(os.getenv("RVC_PROTECT", "0.33")),
        adaptive_pitch=os.getenv("RVC_ADAPTIVE_PITCH", "1") == "1",
        target_f0=float(os.getenv("RVC_TARGET_F0", "208")),
        connect_timeout=150.0,
    )
    result = await run_stream_benchmark(
        converter,
        generate_sine(args.duration),
        readiness_timeout=args.readiness_timeout,
        drain_timeout=args.drain_timeout,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark one real RVC WebSocket conversion session",
    )
    parser.add_argument("--duration", type=float, default=9.6)
    parser.add_argument("--readiness-timeout", type=float, default=360.0)
    parser.add_argument("--drain-timeout", type=float, default=8.0)
    return asyncio.run(_main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
