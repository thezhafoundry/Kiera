#!/usr/bin/env python3
"""LLVC Teacher-Data Parallel Prep Tool.

Prepares aligned 16 kHz mono source/target WAV pairs for LLVC teacher
distillation. Source audio is resampled to 16 kHz, run through the RVC
converter (which outputs 48 kHz), then the converted audio is resampled
back to 16 kHz with anti-aliasing so both halves of each pair share the
same sample rate and duration.

Speaker splits are deterministic (seeded), mutually exclusive, and
speaker-disjoint. At least 3 distinct speakers are required. Ratios
safely allocate minimum 1 speaker per split. Invalid pairs (silence,
clipping, duration drift, corrupt audio) are quarantined and excluded from
training splits.
"""
import argparse
import asyncio
import datetime
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

import sys

# Add backend directory to path so we can import converters
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from backend.converters.rvc import RVCVoiceConverter


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def read_audio_pcm(path: str) -> Tuple[bytes, int, int]:
    """Read raw 16-bit PCM bytes from a WAV or FLAC file, checking encoding."""
    path_lower = path.lower()
    if path_lower.endswith(".flac") or not path_lower.endswith(".wav"):
        try:
            import soundfile as sf
            data, sr = sf.read(path, dtype="int16")
            n_channels = data.shape[1] if data.ndim > 1 else 1
            return data.tobytes(), sr, n_channels
        except (ImportError, Exception):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels", "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True
            )
            parts = [p.strip() for p in probe.stdout.strip().split("\n") if p.strip()]
            sr = int(parts[0]) if len(parts) >= 1 else 16000
            n_channels = int(parts[1]) if len(parts) >= 2 else 1
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-"],
                capture_output=True, check=True
            )
            return proc.stdout, sr, n_channels

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


read_wav_pcm = read_audio_pcm  # Alias for compatibility and tests


def write_wav_pcm(path: str, pcm_bytes: bytes, sample_rate: int) -> None:
    """Write raw PCM bytes to a 16-bit mono WAV file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
    """Resample PCM with anti-aliasing (scipy resample_poly if available, else FIR + interp)."""
    if from_sr == to_sr:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(samples) == 0:
        return pcm_bytes

    try:
        import scipy.signal as _signal
        g = math.gcd(from_sr, to_sr)
        up = to_sr // g
        down = from_sr // g
        resampled = _signal.resample_poly(samples.astype(np.float64), up, down)
        return np.clip(np.round(resampled), -32768, 32767).astype(np.int16).tobytes()
    except (ImportError, AttributeError):
        pass

    # Fallback: anti-aliased numpy resampler
    samples_float = samples.astype(np.float64)
    if to_sr < from_sr:
        # Apply Hamming-windowed sinc low-pass filter to prevent aliasing
        fc = (to_sr * 0.475) / from_sr
        num_taps = 61
        t = np.arange(num_taps) - (num_taps - 1) / 2.0
        h = np.sinc(2 * fc * t) * (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(num_taps) / (num_taps - 1)))
        h /= np.sum(h)
        samples_float = np.convolve(samples_float, h, mode="same")

    duration = len(samples_float) / from_sr
    num_target_samples = int(round(duration * to_sr))
    if num_target_samples == 0:
        return b""
    old_indices = np.linspace(0, len(samples_float) - 1, len(samples_float))
    new_indices = np.linspace(0, len(samples_float) - 1, num_target_samples)
    resampled = np.interp(new_indices, old_indices, samples_float)
    return np.clip(np.round(resampled), -32768, 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Output validation & quarantine guard
# ---------------------------------------------------------------------------

def validate_pair(
    src_pcm: bytes,
    tgt_pcm: bytes,
    src_path: str,
    tgt_path: str,
    sample_rate: int = 16000,
) -> Tuple[bool, List[str], Optional[str]]:
    """Validate a source/target pair.

    Returns (is_valid, warnings, reject_reason). If is_valid is False, the
    pair must not enter the training split.
    """
    warnings: List[str] = []
    reject_reason: Optional[str] = None

    src_samples = np.frombuffer(src_pcm, dtype=np.int16)
    tgt_samples = np.frombuffer(tgt_pcm, dtype=np.int16)

    # Strict duration alignment check (post trim/pad)
    if len(src_samples) != len(tgt_samples):
        reject_reason = f"Sample count mismatch: src={len(src_samples)} vs tgt={len(tgt_samples)}"
        warnings.append(f"[FAIL] {reject_reason}")
        return False, warnings, reject_reason

    src_dur = len(src_samples) / sample_rate
    tgt_dur = len(tgt_samples) / sample_rate
    drift_ms = abs(src_dur - tgt_dur) * 1000.0
    if drift_ms >= 10.0:
        reject_reason = f"Duration drift {drift_ms:.1f} ms >= 10 ms"
        warnings.append(f"[FAIL] {reject_reason}")
        return False, warnings, reject_reason

    # Silence check — RMS < 100 for int16 is near-silence
    for label, arr, path in [("src", src_samples, src_path), ("tgt", tgt_samples, tgt_path)]:
        if len(arr) == 0:
            reject_reason = f"{label} is empty"
            warnings.append(f"[FAIL] {reject_reason} ({path})")
            return False, warnings, reject_reason
        rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
        if rms < 100.0:
            reject_reason = f"{label} near-silence (RMS={rms:.1f} < 100)"
            warnings.append(f"[FAIL] {reject_reason} ({path})")
            return False, warnings, reject_reason

    # Clipping check — >1% of samples at ±32767
    for label, arr, path in [("src", src_samples, src_path), ("tgt", tgt_samples, tgt_path)]:
        if len(arr) == 0:
            continue
        arr_i32 = arr.astype(np.int32)
        clipped = np.sum(np.abs(arr_i32) >= 32767)
        pct = clipped / len(arr) * 100.0
        if pct > 1.0:
            reject_reason = f"{label} excessive clipping ({pct:.1f}% >= 1.0%)"
            warnings.append(f"[FAIL] {reject_reason} ({path})")
            return False, warnings, reject_reason

    # Corruption check — verify both WAV paths decode cleanly if written
    for label, path in [("src", src_path), ("tgt", tgt_path)]:
        if os.path.exists(path):
            try:
                read_wav_pcm(path)
            except Exception as exc:
                reject_reason = f"{label} corrupt WAV ({exc})"
                warnings.append(f"[FAIL] {reject_reason} ({path})")
                return False, warnings, reject_reason

    return True, warnings, None


# ---------------------------------------------------------------------------
# Speaker splitting & path uniqueness
# ---------------------------------------------------------------------------

def load_manifest(manifest_path: str) -> Dict[str, str]:
    """Load a speaker manifest from a JSON or CSV/TSV file mapping path -> speaker_id."""
    manifest_map: Dict[str, str] = {}
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")
    if manifest_path.lower().endswith(".json"):
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                manifest_map = {str(k): str(v) for k, v in data.items()}
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "path" in item and "speaker" in item:
                        manifest_map[str(item["path"])] = str(item["speaker"])
    else:
        with open(manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                sep = "\t" if "\t" in line else ","
                parts = line.split(sep, 1)
                if len(parts) == 2:
                    manifest_map[parts[0].strip()] = parts[1].strip()
    return manifest_map


def get_speaker(
    filepath: str,
    input_dir: str,
    manifest_map: Optional[Dict[str, str]] = None,
    speaker_regex: Optional[re.Pattern] = None,
) -> str:
    """Extract speaker ID from file path using manifest, regex, or structural heuristics."""
    abs_path = os.path.abspath(filepath)
    try:
        rel_path = os.path.relpath(abs_path, os.path.abspath(input_dir))
    except ValueError:
        rel_path = abs_path

    # 1. Manifest check (match exact path, relative path, or basename)
    if manifest_map:
        if abs_path in manifest_map:
            return manifest_map[abs_path]
        if filepath in manifest_map:
            return manifest_map[filepath]
        if rel_path in manifest_map:
            return manifest_map[rel_path]
        if os.path.basename(filepath) in manifest_map:
            return manifest_map[os.path.basename(filepath)]

    # 2. Configurable regex on relative path or filename
    if speaker_regex:
        m = speaker_regex.search(rel_path)
        if not m:
            m = speaker_regex.search(filepath)
        if m:
            return m.group(1) if m.groups() else m.group(0)

    # 3. Structural directory or filename extraction
    parts = Path(rel_path).parts
    if len(parts) > 1:
        # In multi-level structures like LibriSpeech (speaker/chapter/file.flac)
        # or standard speaker_dir/file.wav, the first directory component is the speaker ID.
        return parts[0]

    base = os.path.basename(filepath)
    if "_" in base:
        return base.split("_")[0]
    if "-" in base:
        return base.split("-")[0]
    return "default_speaker"


def split_speakers(
    speakers: List[str],
    train_ratio: float,
    val_ratio: float,
    test_ratio: float,
    seed: int = 42,
) -> Tuple[List[str], List[str], List[str]]:
    """Deterministic, mutually exclusive, speaker-disjoint 3-way split.

    Guarantees at least 1 speaker per split whenever len(speakers) >= 3,
    even with ratios like 0.8/0.1/0.1 on 3-5 speakers.
    """
    num = len(speakers)
    if num < 3:
        raise ValueError(
            f"Need ≥3 speakers for 3-way split, found {num}"
        )
    if train_ratio <= 0 or val_ratio <= 0 or test_ratio <= 0:
        raise ValueError("All split ratios must be strictly positive (> 0)")

    # Sort then shuffle for platform-stable determinism
    ordered = sorted(speakers)
    rng = random.Random(seed)
    rng.shuffle(ordered)

    # Base allocation: exactly 1 for each split to guarantee non-empty
    counts = [1, 1, 1]
    remaining = num - 3
    if remaining > 0:
        total_ratio = train_ratio + val_ratio + test_ratio
        ratios = [train_ratio / total_ratio, val_ratio / total_ratio, test_ratio / total_ratio]
        exact = [r * remaining for r in ratios]
        added = [int(x) for x in exact]
        for i in range(3):
            counts[i] += added[i]
        leftover = remaining - sum(added)
        if leftover > 0:
            remainders = [(exact[i] - added[i], i) for i in range(3)]
            remainders.sort(key=lambda x: (-x[0], x[1]))
            for k in range(leftover):
                idx = remainders[k % 3][1]
                counts[idx] += 1

    train_end = counts[0]
    val_end = train_end + counts[1]

    train = ordered[:train_end]
    val = ordered[train_end:val_end]
    test = ordered[val_end:]

    assert set(train) & set(val) == set(), "train/val overlap"
    assert set(train) & set(test) == set(), "train/test overlap"
    assert set(val) & set(test) == set(), "val/test overlap"
    assert len(train) > 0 and len(val) > 0 and len(test) > 0, "empty split"

    return train, val, test


def get_unique_output_basename(filepath: str, input_dir: str, speaker: str) -> str:
    """Generate a stable, collision-free basename preserving speaker and uniqueness."""
    try:
        rel_path = os.path.relpath(os.path.abspath(filepath), os.path.abspath(input_dir))
    except ValueError:
        rel_path = os.path.abspath(filepath)
    path_hash = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:8]
    stem, _ = os.path.splitext(os.path.basename(filepath))
    return f"{speaker}_{path_hash}_{stem}.wav"


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

async def process_file(
    sem: asyncio.Semaphore,
    converter: RVCVoiceConverter,
    src_path: str,
    dest_src_path: str,
    dest_tgt_path: str,
    quarantine_src_path: str,
    quarantine_tgt_path: str,
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

            # Resample input to 16 kHz if necessary
            raw_pcm_16k = resample_pcm(raw_pcm, sr, 16000)

            # Run through RVC converter
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

            # Exact sample alignment guard (trim/pad if gap <= 20 ms / 640 bytes)
            diff = len(converted_pcm_16k) - len(raw_pcm_16k)
            if 0 < abs(diff) <= 640:
                if diff > 0:
                    converted_pcm_16k = converted_pcm_16k[:len(raw_pcm_16k)]
                else:
                    converted_pcm_16k = converted_pcm_16k + b"\x00" * (-diff)

            # Validate the pair before writing to main split directory
            is_valid, pair_warnings, reject_reason = validate_pair(
                raw_pcm_16k, converted_pcm_16k, dest_src_path, dest_tgt_path,
            )
            for w in pair_warnings:
                print(f"[Prep WARN] {w}")

            if not is_valid:
                print(
                    f"[Prep REJECT] Quarantining {os.path.basename(src_path)}: {reject_reason}"
                )
                write_wav_pcm(quarantine_src_path, raw_pcm_16k, 16000)
                write_wav_pcm(quarantine_tgt_path, converted_pcm_16k, 16000)
                return {
                    "status": "rejected",
                    "reason": reject_reason,
                    "original_path": os.path.abspath(src_path),
                    "quarantine_src": os.path.abspath(quarantine_src_path),
                    "quarantine_tgt": os.path.abspath(quarantine_tgt_path),
                    "warnings": pair_warnings,
                }

            # Write valid source and target WAVs (16 kHz mono)
            write_wav_pcm(dest_src_path, raw_pcm_16k, 16000)
            write_wav_pcm(dest_tgt_path, converted_pcm_16k, 16000)

            src_sha = compute_sha256(dest_src_path)
            tgt_sha = compute_sha256(dest_tgt_path)

            print(f"[Prep] Processed: {os.path.basename(src_path)}")
            return {
                "status": "valid",
                "original_path": os.path.abspath(src_path),
                "src_file": os.path.abspath(dest_src_path),
                "src_sha256": src_sha,
                "tgt_file": os.path.abspath(dest_tgt_path),
                "tgt_sha256": tgt_sha,
                "warnings": pair_warnings,
            }
        except Exception as e:
            print(f"[Prep ERROR] Failed to process {src_path}: {e}")
            return {
                "status": "error",
                "reason": str(e),
                "original_path": os.path.abspath(src_path),
            }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async():
    parser = argparse.ArgumentParser(description="LLVC Teacher-Data Parallel Prep Tool")
    parser.add_argument("--input-dir", required=True, help="Path to folder of source WAV files")
    parser.add_argument("--output-dir", required=True, help="Path to save output parallel datasets")
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("RVC_ENDPOINT_URL", ""),
        help="RVC inference endpoint URL (env: RVC_ENDPOINT_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("RVC_API_KEY", ""),
        help="RVC API Key (env: RVC_API_KEY)",
    )
    parser.add_argument("--pitch-shift", type=int, default=0, help="Pitch shift to apply")
    parser.add_argument("--train-ratio", type=float, default=0.8, help="Train split ratio")
    parser.add_argument("--val-ratio", type=float, default=0.1, help="Validation split ratio")
    parser.add_argument("--test-ratio", type=float, default=0.1, help="Test/dev split ratio")
    parser.add_argument("--concurrency", type=int, default=4, help="Max concurrent RVC requests")
    parser.add_argument("--version", default="v1.0.0", help="Dataset version identifier")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for deterministic speaker splits")
    parser.add_argument(
        "--layout",
        choices=["official", "legacy"],
        default="official",
        help="Output directory layout: 'official' (train/val/dev with *_original.wav/*_converted.wav) "
             "or 'legacy' (train/val/test with src/tgt folders)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Clean existing files in split directories before generating new dataset",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="Path to JSON/CSV/TSV manifest mapping file paths to speaker IDs",
    )
    parser.add_argument(
        "--speaker-regex",
        default="",
        help="Regex pattern with capture group to extract speaker ID from relative file path",
    )
    args = parser.parse_args()

    if not args.endpoint_url:
        parser.error("--endpoint-url (or RVC_ENDPOINT_URL env var) is required")

    manifest_map = load_manifest(args.manifest) if args.manifest else None
    speaker_regex = re.compile(args.speaker_regex) if args.speaker_regex else None

    # Discover audio files (WAV and FLAC) and group by speaker
    audio_files = []
    for root, _, files in os.walk(args.input_dir):
        for f in sorted(files):
            if f.lower().endswith((".wav", ".flac")):
                audio_files.append(os.path.join(root, f))

    if not audio_files:
        print(f"No WAV or FLAC files found in {args.input_dir}")
        return

    speaker_groups: Dict[str, List[str]] = {}
    for f in audio_files:
        spk = get_speaker(f, args.input_dir, manifest_map=manifest_map, speaker_regex=speaker_regex)
        speaker_groups.setdefault(spk, []).append(f)

    print(f"Found {len(audio_files)} files across {len(speaker_groups)} speakers.")

    # Deterministic, speaker-disjoint split (raises on < 3 speakers)
    speakers = list(speaker_groups.keys())
    train_speakers, val_speakers, third_speakers = split_speakers(
        speakers, args.train_ratio, args.val_ratio, args.test_ratio, args.seed,
    )

    if args.layout == "official":
        splits = {"train": train_speakers, "val": val_speakers, "dev": third_speakers}
    else:
        splits = {"train": train_speakers, "val": val_speakers, "test": third_speakers}

    print(
        f"Splits (seed={args.seed}, layout={args.layout}): "
        f"{len(train_speakers)} train, {len(val_speakers)} val, "
        f"{len(third_speakers)} {list(splits.keys())[2]}."
    )

    # Output directory contamination check & cleanup
    for split in splits.keys():
        split_path = os.path.join(args.output_dir, split)
        if os.path.exists(split_path):
            existing_files = [
                p for p in Path(split_path).rglob("*")
                if p.is_file() and p.suffix.lower() in (".wav", ".flac")
            ]
            if existing_files:
                if not args.overwrite:
                    raise FileExistsError(
                        f"Output directory '{args.output_dir}' (split '{split}') already contains audio files. "
                        "Reusing it can contaminate splits when globbing during training. "
                        "Pass --overwrite to clean existing split files or choose a new output directory."
                    )
                shutil.rmtree(split_path)

    converter = RVCVoiceConverter(
        endpoint_url=args.endpoint_url,
        api_key=args.api_key,
        pitch_shift=args.pitch_shift,
    )

    sem = asyncio.Semaphore(args.concurrency)

    # Create directories
    for split in splits.keys():
        if args.layout == "official":
            os.makedirs(os.path.join(args.output_dir, split), exist_ok=True)
            os.makedirs(os.path.join(args.output_dir, "quarantine", split), exist_ok=True)
        else:
            os.makedirs(os.path.join(args.output_dir, split, "src"), exist_ok=True)
            os.makedirs(os.path.join(args.output_dir, split, "tgt"), exist_ok=True)
            os.makedirs(os.path.join(args.output_dir, "quarantine", split, "src"), exist_ok=True)
            os.makedirs(os.path.join(args.output_dir, "quarantine", split, "tgt"), exist_ok=True)

    tasks: List[Tuple[str, asyncio.Task]] = []
    metadata: Dict[str, list] = {s: [] for s in splits.keys()}
    metadata["rejected"] = []
    metadata["error"] = []

    seen_filenames: Dict[str, set] = {s: set() for s in splits.keys()}

    for split, split_speakers_list in splits.items():
        for spk in split_speakers_list:
            for filepath in speaker_groups[spk]:
                out_basename = get_unique_output_basename(filepath, args.input_dir, spk)

                if out_basename in seen_filenames[split]:
                    raise ValueError(
                        f"Filename collision in {split}: {out_basename} (from {filepath})"
                    )
                seen_filenames[split].add(out_basename)

                if args.layout == "official":
                    stem, ext = os.path.splitext(out_basename)
                    dest_src = os.path.join(args.output_dir, split, f"{stem}_original{ext}")
                    dest_tgt = os.path.join(args.output_dir, split, f"{stem}_converted{ext}")
                    quarantine_src = os.path.join(args.output_dir, "quarantine", split, f"{stem}_original{ext}")
                    quarantine_tgt = os.path.join(args.output_dir, "quarantine", split, f"{stem}_converted{ext}")
                else:
                    dest_src = os.path.join(args.output_dir, split, "src", out_basename)
                    dest_tgt = os.path.join(args.output_dir, split, "tgt", out_basename)
                    quarantine_src = os.path.join(args.output_dir, "quarantine", split, "src", out_basename)
                    quarantine_tgt = os.path.join(args.output_dir, "quarantine", split, "tgt", out_basename)

                task = process_file(
                    sem, converter, filepath, dest_src, dest_tgt, quarantine_src, quarantine_tgt
                )
                tasks.append((split, task))

    # Run tasks
    print("Starting async parallel dataset generation...")
    results = await asyncio.gather(*(t[1] for t in tasks))
    await converter.close()

    success_count = 0
    rejected_count = 0
    error_count = 0

    for (split, _), res in zip(tasks, results):
        if res is not None:
            status = res.get("status", "error")
            if status == "valid":
                metadata[split].append(res)
                success_count += 1
            elif status == "rejected":
                metadata["rejected"].append(res)
                rejected_count += 1
            else:
                metadata["error"].append(res)
                error_count += 1

    checksums_path = os.path.join(args.output_dir, "checksums.json")
    with open(checksums_path, "w") as f:
        json.dump(metadata, f, indent=4)

    version_data = {
        "version": args.version,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "layout": args.layout,
        "rvc_endpoint": args.endpoint_url,
        "pitch_shift": args.pitch_shift,
        "seed": args.seed,
        "stats": {
            "total_speakers": len(speakers),
            "train_speakers": len(train_speakers),
            "val_speakers": len(val_speakers),
            "third_split_speakers": len(third_speakers),
            "total_processed": len(tasks),
            "success_count": success_count,
            "rejected_count": rejected_count,
            "error_count": error_count,
        },
    }

    version_path = os.path.join(args.output_dir, "version.json")
    with open(version_path, "w") as f:
        json.dump(version_data, f, indent=4)

    print("\nParallel dataset generation complete!")
    print(f"Stats: {success_count} valid, {rejected_count} quarantined/rejected, {error_count} errors.")
    print(f"Saved dataset metadata to {version_path}")
    print(f"Saved file checksums to {checksums_path}")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
