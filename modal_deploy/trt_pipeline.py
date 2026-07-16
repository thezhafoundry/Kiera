"""TRT-backed re-implementation of RVC's Pipeline.vc for Kiera's streaming worker.

Pure-NumPy helpers + TRTVoicePipeline (ORT/TensorRT sessions). This module must
stay importable WITHOUT Modal or GPU libs at module level, so the helper half is
unit-testable locally. It is mounted into the container via
.add_local_python_source("trt_pipeline") -- see worker.py's image (Modal does NOT
auto-bundle sibling modules; confirmed incident 2026-07-03 with streaming.py).
"""
import numpy as np
import time

try:
    from modal_deploy.rvc_profiles import (
        SAMPLE_RATE_IN,
        SAMPLE_RATE_OUT,
        TRT_T_PAD,
        get_active_profile,
    )
except ImportError:  # inside Modal container
    from rvc_profiles import (
        SAMPLE_RATE_IN,
        SAMPLE_RATE_OUT,
        TRT_T_PAD,
        get_active_profile,
    )

# ---- Canonical static shapes (shared with export_onnx.py) ----
PROFILE = get_active_profile()
PROFILE_NAME = PROFILE.name
SR_IN = SAMPLE_RATE_IN
SR_OUT = SAMPLE_RATE_OUT
CANONICAL_IN = PROFILE.canonical_in
PADDED_IN = PROFILE.padded_in
HUBERT_FRAMES = PROFILE.hubert_frames
GEN_FRAMES = PROFILE.generator_frames
OUT_PADDED_48K = GEN_FRAMES * 480
T_PAD_TGT = TRT_T_PAD * 3
OUT_48K = OUT_PADDED_48K - 2 * T_PAD_TGT
MEL_FRAMES = PROFILE.mel_frames
MEL_FRAMES_PADDED = PROFILE.mel_frames_padded

# RVC f0 mapping constants (verbatim from RVC/infer/modules/vc/pipeline.py)
F0_MIN, F0_MAX = 50.0, 1100.0
F0_MEL_MIN = 1127.0 * np.log(1.0 + F0_MIN / 700.0)
F0_MEL_MAX = 1127.0 * np.log(1.0 + F0_MAX / 700.0)

# RMVPE cents mapping (verbatim from RVC/infer/lib/rmvpe.py)
_CENTS_MAPPING = 20.0 * np.arange(360) + 1997.3794084376191


def assert_static_input_shape(
    session,
    session_name: str,
    input_name: str,
    expected_shape: tuple,
) -> None:
    inputs = {item.name: tuple(item.shape) for item in session.get_inputs()}
    actual_shape = inputs.get(input_name)
    if actual_shape != expected_shape:
        raise RuntimeError(
            f"{session_name}.{input_name} static shape mismatch for RVC "
            f"profile={PROFILE_NAME}: expected {expected_shape}, got "
            f"{actual_shape}. Re-export and recompile this profile's artifacts."
        )


def pad_to_canonical(pcm_int16: np.ndarray) -> tuple:
    """int16 block (<= CANONICAL_IN samples) -> (float32[PADDED_IN], left_zero_pad).

    Short first-of-session blocks are zero-filled at the HEAD of the canonical
    region (they lack left context); the fixed TRT_T_PAD reflect pad is then
    applied outside that. left_zero_pad lets the caller trim 3x that many
    samples off the 48 kHz output head.
    """
    pcm_int16 = np.asarray(pcm_int16, dtype=np.int16)
    if len(pcm_int16) > CANONICAL_IN:
        raise ValueError(f"block of {len(pcm_int16)} exceeds canonical {CANONICAL_IN}")
    zpad = CANONICAL_IN - len(pcm_int16)
    audio = pcm_int16.astype(np.float32) / 32768.0
    canonical = np.concatenate([np.zeros(zpad, dtype=np.float32), audio])
    padded = np.pad(canonical, (TRT_T_PAD, TRT_T_PAD), mode="reflect")
    return padded, zpad


def f0_to_coarse(f0: np.ndarray) -> np.ndarray:
    """Hz -> RVC's 1..255 mel-quantized pitch codes (port of get_f0's tail)."""
    f0_mel = 1127.0 * np.log(1.0 + np.asarray(f0, dtype=np.float64) / 700.0)
    voiced = f0_mel > 0
    f0_mel[voiced] = (f0_mel[voiced] - F0_MEL_MIN) * 254.0 / (F0_MEL_MAX - F0_MEL_MIN) + 1.0
    f0_mel[f0_mel <= 1.0] = 1.0
    f0_mel[f0_mel > 255.0] = 255.0
    return np.rint(f0_mel).astype(np.int64)


def decode_f0(hidden: np.ndarray, thred: float = 0.03) -> np.ndarray:
    """RMVPE hidden [1, MEL_FRAMES_PADDED, 360] -> f0 Hz [MEL_FRAMES].

    Port of RMVPE.decode/to_local_average_cents: local weighted average of
    cents in a +/-4 bin window around the argmax, zeroed where peak < thred.
    """
    salience = np.asarray(hidden, dtype=np.float32)[0, :MEL_FRAMES]      # [T, 360]
    padded_sal = np.pad(salience, ((0, 0), (4, 4)))
    centers = np.argmax(salience, axis=1)
    f0 = np.zeros(MEL_FRAMES, dtype=np.float64)
    for i, c in enumerate(centers):
        window = padded_sal[i, c: c + 9]                   # 9 bins centered on c
        cents_win = _CENTS_MAPPING[max(0, c - 4): c + 5]
        if len(cents_win) < len(window):                   # edge bins
            window = window[: len(cents_win)]
        denom = window.sum()
        if denom > 0:
            cents = float((window * cents_win).sum() / denom)
            f0[i] = 10.0 * 2.0 ** (cents / 1200.0)
    f0[salience.max(axis=1) <= thred] = 0.0
    return f0


def change_rms(source_16k: np.ndarray, out_48k: np.ndarray, rate: float) -> np.ndarray:
    """Port of pipeline.py change_rms: blend output loudness envelope toward the
    source's. rate=1 leaves the converted output's own envelope untouched."""
    if rate >= 1.0:
        return out_48k
    import librosa
    rms1 = librosa.feature.rms(y=source_16k, frame_length=SR_IN // 2 * 2,
                               hop_length=SR_IN // 2)[0]
    rms2 = librosa.feature.rms(y=out_48k, frame_length=SR_OUT // 2 * 2,
                               hop_length=SR_OUT // 2)[0]
    n = len(out_48k)
    rms1 = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(rms1)), rms1)
    rms2 = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(rms2)), rms2)
    rms2 = np.maximum(rms2, 1e-6)
    return out_48k * (rms1 / rms2) ** (1.0 - rate)


def apply_protect(feats: np.ndarray, feats_raw: np.ndarray,
                  pitchf: np.ndarray, protect: float) -> np.ndarray:
    """Port of Pipeline.vc's consonant protection: on unvoiced frames
    (pitchf==0), blend index-mixed feats back toward the raw HuBERT feats.
    protect >= 0.5 disables (RVC convention)."""
    if protect >= 0.5:
        return feats
    mask = (np.asarray(pitchf) == 0.0)[None, :, None]      # [1, T, 1]
    blended = feats * protect + feats_raw * (1.0 - protect)
    return np.where(mask, blended, feats).astype(np.float32)


class TRTVoicePipeline:
    """RVC voice conversion over 3 static-shape ORT/TensorRT sessions.

    FAISS mixing, F0 decode, protect and RMS logic run in NumPy between engine
    calls -- a faithful port of RVC's Pipeline.vc for one fixed block geometry.
    """

    def __init__(self, onnx_dir: str, cache_dir: str, index, big_npy,
                 mel_extractor, device: str = "cuda"):
        import onnxruntime as ort
        self.index = index                # faiss index (worker's cached loader)
        self.big_npy = big_npy            # full reconstruct_n array (cached)
        self.mel = mel_extractor          # RMVPE's torch MelSpectrogram module (GPU)
        self.device = device
        providers_fp16 = [
            ("TensorrtExecutionProvider", {
                "trt_engine_cache_enable": True,
                "trt_engine_cache_path": cache_dir,
                "trt_timing_cache_enable": True,
                "trt_fp16_enable": True,
            }),
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        providers_fp32 = [
            ("TensorrtExecutionProvider", {
                "trt_engine_cache_enable": True,
                "trt_engine_cache_path": cache_dir,
                "trt_timing_cache_enable": True,
                "trt_fp16_enable": False,  # Disabled to bypass TensorRT Myelin FP16 compiler bug on generator
            }),
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        opts = ort.SessionOptions()
        self.s_hubert = ort.InferenceSession(f"{onnx_dir}/hubert.onnx", opts, providers=providers_fp16)
        self.s_gen = ort.InferenceSession(f"{onnx_dir}/generator.onnx", opts, providers=providers_fp32)
        self.s_rmvpe = ort.InferenceSession(f"{onnx_dir}/rmvpe.onnx", opts, providers=providers_fp16)
        assert_static_input_shape(
            self.s_hubert, "hubert", "audio", (1, PADDED_IN)
        )
        assert_static_input_shape(
            self.s_gen, "generator", "phone", (1, GEN_FRAMES, 768)
        )
        assert_static_input_shape(
            self.s_rmvpe,
            "rmvpe",
            "mel",
            (1, 128, MEL_FRAMES_PADDED),
        )
        # Verify all three sessions actually loaded TRT — ORT silently falls back to CPU
        # if TRT init fails; this surfaces that as a hard error rather than ~10x latency.
        for sess_name, sess in [("hubert", self.s_hubert), ("generator", self.s_gen), ("rmvpe", self.s_rmvpe)]:
            active = sess.get_providers()
            if "TensorrtExecutionProvider" not in active:
                raise RuntimeError(
                    f"TensorrtExecutionProvider not active for {sess_name} (got {active}). "
                    "Check LD_LIBRARY_PATH, tensorrt-cu12 install, and engine cache validity."
                )
            print(f"[TRT] {sess_name} session providers: {active}")
        self._rng = np.random.default_rng(0)   # rnd noise; seeded = reproducible tests
        self.last_block_timing: dict = {}

    def warmup(self):
        """One full dummy pass -- builds/loads TRT engine caches."""
        if self.index is None:
            print("[TRT] FAISS index not loaded -- skipping index mixing (feats = raw HuBERT only)")
        self.convert_block(np.zeros(CANONICAL_IN, dtype=np.int16),
                           pitch_shift=0, index_rate=0.75,
                           rms_mix_rate=0.75, protect=0.33)

    def _f0(self, audio_f32: np.ndarray, pitch_shift: float, filter_radius: int):
        """audio [PADDED_IN] float32 -> (pitch int64 [GEN_FRAMES], pitchf f32 [GEN_FRAMES],
        f0_raw f32 [GEN_FRAMES] -- the PRE-shift F0 track in Hz, unvoiced == 0)."""
        import torch
        with torch.no_grad():
            x = torch.from_numpy(audio_f32).float().to(self.device)[None, :]
            mel = self.mel(x, center=True)                 # [1, 128, MEL_FRAMES]
        mel_np = mel.cpu().numpy().astype(np.float32)
        pad = MEL_FRAMES_PADDED - mel_np.shape[-1]
        if pad > 0:
            mel_np = np.pad(mel_np, ((0, 0), (0, 0), (0, pad)), mode="constant")
        hidden = self.s_rmvpe.run(None, {"mel": mel_np})[0]
        f0 = decode_f0(hidden)                             # [MEL_FRAMES] Hz
        if filter_radius >= 2:
            from scipy.signal import medfilt
            voiced = f0 > 0
            ks = filter_radius if filter_radius % 2 == 1 else 3
            f0_f = medfilt(f0, kernel_size=ks)
            f0 = np.where(voiced & (f0_f > 0), f0_f, f0)
        # resample MEL_FRAMES(273) f0 points onto the GEN_FRAMES(270) grid BEFORE
        # applying the shift: linear interp commutes with the scalar 2**(shift/12)
        # factor, and capturing f0_raw here hands PitchLock the pre-shift track.
        f0 = np.interp(np.linspace(0, 1, GEN_FRAMES), np.linspace(0, 1, len(f0)), f0)
        f0_raw = f0.astype(np.float32)
        f0 = f0 * (2.0 ** (pitch_shift / 12.0))
        pitchf = f0.astype(np.float32)
        pitch = f0_to_coarse(f0)
        return pitch, pitchf, f0_raw

    def convert_block(self, pcm_int16, pitch_shift: float = 0, index_rate: float = 0.75,
                      rms_mix_rate: float = 0.75, protect: float = 0.33,
                      filter_radius: int = 3, f0_sink: list = None) -> np.ndarray:
        """Convert one streaming block. Returns int16 @ 48 kHz, length exactly 3*len(pcm_int16).

        Records a per-stage timing breakdown (ms) in self.last_block_timing after
        every call: hubert_ms, index_ms, rmvpe_ms, generator_ms, postproc_ms, total_ms.
        """
        n_in = len(pcm_int16)
        audio, zpad = pad_to_canonical(pcm_int16)

        t_start = time.perf_counter()

        # ---- Engine 1: HuBERT / ContentVec ----
        feats = self.s_hubert.run(None, {"audio": audio[None, :]})[0]   # [1, 170, 768]
        feats_raw_pre = feats.copy()
        t_hubert = time.perf_counter()

        # ---- FAISS index mixing (CPU, cannot be TRT) ----
        if self.index is not None and self.big_npy is not None and index_rate > 0:
            npy = feats[0].astype(np.float32)
            score, ix = self.index.search(npy, k=8)
            weight = np.square(1.0 / np.maximum(score, 1e-9))
            weight /= weight.sum(axis=1, keepdims=True)
            mixed = np.sum(self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1)
            feats = (index_rate * mixed + (1 - index_rate) * npy)[None].astype(np.float32)
        else:
            if index_rate > 0:
                print("[TRT] FAISS index not loaded -- index mixing skipped (raw HuBERT feats only)")

        feats = np.repeat(feats, 2, axis=1)          # [1, 340, 768]
        feats_raw = np.repeat(feats_raw_pre, 2, axis=1)
        t_index = time.perf_counter()

        # ---- Engine 3: RMVPE F0 ----
        pitch, pitchf, f0_raw = self._f0(audio, pitch_shift, filter_radius)
        if f0_sink is not None:
            f0_sink.append(f0_raw)
        t_rmvpe = time.perf_counter()

        # ---- protect (consonant guard) ----
        feats = apply_protect(feats, feats_raw, pitchf, protect)

        # ---- Engine 2: generator ----
        # sine_noise: audio-rate N(0,1) noise for SineGen unvoiced segments.
        # Generated here (numpy RNG) rather than inside the ONNX graph so TRT
        # Myelin never sees a RandomNormal op. Shape [1, OUT_PADDED_48K, 1].
        sine_noise = self._rng.standard_normal((1, OUT_PADDED_48K, 1)).astype(np.float32)
        out = self.s_gen.run(None, {
            "phone": feats.astype(np.float32),
            "phone_lengths": np.array([GEN_FRAMES], dtype=np.int64),
            "pitch": pitch[None, :],
            "pitchf": pitchf[None, :],
            "sid": np.array([0], dtype=np.int64),
            "rnd": self._rng.standard_normal((1, 192, GEN_FRAMES)).astype(np.float32),
            "sine_noise": sine_noise,
        })[0].reshape(-1)                                   # [OUT_PADDED_48K] float32
        t_generator = time.perf_counter()

        # ---- trim fixed pad, then the zero-fill's share, then rms mix ----
        out = out[T_PAD_TGT: T_PAD_TGT + OUT_48K]
        out = out[3 * zpad:]
        # Pad to exactly 3 * n_in to satisfy the streaming contract (offsetting Hubert conv boundary loss)
        target_len = 3 * n_in
        pad_needed = target_len - len(out)
        if pad_needed > 0:
            out = np.pad(out, (0, pad_needed), mode="edge")
        if rms_mix_rate < 1.0:
            src = np.asarray(pcm_int16, dtype=np.float32) / 32768.0
            out = change_rms(src, out, rms_mix_rate)
        out = np.clip(out * 32767.0, -32768, 32767).astype(np.int16)
        assert len(out) == 3 * n_in, f"contract violation: {len(out)} != 3*{n_in}"

        t_end = time.perf_counter()
        self.last_block_timing = {
            "hubert_ms": round((t_hubert - t_start) * 1000.0, 2),
            "index_ms": round((t_index - t_hubert) * 1000.0, 2),
            "rmvpe_ms": round((t_rmvpe - t_index) * 1000.0, 2),
            "generator_ms": round((t_generator - t_rmvpe) * 1000.0, 2),
            "postproc_ms": round((t_end - t_generator) * 1000.0, 2),
            "total_ms": round((t_end - t_start) * 1000.0, 2),
        }
        return out
