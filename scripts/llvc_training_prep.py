#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import os
import random
import wave
import numpy as np
import httpx
from typing import List, Dict, Tuple
import sys

# Add backend directory to path so we can import converters
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from backend.converters.rvc import RVCVoiceConverter

# Helper to read raw PCM bytes from a wav file
def read_wav_pcm(path: str) -> Tuple[bytes, int, int]:
    with wave.open(path, 'rb') as w:
        params = w.getparams()
        n_channels = params.nchannels
        sampwidth = params.sampwidth
        framerate = params.framerate
        n_frames = params.nframes
        raw_data = w.readframes(n_frames)
        return raw_data, framerate, n_channels

# Helper to write raw PCM bytes to a wav file
def write_wav_pcm(path: str, pcm_bytes: bytes, sample_rate: int) -> None:
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2) # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)

def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def resample_pcm(pcm_bytes: bytes, from_sr: int, to_sr: int) -> bytes:
    if from_sr == to_sr:
        return pcm_bytes
    # Convert bytes to int16 numpy array
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    duration = len(samples) / from_sr
    num_target_samples = int(duration * to_sr)
    # Linear interpolation
    old_indices = np.arange(len(samples))
    new_indices = np.linspace(0, len(samples) - 1, num_target_samples)
    resampled_samples = np.interp(new_indices, old_indices, samples).astype(np.int16)
    return resampled_samples.tobytes()

async def process_file(
    sem: asyncio.Semaphore,
    converter: RVCVoiceConverter,
    src_path: str,
    dest_src_path: str,
    dest_tgt_path: str
) -> Dict[str, str]:
    async with sem:
        try:
            # Read PCM data
            raw_pcm, sr, channels = read_wav_pcm(src_path)
            
            # Standardize channels (mono)
            if channels > 1:
                samples = np.frombuffer(raw_pcm, dtype=np.int16).reshape(-1, channels)
                samples = samples.mean(axis=1).astype(np.int16)
                raw_pcm = samples.tobytes()
                
            # Resample to 16 kHz if necessary
            raw_pcm_16k = resample_pcm(raw_pcm, sr, 16000)
            
            # Run conversion
            # RVCVoiceConverter.convert_stream expects an async generator of bytes
            async def in_audio_gen():
                # Yield 20ms (640 bytes) chunks
                chunk_size = 640
                for i in range(0, len(raw_pcm_16k), chunk_size):
                    yield raw_pcm_16k[i:i+chunk_size]
                    
            converted_chunks = []
            async for chunk in converter.convert_stream(in_audio_gen()):
                converted_chunks.append(chunk)
                
            converted_pcm = b"".join(converted_chunks)
            if not converted_pcm:
                raise ValueError("RVC converter returned empty audio")
                
            # Write source WAV (16 kHz mono)
            write_wav_pcm(dest_src_path, raw_pcm_16k, 16000)
            
            # Write target WAV (48 kHz mono)
            write_wav_pcm(dest_tgt_path, converted_pcm, 48000)
            
            # Compute checksums
            src_sha = compute_sha256(dest_src_path)
            tgt_sha = compute_sha256(dest_tgt_path)
            
            print(f"[Prep] Processed: {os.path.basename(src_path)}")
            return {
                "src_file": os.path.abspath(dest_src_path),
                "src_sha256": src_sha,
                "tgt_file": os.path.abspath(dest_tgt_path),
                "tgt_sha256": tgt_sha
            }
        except Exception as e:
            print(f"[Prep ERROR] Failed to process {src_path}: {e}")
            return None

async def main_async():
    parser = argparse.ArgumentParser(description="LLVC Teacher-Data Parallel Prep Tool")
    parser.add_argument("--input-dir", required=True, help="Path to folder of source WAV files")
    parser.add_argument("--output-dir", required=True, help="Path to save output parallel datasets")
    parser.add_argument("--endpoint-url", required=True, help="RVC inference endpoint URL")
    parser.add_argument("--api-key", default="", help="RVC API Key")
    parser.add_argument("--pitch-shift", type=int, default=0, help="Pitch shift to apply")
    parser.add_argument("--train-ratio", type=float, default=0.8, help="Train split ratio")
    parser.add_argument("--val-ratio", type=float, default=0.1, help="Validation split ratio")
    parser.add_argument("--test-ratio", type=float, default=0.1, help="Test split ratio")
    parser.add_argument("--concurrency", type=int, default=4, help="Max concurrent RVC requests")
    parser.add_argument("--version", default="v1.0.0", help="Dataset version identifier")
    
    args = parser.parse_args()
    
    # Verify split ratios
    total_ratio = args.train_ratio + args.val_ratio + args.test_ratio
    if not np.isclose(total_ratio, 1.0):
        raise ValueError(f"Ratios must sum to 1.0 (currently sum to {total_ratio})")
        
    # Gather wav files
    wav_files = []
    for root, _, files in os.walk(args.input_dir):
        for f in files:
            if f.lower().endswith(".wav"):
                wav_files.append(os.path.join(root, f))
                
    if not wav_files:
        print("No WAV files found in input directory.")
        return
        
    # Group by speaker
    speaker_groups = {}
    for path in wav_files:
        filename = os.path.basename(path)
        if "_" in filename:
            speaker = filename.split("_")[0]
        else:
            speaker = os.path.basename(os.path.dirname(path)) or "unknown"
        if speaker not in speaker_groups:
            speaker_groups[speaker] = []
        speaker_groups[speaker].append(path)
        
    print(f"Found {len(wav_files)} files across {len(speaker_groups)} speakers.")
    
    # Shuffle and split speakers
    speakers = list(speaker_groups.keys())
    random.shuffle(speakers)
    
    num_speakers = len(speakers)
    train_idx = int(num_speakers * args.train_ratio)
    val_idx = train_idx + int(num_speakers * args.val_ratio)
    
    train_speakers = speakers[:train_idx]
    val_speakers = speakers[train_idx:val_idx]
    test_speakers = speakers[val_idx:]
    
    # Ensure every split gets at least 1 speaker if ratios allow and database is small
    if not train_speakers and num_speakers >= 1:
        train_speakers = [speakers[0]]
    if not val_speakers and num_speakers >= 2:
        val_speakers = [speakers[1]]
    if not test_speakers and num_speakers >= 3:
        test_speakers = speakers[2:]
        
    splits = {
        "train": train_speakers,
        "val": val_speakers,
        "test": test_speakers
    }
    
    print(f"Splits: {len(train_speakers)} train, {len(val_speakers)} val, {len(test_speakers)} test.")
    
    # Instantiate RVCVoiceConverter
    converter = RVCVoiceConverter(
        endpoint_url=args.endpoint_url,
        api_key=args.api_key,
        pitch_shift=args.pitch_shift
    )
    
    sem = asyncio.Semaphore(args.concurrency)
    
    # Create directories
    for split in ["train", "val", "test"]:
        os.makedirs(os.path.join(args.output_dir, split, "src"), exist_ok=True)
        os.makedirs(os.path.join(args.output_dir, split, "tgt"), exist_ok=True)
        
    tasks = []
    metadata = {"train": [], "val": [], "test": []}
    
    # Build task list
    for split, split_speakers in splits.items():
        for spk in split_speakers:
            for filepath in speaker_groups[spk]:
                filename = os.path.basename(filepath)
                dest_src = os.path.join(args.output_dir, split, "src", filename)
                dest_tgt = os.path.join(args.output_dir, split, "tgt", filename)
                
                task = process_file(sem, converter, filepath, dest_src, dest_tgt)
                tasks.append((split, task))
                
    # Run tasks with asyncio
    print(f"Starting async parallel dataset generation...")
    results = await asyncio.gather(*(t[1] for t in tasks))
    await converter.close()
    
    # Collect metadata
    success_count = 0
    for (split, _), res in zip(tasks, results):
        if res is not None:
            metadata[split].append(res)
            success_count += 1
            
    # Write checksums.json
    checksums_path = os.path.join(args.output_dir, "checksums.json")
    with open(checksums_path, "w") as f:
        json.dump(metadata, f, indent=4)
        
    # Write version.json
    version_data = {
        "version": args.version,
        "timestamp": "2026-07-15T18:32:00Z",
        "rvc_endpoint": args.endpoint_url,
        "pitch_shift": args.pitch_shift,
        "stats": {
            "total_speakers": len(speakers),
            "train_speakers": len(train_speakers),
            "val_speakers": len(val_speakers),
            "test_speakers": len(test_speakers),
            "total_processed": len(tasks),
            "success_count": success_count,
            "failed_count": len(tasks) - success_count
        }
    }
    
    version_path = os.path.join(args.output_dir, "version.json")
    with open(version_path, "w") as f:
        json.dump(version_data, f, indent=4)
        
    print(f"\nParallel dataset generation complete!")
    print(f"Saved dataset metadata to {version_path}")
    print(f"Saved file checksums to {checksums_path}")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
