import os
import sys
import time
import modal
from fastapi import FastAPI, Request, Response
from contextlib import asynccontextmanager
import asyncio


app = modal.App("rvc-worker")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the heavy synchronous startup (model loading) in a thread
    # so it does NOT block the async event loop during container startup
    await asyncio.to_thread(engine.startup)
    yield

web_app = FastAPI(lifespan=lifespan)


# Persistent Modal Volume
volume = modal.Volume.from_name(
    "rvc-models",
    create_if_missing=False,
)

# Image
image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install(
        "git",
        "ffmpeg",
    )
    .run_commands(
        "python -m pip install --upgrade 'pip<24.1'"
    )
    .pip_install_from_requirements(
        "modal_deploy/requirements.txt"
    )
    .add_local_dir(
        "Retrieval-based-Voice-Conversion-WebUI",
        remote_path="/root/rvc",
        ignore=[
            "venv311",
            "dataset",
            "logs",
            "TEMP",
            "__pycache__",
            ".git",
            ".github",
        ],
    )
)


# ---- Shared conversion logic (plain Python, no Modal decorators) ----

class RVCEngine:
    """Plain Python class that loads the RVC model and runs inference."""

    def __init__(self):
        self.vc = None
        self.config = None
        self.index_path = None
        self.ready = False

    def startup(self):
        import glob

        print("=" * 80)
        print("Starting RVC GPU Worker")
        print("=" * 80)

        os.chdir("/root/rvc")

        if "/root/rvc" not in sys.path:
            sys.path.insert(0, "/root/rvc")

        from configs.config import Config
        from infer.modules.vc.modules import VC

        os.environ["weight_root"] = "/root/rvc-models/weights"
        os.environ["index_root"] = "/root/rvc-models/logs/mi-test"
        os.environ["rmvpe_root"] = "/root/rvc/assets/rmvpe"

        print("Loading Config...")
        self.config = Config()

        print("Creating VC...")
        self.vc = VC(self.config)

        print("Loading model...")
        self.vc.get_vc("mi-test.pth")

        # Always prefer the added_* index (has full feature vectors)
        # trained_* index is incomplete — only used as fallback
        index_files = glob.glob(
            "/root/rvc-models/logs/mi-test/added_*.index"
        )
        if not index_files:
            # fallback to any index
            index_files = glob.glob(
                "/root/rvc-models/logs/mi-test/*.index"
            )

        if not index_files:
            raise RuntimeError("No FAISS index found.")

        self.index_path = sorted(index_files)[-1]  # pick latest if multiple
        print(f"Loaded Index: {self.index_path}")

        from infer.modules.vc.utils import load_hubert
        print("Loading HuBERT...")
        self.vc.hubert_model = load_hubert(self.config)
        print("HuBERT loaded.")

        # Pre-load parselmouth (PM pitch method) to avoid cold-import delay on first request
        print("Pre-loading parselmouth (PM pitch method)...")
        import parselmouth  # noqa — just importing triggers the native lib load
        print("parselmouth loaded.")

        # Warm-up inference: run one silent audio chunk through the full pipeline
        # so GPU kernels are JIT-compiled before the first real call arrives
        print("Running GPU warm-up inference pass...")
        try:
            import numpy as np, io, wave, struct
            silence_samples = np.zeros(3200, dtype=np.int16)  # 200ms at 16kHz
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(silence_samples.tobytes())
            self.run_conversion(buf.getvalue(), pitch=0)
            print("GPU warm-up complete.")
        except Exception as e:
            print(f"[Warning] Warm-up failed (non-fatal): {e}")

        self.ready = True
        print("GPU Worker Ready")


    def _auto_detect_pitch(self, wav_bytes: bytes) -> int:
        """
        Estimate speaker gender from audio and return pitch shift.
        Uses autocorrelation to estimate fundamental frequency (F0).
        Male voices: F0 < 145 Hz  → pitch_shift = +12 (shift up to female range)
        Female voices: F0 >= 145 Hz → pitch_shift = 0  (already in female range)

        Fallback is 0 (no shift) rather than +12: a missed female is far less
        damaging than a false-positive octave shift on a real female voice.
        """
        import io
        import wave
        import numpy as np

        try:
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())

            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            # Use first 1 second of audio (or all of it if shorter)
            chunk = audio[:sr]
            chunk = chunk - chunk.mean()  # remove DC offset

            # Autocorrelation pitch detection (80–300 Hz range)
            min_lag = int(sr / 300)   # 300 Hz upper limit
            max_lag = int(sr / 80)    # 80 Hz lower limit

            # Need at least ~125ms of audio for reliable pitch detection
            if max_lag >= len(chunk) or len(chunk) < sr // 8:
                print("[Gender Detection] Not enough audio — defaulting to no shift (0)")
                return 0

            corr = np.correlate(chunk, chunk, mode='full')
            corr = corr[len(corr) // 2:]
            corr = corr / (corr[0] + 1e-8)

            peak_idx = np.argmax(corr[min_lag:max_lag]) + min_lag
            f0 = sr / peak_idx

            # 145 Hz boundary: catches contralto/mezzo voices that sit in 145–165 Hz
            is_male = f0 < 145.0
            pitch_shift = 12 if is_male else 0

            print(f"[Gender Detection] F0={f0:.1f}Hz → {'Male' if is_male else 'Female'} → pitch_shift={pitch_shift}")
            return pitch_shift

        except Exception as e:
            print(f"[Gender Detection] Error: {e} — defaulting to no shift (0)")
            return 0

    def run_conversion(
        self,
        audio_bytes: bytes,
        pitch: int = -1,    # -1 = auto-detect gender, 0 = female input, 12 = male input
        index_rate: float = 0.75,
        filter_radius: int = 3,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
    ) -> bytes:
        import numpy as np

        t0 = time.perf_counter()

        # Auto-detect pitch shift from audio if not specified
        if pitch == -1:
            pitch = self._auto_detect_pitch(audio_bytes)

        # /tmp on Modal containers is a tmpfs (RAM-based) — no directory creation overhead
        input_path = "/tmp/rvc_input.wav"
        with open(input_path, "wb") as f:
            f.write(audio_bytes)

        t1 = time.perf_counter()
        print(f"[Timing] file-write: {(t1-t0)*1000:.1f}ms | pitch={pitch} index_rate={index_rate}")

        info, wav_opt = self.vc.vc_single(
            0,
            input_path,
            pitch,
            None,
            "pm",          # switched from rmvpe → pm saves ~300ms per chunk for real-time
            self.index_path,
            "",
            index_rate,
            filter_radius,
            0,
            rms_mix_rate,
            protect,
        )

        t2 = time.perf_counter()
        print(f"[Timing] vc_single: {(t2-t1)*1000:.1f}ms | {info}")

        sample_rate, audio = wav_opt

        if sample_rate is None or audio is None:
            raise RuntimeError(info)



        if audio.dtype != np.int16:
            if audio.dtype == np.float32 or audio.dtype == np.float64:
                audio = (audio * 32767).clip(-32768, 32767).astype(np.int16)
            else:
                audio = audio.astype(np.int16)

        result = audio.tobytes()
        print(f"[Timing] total run_conversion: {(time.perf_counter()-t0)*1000:.1f}ms → {len(result)} bytes")
        return result


# ---- ASGI Web App (HTTP endpoints for the backend) ----

# Global engine instance — initialized once per container
engine = RVCEngine()


@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={"/root/rvc-models": volume},
    scaledown_window=120,   # keep container warm for 2 min between requests, then auto-shutdown
    # Pin to Asia-Pacific so this GPU sits next to Render/Twilio once those also move to
    # Singapore, instead of Virginia (~13-19,000km / a full extra transpacific round trip
    # per RVC chunk). Narrow "ap-southeast" (Singapore) costs ~1.75x base GPU price vs
    # ~1.5x for the broader "ap" — using the narrow pin here since this container talks to
    # Render/Twilio in Singapore specifically, not elsewhere in APAC.
    region="ap-southeast",
)
@modal.asgi_app()
def fastapi_app():

    @web_app.get("/health")
    def health():
        import torch
        device_name = "None"
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
        return {
            "status": "ready" if engine.ready else "loading",
            "model": "mi-test.pth",
            "cuda_available": torch.cuda.is_available(),
            "cuda_device": device_name,
            "rvc_device": str(engine.config.device) if engine.config else "None",
        }

    @web_app.post("/convert")
    async def convert(
        request: Request,
        pitch_shift: int = -1,   # -1 = auto-detect gender
        index_rate: float = 0.75,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
    ):
        audio_bytes = await request.body()

        try:
            # run_conversion is heavy GPU-bound work — offload to a thread so the
            # FastAPI event loop stays responsive (health checks, other connections)
            import time
            t_start = time.perf_counter()
            pcm_bytes = await asyncio.to_thread(
                engine.run_conversion,
                audio_bytes,
                pitch_shift,
                index_rate,
                3,           # filter_radius default
                rms_mix_rate,
                protect,
            )
            elapsed = (time.perf_counter() - t_start) * 1000.0
            headers = {"X-Server-Time-Ms": f"{elapsed:.1f}"}
            return Response(
                content=pcm_bytes,
                media_type="application/octet-stream",
                headers=headers,
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            return Response(
                content=str(e),
                status_code=500,
            )

    return web_app


# ---- Local testing entrypoint (modal run) ----

@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={"/root/rvc-models": volume},
)
def convert_file(audio_bytes: bytes, pitch: int = 0) -> bytes:
    """
    pitch: semitones to shift. 0 = female input (no shift), 12 = male input,
           -1 = auto-detect from audio (runs _auto_detect_pitch).
    """
    test_engine = RVCEngine()
    test_engine.startup()
    return test_engine.run_conversion(audio_bytes, pitch=pitch)


@app.local_entrypoint()
def main(pitch: int = -1):
    import struct

    input_file = r"D:\Voice Clon\Keira\test1.wav"

    print(f"[Test] Input: {input_file} | pitch_shift={pitch} (Note: -1 means auto-detect)")

    with open(input_file, "rb") as f:
        audio_bytes = f.read()

    output_pcm = convert_file.remote(audio_bytes, pitch=pitch)

    sample_rate = 48000
    num_channels = 1
    bits_per_sample = 16
    block_align = num_channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    data_size = len(output_pcm)

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

    with open(r"D:\Voice Clon\Keira\converted50epoch.wav", "wb") as f:
        f.write(bytes(header) + output_pcm)

    print(f"Conversion Complete — saved {len(output_pcm)} bytes of audio")