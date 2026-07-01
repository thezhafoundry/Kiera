"""
RVC Local Inference Server
--------------------------
Run this directly on any GPU machine (AWS EC2, local GPU laptop, etc.)
without needing Modal at all.

Usage:
    pip install -r modal_deploy/requirements.txt
    uvicorn modal_deploy.local_server:app --host 0.0.0.0 --port 8001

Set in your .env:
    RVC_ENDPOINT_URL=http://<your-server-ip>:8001/convert
"""

import os
import sys
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response

# ── Paths — adjust if your layout is different ────────────────────────────────
RVC_ROOT      = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Retrieval-based-Voice-Conversion-WebUI"))
MODEL_WEIGHTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "weights"))
MODEL_LOGS    = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "logs", "mi-test"))
MODEL_NAME    = "mi-test.pth"
# ──────────────────────────────────────────────────────────────────────────────


class RVCEngine:
    def __init__(self):
        self.vc = None
        self.config = None
        self.index_path = None
        self.ready = False

    def startup(self):
        import glob
        print("=" * 60)
        print("Starting RVC Local Server")
        print("=" * 60)

        os.chdir(RVC_ROOT)
        if RVC_ROOT not in sys.path:
            sys.path.insert(0, RVC_ROOT)

        from configs.config import Config
        from infer.modules.vc.modules import VC

        os.environ["weight_root"] = MODEL_WEIGHTS
        os.environ["index_root"]  = MODEL_LOGS
        os.environ["rmvpe_root"]  = os.path.join(RVC_ROOT, "assets", "rmvpe")

        print("Loading Config...")
        self.config = Config()
        print("Creating VC...")
        self.vc = VC(self.config)
        print(f"Loading model: {MODEL_NAME}...")
        self.vc.get_vc(MODEL_NAME)

        index_files = glob.glob(os.path.join(MODEL_LOGS, "added_*.index"))
        if not index_files:
            index_files = glob.glob(os.path.join(MODEL_LOGS, "*.index"))
        self.index_path = index_files[0] if index_files else ""
        print(f"FAISS index: {self.index_path or 'Not found'}")

        self.ready = True
        print("RVC Local Server Ready!")

    def run_conversion(self, audio_bytes, pitch=0, index_rate=0.75,
                       filter_radius=3, rms_mix_rate=0.75, protect=0.33):
        import numpy as np
        t0 = time.perf_counter()

        if pitch == -1:
            pitch = self._auto_detect_pitch(audio_bytes)

        input_path = "/tmp/rvc_input.wav"
        with open(input_path, "wb") as f:
            f.write(audio_bytes)

        info, wav_opt = self.vc.vc_single(
            0, input_path, pitch, None,
            "pm",            # Fast CPU pitch — change to "rmvpe" for offline use
            self.index_path, "",
            index_rate, filter_radius, 0, rms_mix_rate, protect,
        )

        sample_rate, audio = wav_opt
        if sample_rate is None or audio is None:
            raise RuntimeError(info)

        if audio.dtype != np.int16:
            if audio.dtype in (np.float32, np.float64):
                audio = (audio * 32767).clip(-32768, 32767).astype(np.int16)
            else:
                audio = audio.astype(np.int16)

        result = audio.tobytes()
        print(f"[Timing] total: {(time.perf_counter()-t0)*1000:.1f}ms")
        return result

    def _auto_detect_pitch(self, wav_bytes):
        import io, wave
        import numpy as np
        try:
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                sr  = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            audio    = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            chunk    = audio[:sr] - audio[:sr].mean()
            min_lag  = int(sr / 300)
            max_lag  = int(sr / 80)
            if max_lag >= len(chunk) or len(chunk) < sr // 8:
                return 12
            corr     = np.correlate(chunk, chunk, mode="full")
            corr     = corr[len(corr)//2:]
            corr    /= corr[0] + 1e-8
            peak_idx = int(corr[min_lag:max_lag].argmax()) + min_lag
            f0       = sr / peak_idx
            pitch    = 12 if f0 < 145.0 else 0
            print(f"[Gender] F0={f0:.1f}Hz pitch_shift={pitch}")
            return pitch
        except Exception as e:
            print(f"[Gender] Error: {e}")
            return 0


engine = RVCEngine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(engine.startup)
    yield

app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    import torch
    device = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    return {
        "status": "ready" if engine.ready else "loading",
        "model": MODEL_NAME,
        "cuda_available": torch.cuda.is_available(),
        "device": device,
    }


@app.post("/convert")
async def convert(
    request: Request,
    pitch_shift: int = -1,
    index_rate: float = 0.75,
    rms_mix_rate: float = 0.75,
    protect: float = 0.33,
):
    if not engine.ready:
        return Response(content="Engine not ready", status_code=503)
    audio_bytes = await request.body()
    if not audio_bytes:
        return Response(content="No audio data", status_code=400)
    try:
        t_start = time.perf_counter()
        pcm_bytes = await asyncio.to_thread(
            engine.run_conversion, audio_bytes, pitch_shift,
            index_rate, 3, rms_mix_rate, protect,
        )
        elapsed = (time.perf_counter() - t_start) * 1000.0
        return Response(
            content=pcm_bytes,
            media_type="application/octet-stream",
            headers={"X-Server-Time-Ms": f"{elapsed:.1f}"},
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return Response(content=str(e), status_code=500)
