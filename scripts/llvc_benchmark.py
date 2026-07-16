#!/usr/bin/env python3
"""LLVC / RVC voice-conversion benchmark script.

Fixes applied vs. the original version:
1. Fake-server and real-service benchmarks use ONE long-lived convert_stream() call with explicit readiness gate, finite drain condition, and deterministic teardown.
2. Exact cumulative byte threshold accounting for latency (robust to frame splitting/combining on the wire).
3. Fake-server output clearly labeled [TEST-ONLY / FAKE SERVER].
4. Strict output validation (drift, clipping, silence) making benchmarks fail and exit non-zero when violated.
5. WAV sample width and compression checks, plus environment variable fallbacks for API keys/URLs.
6. RVC HTTP benchmark only runs when --rvc-endpoint is explicitly passed; --fake-only suppresses real/RVC runs.
"""
import argparse
import asyncio
import collections
import contextlib
import os
import struct
import sys
import time
import wave

import numpy as np
import websockets

# Add backend directory to path so we can import modules
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from backend.converters.rvc import RVCVoiceConverter
from backend.converters.llvc_stream import LLVCStreamingConverter
from backend.converters.llvc_fake_server import llvc_fake_ws_handler


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def generate_dummy_sine(duration_seconds: float = 2.0, sample_rate: int = 16000) -> bytes:
    t = np.linspace(0, duration_seconds, int(sample_rate * duration_seconds), endpoint=False)
    samples = (np.sin(2 * np.pi * 440 * t) * 16384).astype(np.int16)
    return samples.tobytes()


def read_wav_pcm(path: str) -> tuple:
    with wave.open(path, 'rb') as w:
        params = w.getparams()
        if params.sampwidth != 2:
            raise ValueError(
                f"Expected 16-bit PCM WAV (sampwidth=2), got sampwidth={params.sampwidth} in {path}"
            )
        if params.comptype != 'NONE':
            raise ValueError(
                f"Expected uncompressed PCM WAV (comptype='NONE'), got comptype={params.comptype} in {path}"
            )
        n_channels = params.nchannels
        framerate = params.framerate
        raw_data = w.readframes(params.nframes)
        return raw_data, framerate, n_channels


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def validate_output(
    pcm_bytes: bytes,
    input_duration_s: float,
    sample_rate: int,
    label: str,
) -> dict:
    """Validate converted PCM output for drift, clipping, and silence.

    Returns a dict with drift_ok, clipping_ok, silence_ok, and warnings.
    Any failure marks the overall benchmark success as False.
    """
    warnings: list[str] = []

    # --- Duration drift ---
    n_samples = len(pcm_bytes) // 2
    out_duration = n_samples / sample_rate
    drift_s = abs(out_duration - input_duration_s)
    drift_ms = drift_s * 1000.0

    drift_ok = True
    if drift_ms > 200.0:
        warnings.append(f"[{label}] FAIL: duration drift {drift_ms:.1f} ms (> 200 ms)")
        drift_ok = False
    elif drift_ms > 50.0:
        warnings.append(f"[{label}] WARN: duration drift {drift_ms:.1f} ms (> 50 ms)")

    # --- Clipping ---
    if n_samples > 0:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        clipped = np.sum((samples == 32767) | (samples == -32768))
        clip_ratio = clipped / n_samples
    else:
        clip_ratio = 0.0

    clipping_ok = True
    if clip_ratio > 0.01:
        warnings.append(
            f"[{label}] FAIL: excessive clipping detected — {clip_ratio * 100:.2f}% of samples at ±32767"
        )
        clipping_ok = False

    # --- Silence ---
    if n_samples > 0:
        rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
    else:
        rms = 0.0

    silence_ok = True
    if rms < 100.0:
        warnings.append(f"[{label}] FAIL: output silent/near-silent — RMS {rms:.1f} (< 100)")
        silence_ok = False

    for w in warnings:
        print(w)

    return {
        "drift_ok": drift_ok,
        "clipping_ok": clipping_ok,
        "silence_ok": silence_ok,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# RVC HTTP benchmark (offline-test-only, behind --rvc-endpoint flag)
# ---------------------------------------------------------------------------

async def run_rvc_benchmark(pcm_16k: bytes, endpoint_url: str, api_key: str) -> dict:
    """Run the RVC HTTP-POST benchmark. Offline-test-only."""
    print("[RVC] Starting benchmark...")
    converter = RVCVoiceConverter(
        endpoint_url=endpoint_url,
        api_key=api_key,
        pitch_shift=0,
    )

    async def in_audio_gen():
        chunk_size = 640
        for i in range(0, len(pcm_16k), chunk_size):
            yield pcm_16k[i:i + chunk_size]

    start_time = time.monotonic()
    try:
        converted_chunks: list[bytes] = []
        async for chunk in converter.convert_stream(in_audio_gen()):
            converted_chunks.append(chunk)
        elapsed = (time.monotonic() - start_time) * 1000.0

        converted_pcm = b"".join(converted_chunks)
        out_samples = len(converted_pcm) // 2
        out_duration = out_samples / 48000.0
        in_duration = len(pcm_16k) // 2 / 16000.0
        drift_ms = abs(out_duration - in_duration) * 1000.0

        return {
            "success": True,
            "elapsed_ms": elapsed,
            "drift_ms": drift_ms,
            "out_bytes": len(converted_pcm),
            "out_pcm": converted_pcm,
        }
    except Exception as e:
        print(f"[RVC ERROR] {e}")
        return {"success": False, "error": str(e)}
    finally:
        await converter.close()


# ---------------------------------------------------------------------------
# Core Stream Benchmark Helper (used by both Fake and Real)
# ---------------------------------------------------------------------------

async def _run_ws_stream_benchmark(
    converter: LLVCStreamingConverter,
    pcm_16k: bytes,
    label: str,
    connect_timeout: float = 10.0,
) -> dict:
    chunk_size = 640  # 20 ms at 16 kHz
    in_duration = len(pcm_16k) // 2 / 16000.0
    padded_input_bytes = ((len(pcm_16k) + chunk_size - 1) // chunk_size) * chunk_size
    expected_out_bytes = padded_input_bytes * 3
    send_times: collections.deque[tuple[int, float]] = collections.deque()
    latencies: list[float] = []
    out_pcm = bytearray()
    ready_event = asyncio.Event()

    async def input_gen():
        """Yield 640-byte frames with exact cumulative threshold tracking."""
        await ready_event.wait()
        target_bytes = 0
        for i in range(0, len(pcm_16k), chunk_size):
            chunk = pcm_16k[i:i + chunk_size]
            if len(chunk) < chunk_size:
                chunk = chunk + b"\x00" * (chunk_size - len(chunk))
            target_bytes += len(chunk) * 3
            send_times.append((target_bytes, time.monotonic()))
            yield chunk
            await asyncio.sleep(0.02)

    start_time = time.monotonic()
    try:
        stream = converter.convert_stream(input_gen())
        async with contextlib.aclosing(stream):
            # Start the generator (which spawns _conn_task and _pump_task)
            first_chunk_task = asyncio.create_task(anext(stream))

            # 1. Explicit readiness gate
            if not await converter.wait_ready(timeout=connect_timeout):
                first_chunk_task.cancel()
                raise RuntimeError(f"Converter failed to become ready within {connect_timeout}s")

            # Handshake successful and session ready; release the input generator
            ready_event.set()

            # Get the first chunk
            try:
                converted_chunk = await asyncio.wait_for(first_chunk_task, timeout=5.0)
                t_recv = time.monotonic()
                out_pcm.extend(converted_chunk)
                while send_times and len(out_pcm) >= send_times[0][0]:
                    _, t_send = send_times.popleft()
                    latencies.append((t_recv - t_send) * 1000.0)
            except (asyncio.TimeoutError, StopAsyncIteration):
                pass

            # 2. Finite drain condition driven by expected output bytes + deadline
            deadline = time.monotonic() + in_duration + 5.0
            while len(out_pcm) < expected_out_bytes and time.monotonic() < deadline:
                try:
                    converted_chunk = await asyncio.wait_for(anext(stream), timeout=1.5)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break

                t_recv = time.monotonic()
                out_pcm.extend(converted_chunk)

                # Exact cumulative threshold accounting for latency
                while send_times and len(out_pcm) >= send_times[0][0]:
                    _, t_send = send_times.popleft()
                    latencies.append((t_recv - t_send) * 1000.0)

        elapsed = (time.monotonic() - start_time) * 1000.0
        out_samples = len(out_pcm) // 2
        out_duration = out_samples / 48000.0
        drift_ms = abs(out_duration - in_duration) * 1000.0

        if latencies:
            med_lat = float(np.median(latencies))
            p95_lat = float(np.percentile(latencies, 95))
            p99_lat = float(np.percentile(latencies, 99))
        else:
            med_lat = p95_lat = p99_lat = 0.0

        print(f"[{label}] Median latency : {med_lat:.1f} ms")
        print(f"[{label}] p95 latency    : {p95_lat:.1f} ms")
        print(f"[{label}] p99 latency    : {p99_lat:.1f} ms")
        print(f"[{label}] Elapsed        : {elapsed:.1f} ms")
        print(f"[{label}] Drift          : {drift_ms:.1f} ms")
        print(f"[{label}] Output bytes   : {len(out_pcm)}")

        return {
            "success": True,
            "median_latency_ms": med_lat,
            "p95_latency_ms": p95_lat,
            "p99_latency_ms": p99_lat,
            "elapsed_ms": elapsed,
            "drift_ms": drift_ms,
            "out_bytes": len(out_pcm),
            "out_pcm": bytes(out_pcm),
            "latencies": latencies,
        }
    except Exception as e:
        print(f"[{label} ERROR] {e}")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# LLVC fake-server (test-only) benchmark
# ---------------------------------------------------------------------------

async def run_fake_server_benchmark(pcm_16k: bytes, port: int = 18000) -> dict:
    """Run LLVC benchmark against a local fake WS server.

    For automated testing only — does not measure real service performance.
    Uses ONE long-lived convert_stream() call with deterministic termination.
    """
    print(f"[TEST-ONLY / FAKE SERVER] Starting benchmark server on 127.0.0.1:{port}...")
    server = await websockets.serve(llvc_fake_ws_handler, "127.0.0.1", port)

    converter = LLVCStreamingConverter(
        ws_url=f"ws://127.0.0.1:{port}",
        api_key="bench-key",
        connect_timeout=5.0,
    )

    try:
        return await _run_ws_stream_benchmark(
            converter, pcm_16k, "TEST-ONLY / FAKE SERVER", connect_timeout=5.0
        )
    finally:
        await converter.close()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# LLVC real-service benchmark
# ---------------------------------------------------------------------------

async def run_real_service_benchmark(ws_url: str, api_key: str, pcm_16k: bytes) -> dict:
    """Run LLVC benchmark against a real remote WS service."""
    print(f"[REAL SERVICE] Connecting to {ws_url} ...")

    converter = LLVCStreamingConverter(
        ws_url=ws_url,
        api_key=api_key,
        connect_timeout=10.0,
    )

    try:
        return await _run_ws_stream_benchmark(
            converter, pcm_16k, "REAL SERVICE", connect_timeout=10.0
        )
    finally:
        await converter.close()


# ---------------------------------------------------------------------------
# Results table
# ---------------------------------------------------------------------------

def print_results_table(duration: float, results: dict[str, dict]) -> None:
    """Print a comparison table for all benchmark runs."""
    print("\n" + "=" * 72)
    print("              VOICE PIPELINE BENCHMARK RESULTS")
    print("=" * 72)
    print(f"Input Audio Duration: {duration:.2f} seconds")
    print("-" * 72)

    labels = list(results.keys())
    header = f"{'Metric':<25}"
    for lbl in labels:
        header += f" | {lbl:<18}"
    print(header)
    print("-" * 72)

    metrics = [
        ("Median Latency", "median_latency_ms", "ms"),
        ("p95 Latency", "p95_latency_ms", "ms"),
        ("p99 Latency", "p99_latency_ms", "ms"),
        ("Total Elapsed", "elapsed_ms", "ms"),
        ("Duration Drift", "drift_ms", "ms"),
        ("Output Bytes", "out_bytes", ""),
    ]

    for display_name, key, unit in metrics:
        row = f"{display_name:<25}"
        for lbl in labels:
            r = results[lbl]
            if not r.get("success"):
                row += f" | {'FAILED':<18}"
            elif key in r:
                val = r[key]
                if unit:
                    row += f" | {val:<18.1f}"
                else:
                    row += f" | {val:<18}"
            else:
                row += f" | {'N/A':<18}"
        print(row)

    print("-" * 72)
    for lbl in labels:
        r = results[lbl]
        v = r.get("validation")
        if v:
            ok = v["drift_ok"] and v["clipping_ok"] and v["silence_ok"]
            status = "PASS" if ok else "FAIL"
            print(f"  {lbl} validation: {status}")
            for w in v.get("warnings", []):
                print(f"    {w}")

    print("=" * 72)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main_async() -> int:
    parser = argparse.ArgumentParser(
        description="LLVC / RVC Voice Conversion Latency & Drift Benchmarker"
    )
    parser.add_argument("--file", help="Path to 16 kHz Mono WAV test file (optional)")
    parser.add_argument(
        "--fake-only",
        action="store_true",
        help="Run only the fake-server benchmark (suppresses real/RVC runs)",
    )
    parser.add_argument(
        "--no-fake",
        action="store_true",
        help="Skip the test-only fake-server benchmark",
    )
    parser.add_argument(
        "--real-ws-url",
        default=os.environ.get("LLVC_WS_URL", ""),
        help="WebSocket URL for real LLVC service benchmark (env: LLVC_WS_URL)",
    )
    parser.add_argument(
        "--real-api-key",
        default=os.environ.get("LLVC_API_KEY", ""),
        help="API key for real LLVC service (env: LLVC_API_KEY)",
    )
    parser.add_argument(
        "--llvc-port",
        type=int,
        default=18000,
        help="Port for local fake LLVC server (default 18000)",
    )
    parser.add_argument(
        "--rvc-endpoint",
        default=os.environ.get("RVC_ENDPOINT_URL", ""),
        help="RVC HTTP endpoint URL (enables RVC HTTP benchmark, env: RVC_ENDPOINT_URL)",
    )
    parser.add_argument(
        "--rvc-api-key",
        default=os.environ.get("RVC_API_KEY", ""),
        help="RVC API key (env: RVC_API_KEY)",
    )

    args = parser.parse_args()

    if args.file:
        raw_pcm, sr, channels = read_wav_pcm(args.file)
        if channels > 1:
            samples = np.frombuffer(raw_pcm, dtype=np.int16).reshape(-1, channels)
            samples = samples.mean(axis=1).astype(np.int16)
            raw_pcm = samples.tobytes()
        if sr != 16000:
            samples = np.frombuffer(raw_pcm, dtype=np.int16)
            num_samples = int(len(samples) * 16000 / sr)
            raw_pcm = np.interp(
                np.linspace(0, len(samples) - 1, num_samples),
                np.arange(len(samples)),
                samples,
            ).astype(np.int16).tobytes()
    else:
        print("No test file provided. Generating 2.0s dummy sine wave input...")
        raw_pcm = generate_dummy_sine(2.0, 16000)

    duration = len(raw_pcm) // 2 / 16000.0
    print(f"Loaded {duration:.2f}s of 16 kHz Mono PCM audio.\n")

    all_results: dict[str, dict] = {}

    # ---- 1. Fake-server benchmark ----
    run_fake = not args.no_fake
    if run_fake:
        fake_res = await run_fake_server_benchmark(raw_pcm, args.llvc_port)
        if fake_res.get("success") and fake_res.get("out_pcm"):
            val = validate_output(
                fake_res["out_pcm"], duration, 48000, "TEST-ONLY / FAKE SERVER"
            )
            fake_res["validation"] = val
            if not (val["drift_ok"] and val["clipping_ok"] and val["silence_ok"]):
                fake_res["success"] = False
        all_results["LLVC (Fake)"] = fake_res

    # ---- 2. Real-service benchmark (suppressed if --fake-only) ----
    if args.real_ws_url and not args.fake_only:
        real_res = await run_real_service_benchmark(
            args.real_ws_url, args.real_api_key, raw_pcm
        )
        if real_res.get("success") and real_res.get("out_pcm"):
            val = validate_output(
                real_res["out_pcm"], duration, 48000, "REAL SERVICE"
            )
            real_res["validation"] = val
            if not (val["drift_ok"] and val["clipping_ok"] and val["silence_ok"]):
                real_res["success"] = False
        all_results["LLVC (Real)"] = real_res

    # ---- 3. RVC HTTP benchmark (suppressed if --fake-only) ----
    if args.rvc_endpoint and not args.fake_only:
        rvc_res = await run_rvc_benchmark(raw_pcm, args.rvc_endpoint, args.rvc_api_key)
        if rvc_res.get("success") and rvc_res.get("out_pcm"):
            val = validate_output(
                rvc_res["out_pcm"], duration, 48000, "RVC HTTP"
            )
            rvc_res["validation"] = val
            if not (val["drift_ok"] and val["clipping_ok"] and val["silence_ok"]):
                rvc_res["success"] = False
        all_results["RVC (HTTP)"] = rvc_res

    if all_results:
        print_results_table(duration, all_results)
    else:
        print("No benchmarks were run.")

    # Return non-zero exit code if any executed benchmark failed
    for lbl, res in all_results.items():
        if not res.get("success"):
            return 1
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
