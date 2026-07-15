#!/usr/bin/env python3
"""LLVC / RVC voice-conversion benchmark script.

Fixes applied vs. the original version:
1.  Fake-server benchmark uses ONE long-lived convert_stream() call (not per-frame).
2.  Real-service benchmark added (--real-ws-url / --real-api-key).
3.  Fake-server output clearly labeled [TEST-ONLY / FAKE SERVER].
4.  Output validation (drift, clipping, silence) after every benchmark run.
5.  RVC HTTP benchmark only runs when --rvc-endpoint is explicitly passed.
"""
import argparse
import asyncio
import contextlib
import collections
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
        n_channels = params.nchannels
        sampwidth = params.sampwidth
        framerate = params.framerate
        n_frames = params.nframes
        raw_data = w.readframes(n_frames)
        return raw_data, framerate, n_channels

# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def validate_output(pcm_bytes: bytes, input_duration_s: float,
                    sample_rate: int, label: str) -> dict:
    """Validate converted PCM output for drift, clipping, and silence.

    Returns a dict with drift_ok, clipping_ok, silence_ok, and a list of
    warning strings.
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
            f"[{label}] WARN: clipping detected — {clip_ratio * 100:.2f}% of samples at ±32767"
        )
        clipping_ok = False

    # --- Silence ---
    if n_samples > 0:
        rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
    else:
        rms = 0.0

    silence_ok = True
    if rms < 100.0:
        warnings.append(f"[{label}] WARN: output may be silent — RMS {rms:.1f} (< 100)")
        silence_ok = False

    # Print warnings inline
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
    """Run the RVC HTTP-POST benchmark.  Offline-test-only."""
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
# LLVC fake-server (test-only) benchmark
# ---------------------------------------------------------------------------

async def run_fake_server_benchmark(pcm_16k: bytes, port: int = 18000) -> dict:
    """Run LLVC benchmark against a local fake WS server.

    For automated testing only — does not measure real service performance.
    Uses ONE long-lived convert_stream() call for the entire audio.
    """
    print(f"[TEST-ONLY / FAKE SERVER] Starting benchmark server on 127.0.0.1:{port}...")
    server = await websockets.serve(llvc_fake_ws_handler, "127.0.0.1", port)

    converter = LLVCStreamingConverter(
        ws_url=f"ws://127.0.0.1:{port}",
        api_key="bench-key",
        connect_timeout=5.0,
    )

    chunk_size = 640  # 20 ms at 16 kHz
    in_duration = len(pcm_16k) // 2 / 16000.0
    send_times: collections.deque[float] = collections.deque()
    latencies: list[float] = []
    out_pcm = bytearray()

    async def input_gen():
        """Yield all 640-byte frames at simulated real-time pace (20 ms)."""
        for i in range(0, len(pcm_16k), chunk_size):
            chunk = pcm_16k[i:i + chunk_size]
            if len(chunk) < chunk_size:
                chunk = chunk + b"\x00" * (chunk_size - len(chunk))
            send_times.append(time.monotonic())
            yield chunk
            await asyncio.sleep(0.02)  # simulate real-time 20 ms cadence

    start_time = time.monotonic()
    try:
        async with contextlib.aclosing(converter.convert_stream(input_gen())) as stream:
            async for converted_chunk in stream:
                t_recv = time.monotonic()
                out_pcm.extend(converted_chunk)
                if send_times:
                    t_send = send_times.popleft()
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

        print(f"[TEST-ONLY] Median latency : {med_lat:.1f} ms")
        print(f"[TEST-ONLY] p95 latency    : {p95_lat:.1f} ms")
        print(f"[TEST-ONLY] p99 latency    : {p99_lat:.1f} ms")
        print(f"[TEST-ONLY] Elapsed        : {elapsed:.1f} ms")
        print(f"[TEST-ONLY] Drift          : {drift_ms:.1f} ms")
        print(f"[TEST-ONLY] Output bytes   : {len(out_pcm)}")

        return {
            "success": True,
            "median_latency_ms": med_lat,
            "p95_latency_ms": p95_lat,
            "p99_latency_ms": p99_lat,
            "elapsed_ms": elapsed,
            "drift_ms": drift_ms,
            "out_bytes": len(out_pcm),
            "out_pcm": bytes(out_pcm),
        }
    except Exception as e:
        print(f"[TEST-ONLY / FAKE SERVER ERROR] {e}")
        return {"success": False, "error": str(e)}
    finally:
        await converter.close()
        server.close()
        await server.wait_closed()

# ---------------------------------------------------------------------------
# LLVC real-service benchmark
# ---------------------------------------------------------------------------

async def run_real_service_benchmark(ws_url: str, api_key: str,
                                     pcm_16k: bytes) -> dict:
    """Run LLVC benchmark against a real remote WS service.

    Uses ONE long-lived convert_stream() call for the entire audio.
    """
    print(f"[REAL SERVICE] Connecting to {ws_url} ...")

    converter = LLVCStreamingConverter(
        ws_url=ws_url,
        api_key=api_key,
        connect_timeout=10.0,
    )

    chunk_size = 640  # 20 ms at 16 kHz
    in_duration = len(pcm_16k) // 2 / 16000.0
    send_times: collections.deque[float] = collections.deque()
    latencies: list[float] = []
    out_pcm = bytearray()

    async def input_gen():
        """Yield all 640-byte frames at simulated real-time pace (20 ms)."""
        for i in range(0, len(pcm_16k), chunk_size):
            chunk = pcm_16k[i:i + chunk_size]
            if len(chunk) < chunk_size:
                chunk = chunk + b"\x00" * (chunk_size - len(chunk))
            send_times.append(time.monotonic())
            yield chunk
            await asyncio.sleep(0.02)

    start_time = time.monotonic()
    try:
        async with contextlib.aclosing(converter.convert_stream(input_gen())) as stream:
            async for converted_chunk in stream:
                t_recv = time.monotonic()
                out_pcm.extend(converted_chunk)
                if send_times:
                    t_send = send_times.popleft()
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

        print(f"[REAL SERVICE] Median latency : {med_lat:.1f} ms")
        print(f"[REAL SERVICE] p95 latency    : {p95_lat:.1f} ms")
        print(f"[REAL SERVICE] p99 latency    : {p99_lat:.1f} ms")
        print(f"[REAL SERVICE] Elapsed        : {elapsed:.1f} ms")
        print(f"[REAL SERVICE] Drift          : {drift_ms:.1f} ms")
        print(f"[REAL SERVICE] Output bytes   : {len(out_pcm)}")

        return {
            "success": True,
            "median_latency_ms": med_lat,
            "p95_latency_ms": p95_lat,
            "p99_latency_ms": p99_lat,
            "elapsed_ms": elapsed,
            "drift_ms": drift_ms,
            "out_bytes": len(out_pcm),
            "out_pcm": bytes(out_pcm),
        }
    except Exception as e:
        print(f"[REAL SERVICE ERROR] {e}")
        return {"success": False, "error": str(e)}
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

    # Column headers
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

    # Validation summary
    print("-" * 72)
    for lbl in labels:
        r = results[lbl]
        v = r.get("validation")
        if v:
            ok = v["drift_ok"] and v["clipping_ok"] and v["silence_ok"]
            status = "PASS" if ok else "WARN"
            print(f"  {lbl} validation: {status}")
            for w in v.get("warnings", []):
                print(f"    {w}")

    print("=" * 72)

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main_async() -> None:
    parser = argparse.ArgumentParser(
        description="LLVC / RVC Voice Conversion Latency & Drift Benchmarker"
    )
    parser.add_argument("--file", help="Path to 16 kHz Mono WAV test file (optional)")
    parser.add_argument("--fake-only", action="store_true",
                        help="Run only the fake-server benchmark (default behaviour)")
    parser.add_argument("--real-ws-url",
                        help="WebSocket URL for real LLVC service benchmark")
    parser.add_argument("--real-api-key", default="",
                        help="API key for the real LLVC service")
    parser.add_argument("--llvc-port", type=int, default=18000,
                        help="Port for local fake LLVC server (default 18000)")
    parser.add_argument("--rvc-endpoint",
                        help="RVC HTTP endpoint URL (enables RVC HTTP benchmark)")
    parser.add_argument("--rvc-api-key", default="",
                        help="RVC API key (used with --rvc-endpoint)")

    args = parser.parse_args()

    # ---- Load or generate audio ----
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

    # ---- 1. Fake-server benchmark (always, unless only --real-ws-url) ----
    run_fake = True
    if args.real_ws_url and not args.fake_only:
        # If the user passed --real-ws-url without --fake-only, still run fake
        # unless they explicitly omitted it.  We always run fake by default.
        pass

    if run_fake:
        fake_res = await run_fake_server_benchmark(raw_pcm, args.llvc_port)
        if fake_res.get("success") and fake_res.get("out_pcm"):
            fake_res["validation"] = validate_output(
                fake_res["out_pcm"], duration, 48000, "TEST-ONLY / FAKE SERVER"
            )
        all_results["LLVC (Fake)"] = fake_res

    # ---- 2. Real-service benchmark (only with --real-ws-url) ----
    if args.real_ws_url:
        real_res = await run_real_service_benchmark(
            args.real_ws_url, args.real_api_key, raw_pcm
        )
        if real_res.get("success") and real_res.get("out_pcm"):
            real_res["validation"] = validate_output(
                real_res["out_pcm"], duration, 48000, "REAL SERVICE"
            )
        all_results["LLVC (Real)"] = real_res

    # ---- 3. RVC HTTP benchmark (only with --rvc-endpoint) ----
    if args.rvc_endpoint:
        rvc_res = await run_rvc_benchmark(raw_pcm, args.rvc_endpoint, args.rvc_api_key)
        if rvc_res.get("success") and rvc_res.get("out_pcm"):
            rvc_res["validation"] = validate_output(
                rvc_res["out_pcm"], duration, 48000, "RVC HTTP"
            )
        all_results["RVC (HTTP)"] = rvc_res

    # ---- Print comparison table ----
    if all_results:
        print_results_table(duration, all_results)
    else:
        print("No benchmarks were run.")


if __name__ == "__main__":
    asyncio.run(main_async())
