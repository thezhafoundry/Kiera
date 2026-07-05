"""TRT-backed re-implementation of RVC's Pipeline.vc for Keira's streaming worker.

Pure-NumPy helpers + TRTVoicePipeline (ORT/TensorRT sessions). This module must
stay importable WITHOUT Modal or GPU libs at module level, so the helper half is
unit-testable locally. It is mounted into the container via
.add_local_python_source("trt_pipeline") — see worker.py's image (Modal does NOT
auto-bundle sibling modules; confirmed incident 2026-07-03 with streaming.py).

Architecture:
  pcm_int16 -> pad_to_canonical -> [HuBERT engine] -> FAISS mix (NumPy)
  -> 2x upsample -> [RMVPE engine + mel frontend] -> f0_to_coarse -> apply_protect
  -> [Generator engine] -> trim pad -> change_rms -> int16 out

Constants must mirror export_onnx.py exactly — any mismatch causes shape errors.
"""
import numpy as np

# ---- Canonical static shapes (MUST mirror export_onnx.py exactly) ----
SR_IN = 16000
SR_OUT = 48000
CANONICAL_IN = 22400          # 1400 ms of 16kHz audio (BLOCK_MS=1000 + CONTEXT_MS=400)
TRT_T_PAD = 16000             # fixed reflect pad each side (x_pad=1 in RVC terms)
PADDED_IN = CANONICAL_IN + 2 * TRT_T_PAD      # 54400 samples fed into HuBERT
HUBERT_FRAMES = PADDED_IN // 320              # 170 HuBERT output frames
GEN_FRAMES = HUBERT_FRAMES * 2                # 340 frames after 2x interpolation
OUT_PADDED_48K = GEN_FRAMES * 480             # 163200 generator output samples
T_PAD_TGT = TRT_T_PAD * 3                     # 48000 samples output-space pad
OUT_48K = OUT_PADDED_48K - 2 * T_PAD_TGT      # 67200 usable output == 3 * CANONICAL_IN
MEL_FRAMES = PADDED_IN // 160 + 1             # 341 mel frames (center=True, hop=160)
MEL_FRAMES_PADDED = ((MEL_FRAMES + 31) // 32) * 32   # 352 (padded to multiple of 32)

# RVC f0 mapping constants (verbatim from RVC/infer/modules/vc/pipeline.py)
F0_MIN, F0_MAX = 50.0, 1100.0
F0_MEL_MIN = 1127.0 * np.log(1.0 + F0_MIN / 700.0)
F0_MEL_MAX = 1127.0 * np.log(1.0 + F0_MAX / 700.0)

# RMVPE cents mapping (verbatim from RVC/infer/lib/rmvpe.py)
_CENTS_MAPPING = 20.0 * np.arange(360) + 1997.3794084376191


# ---------------------------------------------------------------------------
# Pure-NumPy helpers (Task 5) — unit-testable locally, no GPU required
# ---------------------------------------------------------------------------

def pad_to_canonical(pcm_int16: np.ndarray) -> tuple:
    """int16 block (<= CANONICAL_IN samples) -> (float32[PADDED_IN], left_zero_pad).

    Short first-of-session blocks are zero-filled at the HEAD of the canonical
    region (they lack left context); the fixed TRT_T_PAD reflect pad is then
    applied around that. left_zero_pad lets the caller trim 3x that many
    samples off the 48 kHz output head.

    Raises ValueError if the block exceeds CANONICAL_IN.
    """
    pcm_int16 = np.asarray(pcm_int16, dtype=np.int16)
    if len(pcm_int16) > CANONICAL_IN:
        raise ValueError(
            f"block of {len(pcm_int16)} samples exceeds canonical max {CANONICAL_IN}"
        )
    zpad = CANONICAL_IN - len(pcm_int16)
    audio = pcm_int16.astype(np.float32) / 32768.0
    # Zero-fill at head so the canonical region is always exactly CANONICAL_IN samples
    canonical = np.concatenate([np.zeros(zpad, dtype=np.float32), audio])
    # Reflect pad both sides for HuBERT context
    padded = np.pad(canonical, (TRT_T_PAD, TRT_T_PAD), mode="reflect")
    return padded, zpad


def f0_to_coarse(f0: np.ndarray) -> np.ndarray:
    """Hz -> RVC's 1..255 mel-quantized pitch codes (port of Pipeline.get_f0's tail)."""
    f0_mel = 1127.0 * np.log(1.0 + np.asarray(f0, dtype=np.float64) / 700.0)
    voiced = f0_mel > 0
    f0_mel[voiced] = (
        (f0_mel[voiced] - F0_MEL_MIN) * 254.0 / (F0_MEL_MAX - F0_MEL_MIN) + 1.0
    )
    f0_mel[f0_mel <= 1.0] = 1.0
    f0_mel[f0_mel > 255.0] = 255.0
    return np.rint(f0_mel).astype(np.int64)


def decode_f0(hidden: np.ndarray, thred: float = 0.03) -> np.ndarray:
    """RMVPE hidden [1, MEL_FRAMES_PADDED, 360] -> f0 Hz [MEL_FRAMES].

    Port of RMVPE.decode / to_local_average_cents: local weighted average of
    cents in a +/-4 bin window around the argmax, zeroed where peak < thred.
    Slices the output back to MEL_FRAMES (341), discarding the 32-aligned padding.
    """
    salience = np.asarray(hidden, dtype=np.float32)[0, :MEL_FRAMES]   # [T, 360]
    padded = np.pad(salience, ((0, 0), (4, 4)))                        # [T, 368]
    centers = np.argmax(salience, axis=1)                              # [T]
    f0 = np.zeros(MEL_FRAMES, dtype=np.float64)
    for i, c in enumerate(centers):
        window = padded[i, c: c + 9]                   # 9-bin window centered on c
        cents_win = _CENTS_MAPPING[max(0, c - 4): c + 5]
        if len(cents_win) < len(window):               # edge bins: trim window to match
            window = window[: len(cents_win)]
        denom = window.sum()
        if denom > 0:
            cents = float((window * cents_win).sum() / denom)
            f0[i] = 10.0 * 2.0 ** (cents / 1200.0)
    f0[salience.max(axis=1) <= thred] = 0.0
    return f0


def change_rms(source_16k: np.ndarray, out_48k: np.ndarray, rate: float) -> np.ndarray:
    """Port of RVC pipeline.py change_rms: blend output loudness envelope toward source.

    rate=1.0 returns out_48k unchanged (output's own envelope preserved).
    rate=0.0 would fully match the input's envelope (never used in practice).
    """
    if rate >= 1.0:
        return out_48k
    import librosa
    rms1 = librosa.feature.rms(
        y=source_16k.astype(np.float32),
        frame_length=SR_IN // 2 * 2,
        hop_length=SR_IN // 2,
    )[0]
    rms2 = librosa.feature.rms(
        y=out_48k.astype(np.float32),
        frame_length=SR_OUT // 2 * 2,
        hop_length=SR_OUT // 2,
    )[0]
    n = len(out_48k)
    rms1 = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(rms1)), rms1)
    rms2 = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(rms2)), rms2)
    rms2 = np.maximum(rms2, 1e-6)
    return out_48k * (rms1 / rms2) ** (1.0 - rate)


def apply_protect(
    feats: np.ndarray,
    feats_raw: np.ndarray,
    pitchf: np.ndarray,
    protect: float,
) -> np.ndarray:
    """Port of Pipeline.vc consonant protection.

    On unvoiced frames (pitchf == 0), blends index-mixed feats back toward the
    raw HuBERT features to preserve consonant clarity.
    protect >= 0.5 disables the protection entirely (RVC convention).
    """
    if protect >= 0.5:
        return feats
    mask = (np.asarray(pitchf) == 0.0)[None, :, None]     # [1, T, 1] broadcast
    blended = feats * protect + feats_raw * (1.0 - protect)
    return np.where(mask, blended, feats).astype(np.float32)


# ---------------------------------------------------------------------------
# TRTVoicePipeline — ORT sessions + full block conversion (Task 6)
# ---------------------------------------------------------------------------

class TRTVoicePipeline:
    """RVC voice conversion over 3 static-shape ORT/TensorRT sessions.

    FAISS mixing, F0 decode, protect, and RMS logic run in NumPy between engine
    calls — a faithful port of RVC's Pipeline.vc for one fixed block geometry.

    All GPU libs (onnxruntime, torch) are imported lazily inside methods so
    this module stays importable locally without them for unit testing.
    """

    def __init__(
        self,
        onnx_dir: str,
        cache_dir: str,
        index,           # faiss.Index (worker's cached loader)
        big_npy,         # np.ndarray — full reconstruct_n result (cached)
        mel_extractor,   # RMVPE's MelSpectrogram module (already on GPU)
        device: str = "cuda",
    ):
        import onnxruntime as ort
        self.index = index
        self.big_npy = big_npy
        self.mel = mel_extractor
        self.device = device

        # TensorRT EP with engine cache on the volume.
        # Cache is L4(SM89) + TRT-version specific — rebuild via compile_trt.py
        # after any GPU tier or ORT/TRT upgrade.
        providers = [
            ("TensorrtExecutionProvider", {
                "trt_engine_cache_enable": True,
                "trt_engine_cache_path": cache_dir,
                "trt_timing_cache_enable": True,
                "trt_fp16_enable": True,
            }),
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        opts = ort.SessionOptions()
        self.s_hubert = ort.InferenceSession(
            f"{onnx_dir}/hubert.onnx", opts, providers=providers
        )
        self.s_gen = ort.InferenceSession(
            f"{onnx_dir}/generator.onnx", opts, providers=providers
        )
        self.s_rmvpe = ort.InferenceSession(
            f"{onnx_dir}/rmvpe.onnx", opts, providers=providers
        )
        # Seeded RNG for reproducible rnd noise (generator input)
        self._rng = np.random.default_rng(0)

    def warmup(self):
        """One full dummy pass — builds/loads TRT engine caches on first call."""
        self.convert_block(
            np.zeros(CANONICAL_IN, dtype=np.int16),
            pitch_shift=0,
            index_rate=0.75,
            rms_mix_rate=0.75,
            protect=0.33,
        )

    def _f0(
        self,
        audio_f32: np.ndarray,
        pitch_shift: int,
        filter_radius: int,
    ):
        """Compute F0 from padded float32 audio via RMVPE engine.

        Returns: (pitch int64[GEN_FRAMES], pitchf float32[GEN_FRAMES])
        """
        import torch
        # Mel frontend runs in PyTorch on GPU (torch.stft exports unreliably)
        with torch.no_grad():
            x = torch.from_numpy(audio_f32).float().to(self.device)[None, :]
            mel = self.mel(x, center=True)            # [1, 128, MEL_FRAMES]
        mel_np = mel.cpu().numpy().astype(np.float32)
        # Pad to MEL_FRAMES_PADDED (multiple of 32, as RMVPE.mel2hidden does)
        pad = MEL_FRAMES_PADDED - mel_np.shape[-1]
        if pad > 0:
            mel_np = np.pad(mel_np, ((0, 0), (0, 0), (0, pad)), mode="constant")
        # RMVPE E2E net (Engine 3)
        hidden = self.s_rmvpe.run(None, {"mel": mel_np})[0]   # [1, 352, 360]
        f0 = decode_f0(hidden)                                  # [MEL_FRAMES] Hz
        if filter_radius >= 2:
            from scipy.signal import medfilt
            voiced = f0 > 0
            ks = filter_radius if filter_radius % 2 else 3     # must be odd
            f0_f = medfilt(f0, kernel_size=ks)
            f0 = np.where(voiced & (f0_f > 0), f0_f, f0)
        # Apply pitch shift semitones
        f0 = f0 * (2.0 ** (pitch_shift / 12.0))
        # Resample MEL_FRAMES(341) f0 points onto the GEN_FRAMES(340) grid
        f0 = np.interp(
            np.linspace(0, 1, GEN_FRAMES),
            np.linspace(0, 1, len(f0)),
            f0,
        )
        pitchf = f0.astype(np.float32)
        pitch = f0_to_coarse(f0)
        return pitch, pitchf

    def convert_block(
        self,
        pcm_int16,
        pitch_shift: int,
        index_rate: float,
        rms_mix_rate: float,
        protect: float,
        filter_radius: int = 3,
    ) -> np.ndarray:
        """Convert one streaming block. Returns int16 @ 48 kHz, len == 3 * len(pcm_int16).

        This is the full Pipeline.vc equivalent for one fixed-shape block:
        pad -> HuBERT -> FAISS -> interp2x -> RMVPE F0 -> protect -> Generator -> trim -> rms
        """
        n_in = len(pcm_int16)
        audio, zpad = pad_to_canonical(pcm_int16)

        # ---- Engine 1: HuBERT feature extraction ----
        feats = self.s_hubert.run(None, {"audio": audio[None, :]})[0]   # [1, 170, 768]
        feats_raw_pre = feats.copy()

        # ---- FAISS index mixing (CPU NumPy — cannot be a TRT engine) ----
        if self.index is not None and self.big_npy is not None and index_rate > 0:
            npy = feats[0].astype(np.float32)                  # [170, 768]
            score, ix = self.index.search(npy, k=8)
            weight = np.square(1.0 / np.maximum(score, 1e-9))
            weight /= weight.sum(axis=1, keepdims=True)
            mixed = np.sum(
                self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1
            )
            feats = (index_rate * mixed + (1.0 - index_rate) * npy)[None].astype(np.float32)

        # ---- 2x temporal upsample to the generator frame grid ----
        # np.repeat(..., 2, axis=1) matches F.interpolate(scale_factor=2, mode="nearest")
        # which is what the vendored RVC pipeline uses.
        feats = np.repeat(feats, 2, axis=1)           # [1, 340, 768]
        feats_raw = np.repeat(feats_raw_pre, 2, axis=1)

        # ---- Engine 3: RMVPE F0 (mel frontend stays in PyTorch) ----
        pitch, pitchf = self._f0(audio, pitch_shift, filter_radius)

        # ---- Consonant protection (unvoiced frame guard) ----
        feats = apply_protect(feats, feats_raw, pitchf, protect)

        # ---- Engine 2: RVC Generator ----
        out = self.s_gen.run(None, {
            "phone":         feats.astype(np.float32),
            "phone_lengths": np.array([GEN_FRAMES], dtype=np.int64),
            "pitch":         pitch[None, :],                           # [1, 340]
            "pitchf":        pitchf[None, :],                          # [1, 340]
            "sid":           np.array([0], dtype=np.int64),
            "rnd":           self._rng.standard_normal((1, 192, GEN_FRAMES)).astype(np.float32),
        })[0].reshape(-1)                                               # [163200]

        # ---- Trim: remove reflect pad, then remove zero-fill's share ----
        out = out[T_PAD_TGT: T_PAD_TGT + OUT_48K]     # 67200 = 3 * CANONICAL_IN
        out = out[3 * zpad:]                            # exactly 3 * n_in remain

        # ---- RMS envelope mix (optional) ----
        if rms_mix_rate < 1.0:
            src = np.asarray(pcm_int16, dtype=np.float32) / 32768.0
            out = change_rms(src, out, rms_mix_rate)

        # ---- Clip and convert to int16 ----
        out = np.clip(out * 32767.0, -32768, 32767).astype(np.int16)
        assert len(out) == 3 * n_in, (
            f"Output length contract violated: {len(out)} != 3*{n_in}={3*n_in}"
        )
        return out
