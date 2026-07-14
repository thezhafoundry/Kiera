import os
import sys
import time
import modal
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager
import asyncio

# Pure numpy DSP for the /ws streaming path. Imported at module top (not deferred
# inside the handler) so Modal's dependency analysis mounts streaming.py into the
# container. The fallback covers running with modal_deploy/ directly on sys.path.
try:
    from modal_deploy import streaming as st
except ImportError:  # pragma: no cover
    import streaming as st

import faiss
from functools import lru_cache

# RVC/infer/modules/vc/pipeline.py calls faiss.read_index(file_index) then
# index.reconstruct_n(0, index.ntotal) on EVERY vc_single() call — fine for
# the original one-shot-per-file WebUI use case, but this app calls it once
# per ~480ms streaming audio block, re-reading and re-materializing a 221MB
# index from disk every time (confirmed ~1.4-2.0s of the ~3s per-block
# latency in production [Timing] logs). Cache the loaded index and its full
# reconstruction so only the first call (the GPU warm-up pass in
# RVCEngine.startup(), before any real caller connects) pays that cost.
_real_faiss_read_index = faiss.read_index


@lru_cache(maxsize=4)
def _cached_read_index(path: str) -> "faiss.Index":
    index = _real_faiss_read_index(path)
    cached_npy = index.reconstruct_n(0, index.ntotal)
    index.reconstruct_n = lambda *args, **kwargs: cached_npy
    return index


def _install_faiss_index_cache():
    if getattr(faiss.read_index, "_kiera_cached", False):
        return  # already installed (e.g. re-imported in a test)
    faiss.read_index = _cached_read_index


_cached_read_index._kiera_cached = True
_install_faiss_index_cache()


app = modal.App("rvc-worker")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the heavy synchronous startup (model loading) in a thread
    # so it does NOT block the async event loop during container startup
    await asyncio.to_thread(engine.startup)
    yield

web_app = FastAPI(lifespan=lifespan)


# Persistent Modal Volume and images — defined in modal_defs.py (single source of
# truth) which is mounted into containers so compile_trt / export_onnx can also
# import these without needing worker.py on the container path.
try:
    from modal_deploy.modal_defs import volume, image, trt_image
except ImportError:   # inside container: modal_deploy package not present
    from modal_defs import volume, image, trt_image


# ---- Shared conversion logic (plain Python, no Modal decorators) ----

class RVCEngine:
    """ONNX-based RVC model execution engine."""

    def __init__(self):
        self.vec_session = None
        self.rvc_session = None
        self.index = None
        self.big_npy = None
        self.index_path = None
        self.ready = False
        # TRT pipeline (set if USE_TRT=1 and init succeeds)
        self.trt_pipe = None
        self.engine_kind = "onnx-cuda"   # "onnx-cuda" (fallback) or "trt"
        self._trt_cache_hot = False

    def startup(self):
        import glob
        import faiss
        import onnxruntime as ort

        print("=" * 80)
        print("Starting RVC ONNX GPU Worker")
        print("=" * 80)

        # Set path context
        os.chdir("/root/rvc")
        if "/root/rvc" not in sys.path:
            sys.path.insert(0, "/root/rvc")

        # A3: Fail-fast on missing FAISS index — a silent skip produces the
        # "doesn't sound like the trained voice" symptom (documented in subsystem-notes).
        # Always prefer the added_* index.
        index_files = glob.glob(
            "/root/rvc-models/logs/mi-test/added_*.index"
        )
        if not index_files:
            index_files = glob.glob(
                "/root/rvc-models/logs/mi-test/*.index"
            )

        if not index_files:
            raise RuntimeError("No FAISS index found in /root/rvc-models/logs/mi-test/ — "
                               "refusing to start without target-timbre index.")
        self.index_path = sorted(index_files)[-1]
        print(f"Loading FAISS Index: {self.index_path}")
        self.index = faiss.read_index(self.index_path)
        # Reconstruct the full index to big_npy (numpy array)
        self.big_npy = self.index.reconstruct_n(0, self.index.ntotal)
        print("Index loaded and reconstructed successfully.")

        # A1: Load the base (non-TRT) ONNX sessions only if the artifacts exist.
        # They are the FALLBACK path; their absence must not crash a container whose
        # TRT engines are fine. If neither path can load, startup fails loudly below.
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        vec_path = "/root/rvc-models/onnx/vec-768-layer-12.onnx"
        rvc_path = "/root/rvc-models/onnx/mi-test.onnx"

        if os.path.exists(vec_path) and os.path.exists(rvc_path):
            print("Loading ContentVec ONNX...")
            self.vec_session = ort.InferenceSession(vec_path, providers=providers)
            print("Loading RVC Generator ONNX...")
            self.rvc_session = ort.InferenceSession(rvc_path, providers=providers)
            print("ONNX Inference sessions initialized.")
            # A2: Assert GPU execution is active — CPU cannot keep up with real-time.
            for name, sess in [("vec", self.vec_session), ("rvc", self.rvc_session)]:
                active = sess.get_providers()
                if "CUDAExecutionProvider" not in active:
                    raise RuntimeError(
                        f"CUDAExecutionProvider not active for {name} session (got {active}) — "
                        "CPU inference cannot keep up with real time; failing startup instead "
                        "of serving broken audio."
                    )
        else:
            print(f"[Warning] Base ONNX artifacts missing ({vec_path}, {rvc_path}) — "
                  "fallback path unavailable; TRT path is required for this container.")

        # Pre-load parselmouth
        print("Pre-loading parselmouth (PM pitch method)...")
        import parselmouth  # noqa
        print("parselmouth loaded.")

        # Warm-up inference
        print("Running GPU warm-up inference pass...")
        try:
            import numpy as np, io, wave
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

        # ---- Optional TRT path (USE_TRT=1). Fail-closed: if TRT init fails we fall
        # back to the ONNX-CUDA path (never raw audio), log loudly, and expose the
        # degradation in /health.
        if os.environ.get("USE_TRT", "0") == "1":
            try:
                import glob as _glob
                try:
                    from modal_deploy import trt_pipeline as tp
                except ImportError:
                    import trt_pipeline as tp
                from infer.lib.rmvpe import MelSpectrogram
                import torch as _torch

                # Reuse the already-loaded index from startup above
                index = faiss.read_index(self.index_path)
                big_npy = index.reconstruct_n(0, index.ntotal)
                mel = MelSpectrogram(
                    is_half=False, n_mel_channels=128, sampling_rate=16000,
                    win_length=1024, hop_length=160, mel_fmin=30, mel_fmax=8000,
                ).to("cuda" if _torch.cuda.is_available() else "cpu")
                cache_dir = "/root/rvc-models/trt_cache"
                self._trt_cache_hot = bool(_glob.glob(f"{cache_dir}/*.engine"))
                print(f"[TRT] engine cache {'HOT' if self._trt_cache_hot else 'COLD -- building now (slow first boot)'}")
                t0 = time.perf_counter()
                self.trt_pipe = tp.TRTVoicePipeline(
                    "/root/rvc-models/onnx", cache_dir, index, big_npy, mel,
                )
                self.trt_pipe.warmup()
                self.engine_kind = "trt"
                print(f"[TRT] pipeline ready in {time.perf_counter()-t0:.1f}s")
            except Exception as e:
                import traceback
                traceback.print_exc()
                # A4: truthful label — the PyTorch path no longer exists; fallback is ONNX-CUDA
                print(f"[TRT] init FAILED ({e}) -- falling back to onnx-cuda path")
                self.trt_pipe = None
                self.engine_kind = "onnx-cuda"

        # A1: Fail-closed — at least one engine must be ready before reporting healthy.
        # This guard runs after the TRT branch so `ready` truly means "an engine works".
        if self.trt_pipe is None and self.vec_session is None:
            raise RuntimeError(
                "No conversion engine available: TRT init failed/disabled AND base ONNX "
                "artifacts are missing. Refusing to start (fail-closed, never raw)."
            )

        self.ready = True
        print("GPU Worker Ready")

    def _auto_detect_pitch(self, wav_bytes: bytes) -> int:
        import io
        import wave
        import numpy as np

        try:
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())

            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            chunk = audio[:sr]
            chunk = chunk - chunk.mean()

            min_lag = int(sr / 300)
            max_lag = int(sr / 80)

            if max_lag >= len(chunk) or len(chunk) < sr // 8:
                return 0

            corr = np.correlate(chunk, chunk, mode='full')
            corr = corr[len(corr) // 2:]
            corr = corr / (corr[0] + 1e-8)

            peak_idx = np.argmax(corr[min_lag:max_lag]) + min_lag
            f0 = sr / peak_idx

            is_male = f0 < 145.0
            pitch_shift = 12 if is_male else 0
            return pitch_shift
        except Exception as e:
            return 0

    def run_conversion(
        self,
        audio_bytes: bytes,
        pitch: float = -1,
        index_rate: float = 0.75,
        filter_radius: int = 3,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
        f0_sink: list = None,
    ) -> bytes:
        import numpy as np
        import librosa
        import wave
        import io

        t0 = time.perf_counter()

        # 1. Parse WAV bytes to float32 numpy array
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            sr = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        
        # Resample to 16kHz if needed (ContentVec and F0 Predictor expect 16kHz)
        if sr != 16000:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)

        # Auto-detect pitch shift if not specified
        if pitch == -1:
            pitch = self._auto_detect_pitch(audio_bytes)

        t1 = time.perf_counter()
        
        # 2. Extract ContentVec features
        feats_input = np.expand_dims(np.expand_dims(audio, 0), 0)
        vec_input_name = self.vec_session.get_inputs()[0].name
        vec_outputs = self.vec_session.run(None, {vec_input_name: feats_input})

        # Ensure feats layout is (1, seq_len, 768)
        feats = vec_outputs[0]
        if feats.shape[1] == 768:
            feats = feats.transpose(0, 2, 1)
        
        t2 = time.perf_counter()
        
        # 3. Perform index search and feature blending (numpy)
        feats0 = feats.copy()
        if self.index is not None and self.big_npy is not None and index_rate > 0:
            npy = feats[0].astype("float32")
            score, ix = self.index.search(npy, k=8)
            weight = np.square(1.0 / (score + 1e-8))
            weight /= weight.sum(axis=1, keepdims=True)
            blended_npy = np.sum(self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1)
            feats = blended_npy * index_rate + (1 - index_rate) * feats[0]
            feats = np.expand_dims(feats, 0)
            
        # 4. Interpolate features (upsample by 2)
        feats = np.repeat(feats, 2, axis=1)
        feats0 = np.repeat(feats0, 2, axis=1)
        
        # 5. Compute F0 and coarse pitch
        p_len = feats.shape[1]
        
        from infer.lib.infer_pack.modules.F0Predictor.PMF0Predictor import PMF0Predictor
        f0_predictor = PMF0Predictor(hop_length=160, sampling_rate=16000)
        
        pitchf = f0_predictor.compute_f0(audio, p_len)
        if len(pitchf) < p_len:
            pitchf = np.pad(pitchf, (0, p_len - len(pitchf)), mode="edge")
        elif len(pitchf) > p_len:
            pitchf = pitchf[:p_len]

        if f0_sink is not None:
            f0_sink.append(pitchf.astype(np.float32, copy=True))
        pitchf = pitchf * (2 ** (pitch / 12))
        
        f0_min = 50
        f0_max = 1100
        f0_mel_min = 1127 * np.log(1 + f0_min / 700)
        f0_mel_max = 1127 * np.log(1 + f0_max / 700)
        
        f0_mel = 1127 * np.log(1 + pitchf / 700)
        f0_mel[f0_mel > 0] = (f0_mel[f0_mel > 0] - f0_mel_min) * 254 / (
            f0_mel_max - f0_mel_min
        ) + 1
        f0_mel[f0_mel <= 1] = 1
        f0_mel[f0_mel > 255] = 255
        pitch_coarse = np.rint(f0_mel).astype(np.int64)
        
        # 6. Consonant protection
        if protect < 0.5:
            pitchff = pitchf.copy()
            pitchff[pitchf > 0] = 1.0
            pitchff[pitchf < 1] = protect
            pitchff = np.expand_dims(pitchff, (0, -1))
            feats = feats * pitchff + feats0 * (1.0 - pitchff)
            
        # 7. Prepare inputs for RVC generator ONNX model
        pitchf_input = np.expand_dims(pitchf.astype(np.float32), 0)
        pitch_coarse_input = np.expand_dims(pitch_coarse, 0)
        phone_lengths_input = np.array([p_len]).astype(np.int64)
        ds_input = np.array([0]).astype(np.int64)
        rnd_input = np.random.randn(1, 192, p_len).astype(np.float32)
        
        onnx_inputs = {
            self.rvc_session.get_inputs()[0].name: feats.astype(np.float32),
            self.rvc_session.get_inputs()[1].name: phone_lengths_input,
            self.rvc_session.get_inputs()[2].name: pitch_coarse_input,
            self.rvc_session.get_inputs()[3].name: pitchf_input,
            self.rvc_session.get_inputs()[4].name: ds_input,
            self.rvc_session.get_inputs()[5].name: rnd_input,
        }
        
        t3 = time.perf_counter()
        audio_opt = self.rvc_session.run(None, onnx_inputs)[0]
        t4 = time.perf_counter()
        
        print(f"[Timing] RVC ONNX session.run: {(t4-t3)*1000:.1f}ms")
        
        audio_opt = audio_opt.squeeze()
        if audio_opt.dtype != np.int16:
            audio_opt = (audio_opt * 32767).clip(-32768, 32767).astype(np.int16)
            
        result = audio_opt.tobytes()
        print(f"[Timing] total run_conversion: {(time.perf_counter()-t0)*1000:.1f}ms | feats={p_len} → {len(result)} bytes")
        return result

    def convert_block(
        self,
        pcm_int16,                      # np.int16 array, 16 kHz, <= CANONICAL_IN samples
        pitch: float = 0,
        index_rate: float = 0.75,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
        f0_sink: list = None,
    ) -> bytes:
        """Single streaming-block conversion. TRT path when loaded, else the
        existing PyTorch run_conversion via WAV bytes. Returns 48 kHz int16 bytes,
        ~3x the input duration either way. When `f0_sink` is a list, the block's
        pre-shift F0 track (Hz, unvoiced == 0) is appended to it."""
        if self.trt_pipe is not None:
            out = self.trt_pipe.convert_block(
                pcm_int16, pitch, index_rate, rms_mix_rate, protect,
                f0_sink=f0_sink,
            )
            return out.tobytes()
        # PyTorch fallback: wrap in WAV and call run_conversion
        try:
            from modal_deploy import streaming as _st
        except ImportError:
            import streaming as _st
        return self.run_conversion(
            _st.pcm16_to_wav_bytes(pcm_int16), pitch, index_rate, 3,
            rms_mix_rate, protect, f0_sink=f0_sink,
        )


# ---- ASGI Web App (HTTP endpoints for the backend) ----

# Global engine instance — initialized once per container
engine = RVCEngine()


# ---- WebSocket streaming session state ----
# MVP = a single concurrent /ws session. A second connection is told
# {"type":"busy"} and closed. Both locks are module-level so they persist for the
# life of the container (one process per Modal container).
_session_active = False
_session_lock = asyncio.Lock()   # guards the _session_active check-and-set
_gpu_lock = asyncio.Lock()       # serializes inference — the GPU is single-tenant

# ---- Per-call audio debug capture ----
# Saves each /ws session's input (16 kHz post-denoise PCM, as received) and output
# (48 kHz post-SOLA PCM, exactly what was sent back — the backend applies PresenceEQ
# *after* this point, so expect a ~4dB 1.2-3.4kHz difference vs what gets published)
# to the volume's debug/ dir for offline clarity analysis against the Twilio call
# recording. Bounded per side; disable with DEBUG_SAVE_AUDIO=0 and redeploy.
_DEBUG_SAVE_AUDIO = os.environ.get("DEBUG_SAVE_AUDIO", "1") == "1"
_DEBUG_MAX_SECONDS = 120


def _save_debug_wavs(in_pcm16k: bytes, out_pcm48k: bytes) -> None:
    import wave
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    os.makedirs("/root/rvc-models/debug", exist_ok=True)
    for tag, rate, data in (("in16k", 16000, in_pcm16k), ("out48k", 48000, out_pcm48k)):
        if not data:
            continue
        path = f"/root/rvc-models/debug/call_{ts}_{tag}.wav"
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(data)
        print(f"[Debug] saved {path} ({len(data)} bytes)")
    volume.commit()


@app.function(
    image=trt_image,   # includes ORT + TRT stack; required for USE_TRT to work at runtime
    gpu="L4",
    timeout=10800,
    volumes={"/root/rvc-models": volume},
    scaledown_window=120,   # keep container warm for 2 min between requests, then auto-shutdown
    # Pin to Asia-Pacific so this GPU sits next to Render/Twilio in Singapore (Render moves
    # there in Task 6), instead of Virginia (~13-19,000km / a full extra transpacific round
    # trip per audio block on the persistent /ws stream). Narrow "ap-southeast" (Singapore)
    # costs ~1.75x base GPU price vs ~1.5x for the broader "ap" — using the narrow pin here
    # since this container talks to Render/Twilio in Singapore specifically, not elsewhere in APAC.
    region="ap-southeast",
    # _session_active (below) only enforces single-tenancy inside one already-running
    # container — it does nothing to stop Modal's autoscaler from booting a second GPU
    # container for a connection attempt that arrives while the first is still cold-starting
    # (~75s) or mid-call. Cap at 1 so extra connection attempts queue against the single
    # container instead of each paying for its own T4 GPU concurrently.
    max_containers=1,
    env={"USE_TRT": "1"},
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
            "rvc_device": "cuda" if torch.cuda.is_available() else "cpu",
            "engine": engine.engine_kind,
            "trt_cache": (
                "hot" if getattr(engine, "_trt_cache_hot", False) else "cold"
            ) if engine.engine_kind == "trt" else "n/a",
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

    @web_app.websocket("/ws")
    async def ws_stream(ws: WebSocket):
        """Persistent streaming voice-conversion socket (see Task 1 protocol).

        NEVER emits raw/unconverted voice: on inference failure it sends an
        {"type":"error"} and keeps the session alive; the silence bypass emits
        zeros. Single-tenant — a second connection is closed with {"type":"busy"}.
        """
        import json
        import traceback
        import numpy as np

        global _session_active

        await ws.accept()

        # ---- Single-tenancy gate (MVP = 1 session) ----
        async with _session_lock:
            if _session_active:
                await ws.send_json({"type": "busy"})
                await ws.close()
                return
            _session_active = True

        # Bounded per-session capture, written out in the finally below.
        dbg_in = bytearray() if _DEBUG_SAVE_AUDIO else None
        dbg_out = bytearray() if _DEBUG_SAVE_AUDIO else None

        try:
            # ---- Handshake: first message is the config JSON ----
            first = await ws.receive()
            if first.get("type") == "websocket.disconnect":
                return
            cfg = json.loads(first.get("text") or "{}")
            cfg_pitch = int(cfg.get("pitch_shift", -1))
            index_rate = float(cfg.get("index_rate", 0.75))
            rms_mix_rate = float(cfg.get("rms_mix_rate", 0.75))
            protect = float(cfg.get("protect", 0.33))

            # Warm-gate: only reply "ready" when the engine is actually loaded AND
            # (guaranteed above) no other session is active.
            if not engine.ready:
                await ws.send_json({"type": "error", "message": "engine not ready"})
                await ws.close()
                return
            await ws.send_json({"type": "ready"})

            # ---- Per-session streaming state ----
            acc = st.BlockAccumulator()
            pending_tail = np.zeros(0, dtype=np.int16)
            # pitch_shift == -1 -> auto-detect once from the first non-silent block,
            # then fixed for the whole session.
            session_pitch = None if cfg_pitch == -1 else cfg_pitch

            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                text = message.get("text")
                if text is not None:
                    data = json.loads(text)
                    if data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                    continue

                payload = message.get("bytes")
                if not payload:
                    continue
                if len(payload) % 2:
                    payload = payload[:len(payload) - 1]  # keep int16 alignment
                if dbg_in is not None and len(dbg_in) < 16000 * 2 * _DEBUG_MAX_SECONDS:
                    dbg_in.extend(payload)
                acc.push(np.frombuffer(payload, dtype=np.int16))

                # Drain every complete 1000 ms block currently buffered.
                while True:
                    popped = acc.pop_block()
                    if popped is None:
                        break
                    infer_input, context_len, block = popped

                    infer_ms = 0.0
                    lock_wait_ms = 0.0
                    block_timing: dict = {}
                    if st.block_rms(block) < st.SILENCE_RMS_THRESHOLD:
                        # Silence bypass: skip the GPU, emit equal-duration 48 kHz
                        # silence (3x samples), never raw audio. The extra crossfade of
                        # zeros mirrors the +crossfade overlap that voiced blocks carry,
                        # so after SOLA holds back its tail the net emitted duration is
                        # still a full block (otherwise each silence block would lose one
                        # crossfade, shortening pauses).
                        out = np.zeros(len(block) * 3 + st.SOLA_CROSSFADE_SAMPLES, dtype=np.int16)
                    else:
                        # Resolve the session pitch once from the first non-silent audio.
                        if session_pitch is None:
                            probe_wav = st.pcm16_to_wav_bytes(infer_input)
                            session_pitch = await asyncio.to_thread(
                                engine._auto_detect_pitch, probe_wav
                            )
                        try:
                            t_wait_start = time.perf_counter()
                            async with _gpu_lock:  # single-tenant GPU
                                t_compute_start = time.perf_counter()
                                out_bytes = await asyncio.to_thread(
                                    engine.convert_block,
                                    infer_input,
                                    session_pitch,
                                    index_rate,
                                    rms_mix_rate,
                                    protect,
                                )
                            t_compute_end = time.perf_counter()
                            infer_ms = (t_compute_end - t_wait_start) * 1000.0
                            lock_wait_ms = (t_compute_start - t_wait_start) * 1000.0
                            # Per-stage breakdown lives on the TRT pipeline object
                            # (engine.trt_pipe), not on RVCEngine itself — engine's
                            # convert_block only delegates there. trt_pipe is None on
                            # the ONNX-CUDA fallback path: no breakdown, empty dict.
                            block_timing = dict(
                                getattr(getattr(engine, "trt_pipe", None), "last_block_timing", None) or {}
                            )
                        except Exception as e:
                            # Inference failed: report, emit NOTHING (never raw),
                            # keep the session alive, hold the pending tail as-is.
                            traceback.print_exc()
                            await ws.send_json({"type": "error", "message": str(e)})
                            continue

                        out = np.frombuffer(out_bytes, dtype=np.int16)
                        # Trim the converted context but RETAIN a crossfade's worth of
                        # overlap so consecutive block outputs share overlapping content
                        # for SOLA to align/merge (else the crossfade compresses time).
                        out = st.trim_context(
                            out,
                            context_len,
                            len(infer_input),
                            overlap_keep=st.SOLA_CROSSFADE_SAMPLES,
                        )

                    emit, pending_tail = st.sola_crossfade(pending_tail, out)
                    if len(emit):
                        emit_bytes = emit.tobytes()
                        await ws.send_bytes(emit_bytes)
                        if dbg_out is not None and len(dbg_out) < 48000 * 2 * _DEBUG_MAX_SECONDS:
                            dbg_out.extend(emit_bytes)
                    stats_payload = {
                        "type": "stats",
                        "infer_ms": round(infer_ms, 2),
                        "block_ms": st.BLOCK_MS,
                        "lock_wait_ms": round(lock_wait_ms, 2),
                    }
                    stats_payload.update(block_timing)
                    await ws.send_json(stats_payload)

        except WebSocketDisconnect:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                await ws.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            async with _session_lock:
                _session_active = False
            if dbg_in is not None:
                try:
                    await asyncio.to_thread(
                        _save_debug_wavs, bytes(dbg_in), bytes(dbg_out)
                    )
                except Exception:
                    traceback.print_exc()

    return web_app


# ---- Local testing entrypoint (modal run) ----

@app.function(
    image=trt_image,
    gpu="L4",   # L4 (SM89) matches fastapi_app and TRT engine cache
    timeout=10800,
    volumes={"/root/rvc-models": volume},
    env={"USE_TRT": "1"},
)
def convert_file(audio_bytes: bytes, pitch: int = 0) -> bytes:
    """
    pitch: semitones to shift. 0 = female input (no shift), 12 = male input,
           -1 = auto-detect from audio (runs _auto_detect_pitch).
    """
    test_engine = RVCEngine()
    test_engine.startup()
    return test_engine.run_conversion(audio_bytes, pitch=pitch)


def _load_wav_as_pcm16_mono(audio_bytes: bytes):
    """Loads a WAV file and resamples it to the production pipeline's 16kHz
    mono contract if it isn't already there -- test1.wav is a 48kHz recording,
    not 16kHz, but that's just the test file's own native rate. Real agent mic
    audio arrives to the live pipeline already resampled to 16kHz by LiveKit
    (rtc.AudioStream(track, sample_rate=16000, ...) -- see backend/pipeline.py),
    so resampling here reproduces the SAME 16kHz input the live path actually
    sees, rather than testing on 48kHz audio the live path never encounters."""
    import io
    import wave
    import numpy as np
    import librosa

    with wave.open(io.BytesIO(audio_bytes)) as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    if sw != 2:
        raise ValueError(f"expected 16-bit PCM, got sampwidth={sw}")

    samples = np.frombuffer(raw, dtype=np.int16)
    if nch > 1:
        samples = samples.reshape(-1, nch)[:, 0]  # take first channel

    if sr != st.SAMPLE_RATE_IN:
        print(f"[Chunked Test] Resampling test file {sr}Hz -> {st.SAMPLE_RATE_IN}Hz to match what LiveKit actually delivers live")
        float_samples = samples.astype(np.float32) / 32768.0
        resampled = librosa.resample(float_samples, orig_sr=sr, target_sr=st.SAMPLE_RATE_IN)
        samples = (resampled * 32767.0).clip(-32768, 32767).astype(np.int16)

    return samples


@app.function(
    image=trt_image,
    gpu="L4",   # L4 (SM89) matches fastapi_app; TRT engines are SM89-specific
    timeout=10800,
    volumes={"/root/rvc-models": volume},
)
def convert_file_chunked(
    audio_bytes: bytes,
    pitch: int = -1,
    use_trt: int = 0,
    index_rate: float = 0.75,
    rms_mix_rate: float = 0.75,
    protect: float = 0.33,
) -> bytes:
    """
    Diagnostic: runs the SAME block-accumulate + SOLA-crossfade pipeline the live
    /ws handler uses, driven by a static WAV file. Isolates whether the live call
    quality comes from the chunked-streaming architecture itself.

    use_trt=1 enables the TRT path (sets USE_TRT=1 in the environment before
    engine startup), so you can A/B compare PyTorch vs TRT offline before live rollout.
    """
    import numpy as np
    import os

    if use_trt:
        os.environ["USE_TRT"] = "1"

    test_engine = RVCEngine()
    test_engine.startup()

    samples = _load_wav_as_pcm16_mono(audio_bytes)

    if pitch == -1:
        probe_wav = st.pcm16_to_wav_bytes(samples)
        pitch = test_engine._auto_detect_pitch(probe_wav)
        print(f"[Chunked Test] Whole-file auto-detected pitch_shift={pitch}")

    acc = st.BlockAccumulator()
    acc.push(samples)

    pending_tail = np.zeros(0, dtype=np.int16)
    emitted = []

    while True:
        popped = acc.pop_block()
        if popped is None:
            break
        infer_input, context_len, block = popped

        if st.block_rms(block) < st.SILENCE_RMS_THRESHOLD:
            out = np.zeros(len(block) * 3 + st.SOLA_CROSSFADE_SAMPLES, dtype=np.int16)
        else:
            t0 = time.perf_counter()
            out_bytes = test_engine.convert_block(
                infer_input,
                pitch=pitch,
                index_rate=index_rate,
                rms_mix_rate=rms_mix_rate,
                protect=protect,
            )
            infer_ms = (time.perf_counter() - t0) * 1000
            print(f"[Timing] convert_block: {infer_ms:.1f}ms ({test_engine.engine_kind})")
            out = np.frombuffer(out_bytes, dtype=np.int16)
            out = st.trim_context(
                out, context_len, len(infer_input), overlap_keep=st.SOLA_CROSSFADE_SAMPLES
            )

        emit, pending_tail = st.sola_crossfade(pending_tail, out)
        if len(emit):
            emitted.append(emit.tobytes())

    # Flush the held-back SOLA tail
    if len(pending_tail):
        emitted.append(pending_tail.tobytes())

    return b"".join(emitted)


@app.local_entrypoint()
def main_chunked(
    pitch: int = -1,
    use_trt: int = 0,
    input_file: str = r"D:\Kiera\male_test.wav",
    output_file: str = "",
    index_rate: float = 0.75,
    rms_mix_rate: float = 0.75,
    protect: float = 0.33,
):
    import struct

    print(f"[Chunked Test] Input: {input_file} | pitch_shift={pitch} | use_trt={use_trt} "
          f"| index_rate={index_rate} rms_mix_rate={rms_mix_rate} protect={protect}")
    print("[Chunked Test] Simulates the live /ws block-accumulate + SOLA-crossfade")
    print("[Chunked Test] pipeline on a static file -- compare the output against")
    print("[Chunked Test] test11.wav (convert_file's single-pass output) on the")
    print("[Chunked Test] exact same input file.")

    with open(input_file, "rb") as f:
        audio_bytes = f.read()

    output_pcm = convert_file_chunked.remote(
        audio_bytes,
        pitch=pitch,
        use_trt=use_trt,
        index_rate=index_rate,
        rms_mix_rate=rms_mix_rate,
        protect=protect,
    )

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

    import os
    output_path = output_file or r"D:\Kiera\test11_chunked.wav"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    base, ext = os.path.splitext(output_path)
    counter = 1
    while os.path.exists(output_path):
        output_path = f"{base}_{counter}{ext}"
        counter += 1

    with open(output_path, "wb") as f:
        f.write(bytes(header) + output_pcm)

    print(f"[Chunked Test] Conversion complete -- saved {len(output_pcm)} bytes to {output_path}")
    print("[Chunked Test] Compare against D:\\Kiera\\test11.wav (single-pass) on the same input.")


# A6: export_model_to_onnx() removed — its two jobs (ContentVec download and
# mi-test.onnx export) are now consolidated in modal_deploy/export_onnx.py
# as export_fallback(), which reuses the same checkpoint/config code as
# export_generator() and pins the ContentVec download to a specific revision.


@app.local_entrypoint()
def main(pitch: int = -1, input_file: str = r"D:\Kiera\test1.wav", output_file: str = ""):
    import struct

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

    import os
    output_path = output_file or r"D:\Kiera\test11.wav"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    base, ext = os.path.splitext(output_path)
    counter = 1
    while os.path.exists(output_path):
        output_path = f"{base}_{counter}{ext}"
        counter += 1

    with open(output_path, "wb") as f:
        f.write(bytes(header) + output_pcm)

    print(f"Conversion Complete — saved {len(output_pcm)} bytes of audio")