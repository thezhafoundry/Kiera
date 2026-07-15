#!/usr/bin/env python3
import argparse
import asyncio
import os
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

async def run_rvc_benchmark(pcm_16k: bytes, endpoint_url: str, api_key: str) -> dict:
    print("[RVC] Starting benchmark...")
    converter = RVCVoiceConverter(
        endpoint_url=endpoint_url,
        api_key=api_key,
        pitch_shift=0
    )
    
    async def in_audio_gen():
        chunk_size = 640
        for i in range(0, len(pcm_16k), chunk_size):
            yield pcm_16k[i:i+chunk_size]
            
    start_time = time.monotonic()
    try:
        converted_chunks = []
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
            "out_bytes": len(converted_pcm)
        }
    except Exception as e:
        print(f"[RVC ERROR] {e}")
        return {"success": False, "error": str(e)}
    finally:
        await converter.close()

async def run_llvc_benchmark(pcm_16k: bytes, port: int = 18000) -> dict:
    print(f"[LLVC] Starting benchmark server on 127.0.0.1:{port}...")
    server = await websockets.serve(llvc_fake_ws_handler, "127.0.0.1", port)
    
    converter = LLVCStreamingConverter(
        ws_url=f"ws://127.0.0.1:{port}",
        api_key="bench-key",
        connect_timeout=2.0
    )
    
    # Wait for converter readiness
    ready = await converter.wait_ready(2.0)
    if not ready:
        server.close()
        await server.wait_closed()
        return {"success": False, "error": "LLVC converter ready handshake timed out"}
        
    latencies = []
    chunk_size = 640 # 20ms at 16kHz
    
    # We feed chunks and wait for output. Since LLVC converter runs duplex,
    # we feed in a separate task and read the results.
    out_pcm = bytearray()
    in_duration = len(pcm_16k) // 2 / 16000.0
    
    async def feed_input():
        for i in range(0, len(pcm_16k), chunk_size):
            chunk = pcm_16k[i:i+chunk_size]
            if len(chunk) < chunk_size:
                # pad last chunk
                chunk = chunk + b"\x00" * (chunk_size - len(chunk))
            
            # Send chunk and track time
            t_send = time.monotonic()
            
            # Create a generator containing just this chunk
            async def single_chunk_gen():
                yield chunk
                
            async for converted_chunk in converter.convert_stream(single_chunk_gen()):
                out_pcm.extend(converted_chunk)
                t_recv = time.monotonic()
                latencies.append((t_recv - t_send) * 1000.0)
                
            # Simulate real-time streaming pace (20ms)
            await asyncio.sleep(0.02)
            
    start_time = time.monotonic()
    try:
        await feed_input()
        elapsed = (time.monotonic() - start_time) * 1000.0
        
        out_samples = len(out_pcm) // 2
        out_duration = out_samples / 48000.0
        drift_ms = abs(out_duration - in_duration) * 1000.0
        
        # Calculate stats
        if latencies:
            mean_lat = np.mean(latencies)
            med_lat = np.median(latencies)
            p95_lat = np.percentile(latencies, 95)
        else:
            mean_lat = med_lat = p95_lat = 0.0
            
        return {
            "success": True,
            "mean_latency_ms": mean_lat,
            "median_latency_ms": med_lat,
            "p95_latency_ms": p95_lat,
            "drift_ms": drift_ms,
            "elapsed_ms": elapsed,
            "out_bytes": len(out_pcm)
        }
    except Exception as e:
        print(f"[LLVC ERROR] {e}")
        return {"success": False, "error": str(e)}
    finally:
        await converter.close()
        server.close()
        await server.wait_closed()

async def main_async():
    parser = argparse.ArgumentParser(description="Voice Conversion Latency and Drift Benchmarker")
    parser.add_argument("--file", help="Path to 16 kHz Mono WAV test file (optional)")
    parser.add_argument("--endpoint-url", default="http://localhost:8000/convert", help="RVC inference endpoint url")
    parser.add_argument("--api-key", default="", help="RVC API Key")
    parser.add_argument("--llvc-port", type=int, default=18000, help="Port to run fake LLVC server on")
    
    args = parser.parse_args()
    
    # 1. Load or Generate audio
    if args.file:
        raw_pcm, sr, channels = read_wav_pcm(args.file)
        # Convert to mono if stereo
        if channels > 1:
            samples = np.frombuffer(raw_pcm, dtype=np.int16).reshape(-1, channels)
            samples = samples.mean(axis=1).astype(np.int16)
            raw_pcm = samples.tobytes()
        # Resample to 16 kHz
        if sr != 16000:
            # simple linear interpolation
            samples = np.frombuffer(raw_pcm, dtype=np.int16)
            num_samples = int(len(samples) * 16000 / sr)
            raw_pcm = np.interp(
                np.linspace(0, len(samples)-1, num_samples),
                np.arange(len(samples)),
                samples
            ).astype(np.int16).tobytes()
    else:
        print("No test file provided. Generating 2.0s dummy sine wave input...")
        raw_pcm = generate_dummy_sine(2.0, 16000)
        
    duration = len(raw_pcm) // 2 / 16000.0
    print(f"Loaded {duration:.2f}s of 16 kHz Mono PCM audio.")
    
    # 2. Run Benchmarks
    rvc_results = await run_rvc_benchmark(raw_pcm, args.endpoint_url, args.api_key)
    llvc_results = await run_llvc_benchmark(raw_pcm, args.llvc_port)
    
    # 3. Print Comparison Table
    print("\n" + "="*60)
    print("           VOICE PIPELINE BENCHMARK RESULTS")
    print("="*60)
    print(f"Input Audio Duration: {duration:.2f} seconds")
    print("-"*60)
    print(f"{'Metric':<25} | {'RVC (Stable)':<15} | {'LLVC (Pilot)':<15}")
    print("-"*60)
    
    if rvc_results["success"]:
        rvc_med = f"{rvc_results['elapsed_ms']:.1f} ms"
        rvc_p95 = f"{rvc_results['elapsed_ms']:.1f} ms"
        rvc_drift = f"{rvc_results['drift_ms']:.1f} ms"
    else:
        rvc_med = rvc_p95 = rvc_drift = "FAILED"
        
    if llvc_results["success"]:
        llvc_med = f"{llvc_results['median_latency_ms']:.1f} ms"
        llvc_p95 = f"{llvc_results['p95_latency_ms']:.1f} ms"
        llvc_drift = f"{llvc_results['drift_ms']:.1f} ms"
    else:
        llvc_med = llvc_p95 = llvc_drift = "FAILED"
        
    print(f"{'Median Latency':<25} | {rvc_med:<15} | {llvc_med:<15}")
    print(f"{'p95 Latency':<25} | {rvc_p95:<15} | {llvc_p95:<15}")
    print(f"{'Duration Drift':<25} | {rvc_drift:<15} | {llvc_drift:<15}")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(main_async())
