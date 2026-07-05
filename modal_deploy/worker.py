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
        "RVC",
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
    # Modal does not auto-bundle a sibling module just because worker.py imports
    # it locally — that local import succeeding (during `modal deploy` itself)
    # says nothing about what ships in the remote container. Explicitly mount
    # streaming.py so `import streaming` resolves inside the container too.
    .add_local_python_source("streaming")
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
        f0_method: str = "pm",
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
        print(f"[Timing] file-write: {(t1-t0)*1000:.1f}ms | pitch={pitch} index_rate={index_rate} f0_method={f0_method}")

        info, wav_opt = self.vc.vc_single(
            0,
            input_path,
            pitch,
            None,
            f0_method,
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


# ---- WebSocket streaming session state ----
# MVP = a single concurrent /ws session. A second connection is told
# {"type":"busy"} and closed. Both locks are module-level so they persist for the
# life of the container (one process per Modal container).
_session_active = False
_session_lock = asyncio.Lock()   # guards the _session_active check-and-set
_gpu_lock = asyncio.Lock()       # serializes inference — the GPU is single-tenant


@app.function(
    image=image,
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
        f0_method: str = "pm",
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
                f0_method,
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
            f0_method = cfg.get("f0_method", "pm")

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
                acc.push(np.frombuffer(payload, dtype=np.int16))

                # Drain every complete 320 ms block currently buffered.
                while True:
                    popped = acc.pop_block()
                    if popped is None:
                        break
                    infer_input, context_len, block = popped

                    infer_ms = 0.0
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
                        wav_bytes = st.pcm16_to_wav_bytes(infer_input)
                        try:
                            t0 = time.perf_counter()
                            async with _gpu_lock:  # single-tenant GPU
                                out_bytes = await asyncio.to_thread(
                                    engine.run_conversion,
                                    wav_bytes,
                                    session_pitch,
                                    index_rate,
                                    3,             # filter_radius default
                                    rms_mix_rate,
                                    protect,
                                    f0_method,
                                )
                            infer_ms = (time.perf_counter() - t0) * 1000.0
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
                        await ws.send_bytes(emit.tobytes())
                    await ws.send_json({
                        "type": "stats",
                        "infer_ms": round(infer_ms, 2),
                        "block_ms": st.BLOCK_MS,
                    })

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
    image=image,
    gpu="T4",  # matches convert_file's GPU and the actually-deployed fastapi_app
    timeout=10800,
    volumes={"/root/rvc-models": volume},
)
def convert_file_chunked(audio_bytes: bytes, pitch: int = -1, f0_method: str = "pm") -> bytes:
    """
    Diagnostic (2026-07-03): runs the SAME block-accumulate + SOLA-crossfade
    pipeline the live /ws handler uses (see fastapi_app's ws_stream), but driven
    by a static WAV file instead of a live WebSocket. Isolates whether the live
    call's "doesn't sound like the trained voice" symptom comes from the
    chunked-streaming architecture itself, on the exact same reference audio
    convert_file() already produces a known single-pass conversion for.

    pitch=-1 (default, matches convert_file/main()'s own default) auto-detects
    ONCE from the whole resampled file -- same as the single-pass reference --
    then reuses that one value for every block. This deliberately does NOT
    replicate ws_stream's own "detect from just the first non-silent block"
    behavior: production no longer uses -1 at all (backend/main.py always
    sends an explicit pitch_shift from the UI gender toggle now), so
    replicating the old per-block/first-block detection here would test a
    code path production doesn't actually use. Whole-file detection holds
    "how pitch gets resolved" constant and known-good against the reference,
    so only chunking+SOLA is the variable under test.
    """
    import numpy as np

    test_engine = RVCEngine()
    test_engine.startup()

    samples = _load_wav_as_pcm16_mono(audio_bytes)

    if pitch == -1:
        probe_wav = st.pcm16_to_wav_bytes(samples)
        pitch = test_engine._auto_detect_pitch(probe_wav)
        print(f"[Chunked Test] Whole-file auto-detected pitch_shift={pitch} -- fixed for the whole test")

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
            wav_bytes = st.pcm16_to_wav_bytes(infer_input)
            out_bytes = test_engine.run_conversion(wav_bytes, pitch=pitch, f0_method=f0_method)
            out = np.frombuffer(out_bytes, dtype=np.int16)
            out = st.trim_context(
                out, context_len, len(infer_input), overlap_keep=st.SOLA_CROSSFADE_SAMPLES
            )

        emit, pending_tail = st.sola_crossfade(pending_tail, out)
        if len(emit):
            emitted.append(emit.tobytes())

    # ws_stream never flushes the held-back tail (a real call has no "end of
    # stream" to flush at) -- a one-shot file test does, so flush it here or
    # the last ~80ms crossfade tail is silently dropped from the output.
    if len(pending_tail):
        emitted.append(pending_tail.tobytes())

    return b"".join(emitted)


@app.local_entrypoint()
def main_chunked(pitch: int = -1):
    import struct

    input_file = r"D:\Kiera\test1.wav"

    print(f"[Chunked Test] Input: {input_file} | pitch_shift={pitch} (Note: -1 means whole-file auto-detect, matching main()'s default)")
    print("[Chunked Test] Simulates the live /ws block-accumulate + SOLA-crossfade")
    print("[Chunked Test] pipeline on a static file -- compare the output against")
    print("[Chunked Test] test11.wav (convert_file's single-pass output) on the")
    print("[Chunked Test] exact same input file.")

    with open(input_file, "rb") as f:
        audio_bytes = f.read()

    output_pcm = convert_file_chunked.remote(audio_bytes, pitch=pitch)

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

    output_path = r"D:\Kiera\test11_chunked.wav"
    with open(output_path, "wb") as f:
        f.write(bytes(header) + output_pcm)

    print(f"[Chunked Test] Conversion complete -- saved {len(output_pcm)} bytes to {output_path}")
    print("[Chunked Test] Compare against D:\\Kiera\\test11.wav (single-pass) on the same input.")


@app.local_entrypoint()
def main(pitch: int = -1):
    import struct

    input_file =r"D:\Kiera\test1.wav"

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

    with open(r"D:\Kiera\test11.wav", "wb") as f:
        f.write(bytes(header) + output_pcm)

    print(f"Conversion Complete — saved {len(output_pcm)} bytes of audio")