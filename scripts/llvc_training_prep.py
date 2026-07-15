#!/usr/bin/env python3
"""LLVC Teacher-Data Parallel Prep Tool.

Prepares aligned 16 kHz mono source/target WAV pairs for LLVC teacher
distillation.  Source audio is resampled to 16 kHz, run through the RVC
converter (which outputs 48 kHz), then the converted audio is resampled
back to 16 kHz so both halves of each pair share the same sample rate
and duration.

Speaker splits are deterministic (seeded), mutually exclusive, and
speaker-disjoint.  At least 3 distinct speakers are required.
"""
import argparse
import asyncio
import datetime
import hashlib
import json
import os
import random
import wave
from typing import Dict, List, Optional, Tuple

import numpy as np

import sys

# Add backend directory to path so we can import converters
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from backend.converters.rvc import RVCVoiceConverter


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def read_wav_pcm(path: str) -> Tuple[bytes, int, int]:
    """Read raw PCM bytes from a WAV file."""
    with wave.open(path, 'rb') as w:
        params = w.getparams()
        n_channels = params.nchannels
        framerate = params.framerate
        raw_data = w.readframes(params.nframes)
        return raw_data, framerate, n_channels


def write_wav_pcm(path: str, pcm_bytes: bytes, sample_rate: int) -> None:
    """Write raw PCM bytes to a 16-bit mono WAV file."""
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
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
    """Resample PCM via linear interpolation."""
    if from_sr == to_sr:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    duration = len(samples) / from_sr
    num_target_samples = int(duration * to_sr)
    old_indices = np.arange(len(samples))
    new_indices = np.linspace(0, len(samples) - 1, num_target_samples)
    resampled_samples = np.interp(new_indices, old_indices, samples).astype(np.int16)
    return resampled_samples.tobytes()


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def validate_pair(
    src_pcm: bytes,
    tgt_pcm: bytes,
    src_path: str,
    tgt_path: str,
    sample_rate: int = 16000,
) -> List[str]:
    """Validate a source/target pair; return a list of warning strings."""
    warnings: List[str] = []

    src_samples = np.frombuffer(src_pcm, dtype=np.int16)
    tgt_samples = np.frombuffer(tgt_pcm, dtype=np.int16)

    # Duration alignment — 10 ms tolerance
    src_dur = len(src_samples) / sample_rate
    tgt_dur = len(tgt_samples) / sample_rate
    drift_ms = abs(src_dur - tgt_dur) * 1000.0
    if drift_ms >= 10.0:
        warnings.append(
            f"Duration drift {drift_ms:.1f} ms between src ({src_dur:.3f}s) "
            f"and tgt ({tgt_dur:.3f}s)"
        )

    # Silence check — RMS < 100 for int16 is near-silence
    for label, arr, path in [("src", src_samples, src_path), ("tgt", tgt_samples, tgt_path)]:
        if len(arr) == 0:
            warnings.append(f"{label} is empty: {path}")
            continue
        rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
        if rms < 100.0:
            warnings.append(f"{label} near-silence (RMS={rms:.1f}): {path}")

    # Clipping check — >1 % of samples at ±32767
    for label, arr, path in [("src", src_samples, src_path), ("tgt", tgt_samples, tgt_path)]:
        if len(arr) == 0:
            continue
        clipped = np.sum(np.abs(arr) >= 32767)
        pct = clipped / len(arr) * 100.0
        if pct > 1.0:
            warnings.append(f"{label} clipping ({pct:.1f}% at ±32767): {path}")

    # Corruption check — re-read both WAVs to verify they decode
    for label, path in [("src", src_path), ("tgt", tgt_path)]:
        try:
            read_wav_pcm(path)
        except Exception as exc:
            warnings.append(f"{label} corrupt WAV ({exc}): {path}")

    return warnings


# ---------------------------------------------------------------------------
# Speaker splitting
# ---------------------------------------------------------------------------

def split_speakers(
    speakers: List[str],
    train_ratio: float,
    val_ratio: float,
    test_ratio: float,
    seed: int = 42,
) -> Tuple[List[str], List[str], List[str]]:
    """Deterministic, mutually exclusive, speaker-disjoint 3-way split.

    Raises ``ValueError`` when fewer than 3 speakers are available or when
    the ratios make it impossible to give each split at least one speaker.
    """
    num = len(speakers)
    if num < 3:
        raise ValueError(
            f"Need ≥3 speakers for 3-way split, found {num}"
        )

    # Sort then shuffle for platform-stable determinism
    ordered = sorted(speakers)
    rng = random.Random(seed)
    rng.shuffle(ordered)

    # Ratio-based indices, each split guaranteed ≥ 1 speaker
    train_end = max(1, int(num * train_ratio))
    val_end = max(train_end + 1, train_end + int(num * val_ratio))

    if val_end >= num:
        raise ValueError(
            f"Cannot create 3 non-empty splits with {num} speakers and "
            f"ratios {train_ratio}/{val_ratio}/{test_ratio}"
        )

    train = ordered[:train_end]
    val = ordered[train_end:val_end]
    test = ordered[val_end:]

    # Paranoia: assert disjointness
    assert set(train) & set(val) == set(), "train/val overlap"
    assert set(train) & set(test) == set(), "train/test overlap"
    assert set(val) & set(test) == set(), "val/test overlap"
    assert len(train) > 0 and len(val) > 0 and len(test) > 0, "empty split"

    return train, val, test


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

async def process_file(
    sem: asyncio.Semaphore,
    converter: RVCVoiceConverter,
    src_path: str,
    dest_src_path: str,
    dest_tgt_path: str,
) -> Optional[Dict]:
    async with sem:
        try:
            # Read PCM data
            raw_pcm, sr, channels = read_wav_pcm(src_path)

            # Standardize to mono
            if channels > 1:
                samples = np.frombuffer(raw_pcm, dtype=np.int16).reshape(-1, channels)
                samples = samples.mean(axis=1).astype(np.int16)
                raw_pcm = samples.tobytes()

            # Resample to 16 kHz
            raw_pcm_16k = resample_pcm(raw_pcm, sr, 16000)

            # Run conversion via RVC (yields 48 kHz PCM)
            async def in_audio_gen():
                chunk_size = 640  # 20 ms at 16 kHz
                for i in range(0, len(raw_pcm_16k), chunk_size):
                    yield raw_pcm_16k[i:i + chunk_size]

            converted_chunks: List[bytes] = []
            async for chunk in converter.convert_stream(in_audio_gen()):
                converted_chunks.append(chunk)

            converted_pcm_48k = b"".join(converted_chunks)
            if not converted_pcm_48k:
                raise ValueError("RVC converter returned empty audio")

            # Resample target from 48 kHz → 16 kHz for aligned teacher pairs
            converted_pcm_16k = resample_pcm(converted_pcm_48k, 48000, 16000)

            # Byte-level alignment guard (max 20 ms = 640 bytes drift)
            if abs(len(raw_pcm_16k) - len(converted_pcm_16k)) > 640:
                print(
                    f"[Prep WARN] Large alignment gap: src={len(raw_pcm_16k)}B "
                    f"tgt={len(converted_pcm_16k)}B for {src_path}"
                )

            # Write source WAV (16 kHz mono)
            write_wav_pcm(dest_src_path, raw_pcm_16k, 16000)

            # Write target WAV (16 kHz mono)
            write_wav_pcm(dest_tgt_path, converted_pcm_16k, 16000)

            # Validate the written pair
            pair_warnings = validate_pair(
                raw_pcm_16k, converted_pcm_16k, dest_src_path, dest_tgt_path,
            )
            for w in pair_warnings:
                print(f"[Prep WARN] {w}")

            # Compute checksums
            src_sha = compute_sha256(dest_src_path)
            tgt_sha = compute_sha256(dest_tgt_path)

            print(f"[Prep] Processed: {os.path.basename(src_path)}")
            return {
                "original_path": os.path.abspath(src_path),
                "src_file": os.path.abspath(dest_src_path),
                "src_sha256": src_sha,
                "tgt_file": os.path.abspath(dest_tgt_path),
                "tgt_sha256": tgt_sha,
                "warnings": pair_warnings,
            }
        except Exception as e:
            print(f"[Prep ERROR] Failed to process {src_path}: {e}")
            return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for deterministic speaker splits")

    args = parser.parse_args()

    # Verify split ratios
    total_ratio = args.train_ratio + args.val_ratio + args.test_ratio
    if not np.isclose(total_ratio, 1.0):
        raise ValueError(f"Ratios must sum to 1.0 (currently sum to {total_ratio})")

    # Gather WAV files
    wav_files: List[str] = []
    for root, _, files in os.walk(args.input_dir):
        for f in files:
            if f.lower().endswith(".wav"):
                wav_files.append(os.path.join(root, f))

    if not wav_files:
        print("No WAV files found in input directory.")
        return

    # Group by speaker
    speaker_groups: Dict[str, List[str]] = {}
    for path in wav_files:
        filename = os.path.basename(path)
        if "_" in filename:
            speaker = filename.split("_")[0]
        else:
            speaker = os.path.basename(os.path.dirname(path)) or "unknown"
        speaker_groups.setdefault(speaker, []).append(path)

    print(f"Found {len(wav_files)} files across {len(speaker_groups)} speakers.")

    # Deterministic, speaker-disjoint split (raises on < 3 speakers)
    speakers = list(speaker_groups.keys())
    train_speakers, val_speakers, test_speakers = split_speakers(
        speakers, args.train_ratio, args.val_ratio, args.test_ratio, args.seed,
    )

    splits = {
        "train": train_speakers,
        "val": val_speakers,
        "test": test_speakers,
    }
    print(
        f"Splits (seed={args.seed}): "
        f"{len(train_speakers)} train, {len(val_speakers)} val, "
        f"{len(test_speakers)} test."
    )

    # Instantiate RVCVoiceConverter
    converter = RVCVoiceConverter(
        endpoint_url=args.endpoint_url,
        api_key=args.api_key,
        pitch_shift=args.pitch_shift,
    )

    sem = asyncio.Semaphore(args.concurrency)

    # Create directories
    for split in ("train", "val", "test"):
        os.makedirs(os.path.join(args.output_dir, split, "src"), exist_ok=True)
        os.makedirs(os.path.join(args.output_dir, split, "tgt"), exist_ok=True)

    tasks: List[Tuple[str, asyncio.Task]] = []
    metadata: Dict[str, list] = {"train": [], "val": [], "test": []}

    # Build task list — prefix filenames with speaker to prevent collisions
    seen_filenames: Dict[str, set] = {"train": set(), "val": set(), "test": set()}

    for split, split_speakers_list in splits.items():
        for spk in split_speakers_list:
            for filepath in speaker_groups[spk]:
                out_basename = f"{spk}_{os.path.basename(filepath)}"

                # Assert uniqueness within split
                if out_basename in seen_filenames[split]:
                    raise ValueError(
                        f"Filename collision in {split}: {out_basename} "
                        f"(from {filepath})"
                    )
                seen_filenames[split].add(out_basename)

                dest_src = os.path.join(args.output_dir, split, "src", out_basename)
                dest_tgt = os.path.join(args.output_dir, split, "tgt", out_basename)

                task = process_file(sem, converter, filepath, dest_src, dest_tgt)
                tasks.append((split, task))

    # Run tasks
    print("Starting async parallel dataset generation...")
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
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "rvc_endpoint": args.endpoint_url,
        "pitch_shift": args.pitch_shift,
        "seed": args.seed,
        "stats": {
            "total_speakers": len(speakers),
            "train_speakers": len(train_speakers),
            "val_speakers": len(val_speakers),
            "test_speakers": len(test_speakers),
            "total_processed": len(tasks),
            "success_count": success_count,
            "failed_count": len(tasks) - success_count,
        },
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
