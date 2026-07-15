# CLAUDE.md — Keira Developer Guidelines

Keira is a real-time voice-conversion softphone. An agent speaks from a browser, their
voice is denoised and converted to a consistent "brand voice" (RVC v2 on a Modal GPU),
and streamed to a lead over the PSTN via Twilio + LiveKit SIP. Conversion is applied
**only** to the agent→lead direction; lead→agent audio is bridged unmodified.

See [README.md](README.md) for account setup and the end-to-end run guide, and
[LATENCY.md](LATENCY.md) for the latency methodology.

There is also a human-facing knowledge wiki at [wiki/](wiki/WIKI.md) — narrative,
cross-referenced pages synthesized from project docs (latency, decisions, incidents),
meant to be browsed (e.g. in Obsidian) or queried directly. It's separate from
`.agents/` below: `.agents/` is terse operating memory for this agent, `wiki/` is for
humans and is updated on request (ingest/query/lint), not read automatically per task.

---

## How to Work Efficiently (low context — this is the DEFAULT, no need to be told)
- The brain is **queried, not loaded**. Never read whole files or the whole `.agents/` tree "to get context."
- Lookup order for ANY task: (1) the ONE relevant `.agents/` file the task scope points to below, (2) at most 2–3 targeted reads. Full-file reads are the last resort.
- Pull ONLY the `.agents/` file the task scope points to — never preload all of them.
- This runs automatically for every task; the user does NOT have to say "use the second brain."

## Agent Routing Instructions
To prevent context dilution, general invariants and rules are split into modular guides. **Always read these files first based on the scope of your task:**

1.  **Identity, Dev Persona & Code Style Rules**:
    *   Location: `.agents/context/identity.md`
    *   Read when: Starting a new session or reviewing coding style, formatting, and response conventions.
2.  **Invariants, Tech Stack & File Map**:
    *   Location: `.agents/context/stack-and-rules.md`
    *   Read when: Touching the audio pipeline (`backend/pipeline.py`), the RVC/Modal integration, Twilio/LiveKit SIP routing, or sample-rate/frame-size contracts.
3.  **Historical Decisions & Migrations**:
    *   Location: `.agents/decisions/log.md`
    *   Read when: Seeking context on why the pipeline was built/changed a certain way, or checking the buffering/latency migration history.
4.  **Active Roadmap & Technical Debt**:
    *   Location: `.agents/projects/active-backlog.md`
    *   Read when: Checking current backlog tasks or known tech debt.
5.  **Subsystem Notes & Load-Bearing Gotchas**:
    *   Location: `.agents/context/subsystem-notes.md`
    *   Read when: Editing pipeline latency/playout, the Modal RVC worker, the Render deployment, or debugging Windows-only degraded-audio behavior — holds the *why* and traps the code can't.

If a non-trivial architecture question can't be answered from the file above, fall back
to [LATENCY.md](LATENCY.md) (pipeline timing detail) or a targeted `git log`/grep before
reading whole source files.

---

## Project Layout

```
backend/
  main.py                     FastAPI app: token broker, Twilio/SIP webhooks, /api/setup,
                              warmup & deploy endpoints, WebSocket signaling, static hosting.
  pipeline.py                 VoiceConversionWorker — the LiveKit bot audio loop.
  converters/
    base.py                   VoiceConverter ABC.
    rvc.py                    RVCVoiceConverter — HTTP to the Modal RVC endpoint (offline-test-only).
    rvc_stream.py             RVCStreamingConverter — persistent WS client to the Modal /ws
                              endpoint; what _do_start_bot actually selects in production.
    dummy.py                  DummyVoiceConverter — ring-mod effect, no API needed (local test).
  noise/noise_suppressor.py   NoiseSuppressor ABC + WebRTCNoiseSuppressor / RNNoiseSuppressor.
  test_pipeline.py            Offline pipeline smoke tests.
modal_deploy/                 Serverless RVC GPU worker deployed to Modal: worker.py (the
                              /health, /convert, /ws endpoints), modal_defs.py (single source
                              for volume/image/trt_image), streaming.py (block/SOLA DSP),
                              trt_pipeline.py (TensorRT engine wrapper), export_onnx.py /
                              compile_trt.py (ONNX export + TRT engine compilation).
frontend/                     Vanilla HTML/JS/CSS agent dashboard (served by FastAPI at /).
scripts/                      build_rnnoise.sh, dataset prep/denoise helpers.
```

---

## Build & Run Commands

### Virtual Environment Setup
```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
```

### Run Server (with live reload)
```bash
uvicorn backend.main:app --reload --port 8000
```
Then open `http://localhost:8000`. The server logs its full config (LiveKit/Twilio/RVC)
at startup — check that block first when something is misconfigured.

### Run Automated Pipeline Tests
```bash
python -m backend.test_pipeline
```

### Deploy the RVC GPU Worker
```bash
modal deploy modal_deploy/worker.py
```
Copy the deployed `/convert` URL into `RVC_ENDPOINT_URL`. `POST /api/deploy` can trigger
this from the running server if `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` are set.

### Compile C-based RNNoise (optional, macOS)
```bash
./scripts/build_rnnoise.sh      # places librnnoise.dylib in backend/libs/
```

---

## Codebase Architecture Guidelines

### Pluggable Interfaces
1. **Voice Conversion** — subclass `VoiceConverter` in [backend/converters/base.py](backend/converters/base.py)
   and implement `async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]`.
   - Input is raw **16 kHz** mono 16-bit PCM, fed in as 20 ms (640-byte) frames.
   - `convert_stream` is now typically driven as ONE long-lived duplex generator for the life of a
     call — `in_audio` is a continuous async iterator, not a single pre-cut phrase — rather than
     invoked fresh per chunk. Implementations should expect to be iterated indefinitely and torn
     down via `contextlib.aclosing` (see `pipeline.py`'s `_run_conversion_stream`), not just
     exhausted after one request/response.
   - Output must be **48 kHz** PCM (the published track is 48 kHz) — the pipeline no longer
     resamples on egress, so a converter returning 16 kHz audio will play back at the wrong
     speed/pitch, not just be mis-sliced.
   - `RVCStreamingConverter` (`backend/converters/rvc_stream.py`) is what `_do_start_bot` actually
     selects when `RVC_ENDPOINT_URL` is set: a WebSocket client to the Modal worker's `/ws`
     endpoint, one persistent duplex connection for the whole call, with bounded reconnect
     buffering/backoff on WS drop (never raw fallback — see "Audio Pipeline & Streaming" below).
     `RVCVoiceConverter` (`backend/converters/rvc.py`, one HTTP POST per `convert_stream` call)
     still exists but is **offline-test-only** now — it is not wired into `_do_start_bot`, only
     exercised by `backend/test_pipeline.py`.
   - Register the engine in `main.py`'s `_do_start_bot`. Selection is by env: an `RVC_ENDPOINT_URL`
     picks `RVCStreamingConverter`, otherwise it falls back to `DummyVoiceConverter`.

2. **Noise Suppression** — subclass `NoiseSuppressor` in [backend/noise/noise_suppressor.py](backend/noise/noise_suppressor.py)
   and implement `def process_frame(self, frame_bytes: bytes) -> bytes`.
   - Expect and return exactly **320 bytes** (10 ms of 16 kHz mono 16-bit PCM).
   - Both built-in suppressors degrade gracefully to passthrough if their native lib is missing.

### Audio Pipeline & Streaming ([backend/pipeline.py](backend/pipeline.py))
Single long-lived duplex stream per call (no VAD/chunking, no reordering — one ordered
WS/generator stream in, one steady playout consumer out), agent→lead only, **fail-closed**:
any conversion outage publishes silence, never the raw unconverted voice. Buffer targets,
block sizes, and the exact mechanism change often and are tracked in one place, not here —
see `.agents/context/stack-and-rules.md` (hard invariants) and
`.agents/context/subsystem-notes.md` (current constants + why) rather than trusting a
remembered number in this file.

### Telephony & SIP ([backend/main.py](backend/main.py))
`POST /api/setup` provisions the LiveKit↔Twilio SIP trunks + dispatch rule and points the
Twilio webhook at this server — **destructive**, see `.agents/projects/active-backlog.md`
before running it against a shared LiveKit project. Both outbound dial and inbound bridge
gate on `worker.is_ready` (fail-closed warm gate) before touching the lead. See
`.agents/context/stack-and-rules.md` (File Map) and `.agents/context/subsystem-notes.md`
for endpoint-level detail, known races, and open findings.

### Environment
Config is read from `.env` (no `.env.example` is checked in — see
[README.md §3](README.md#3-environment-variables-reference) for the reference list):
`LIVEKIT_*`, `RVC_ENDPOINT_URL`, `RVC_API_KEY`, `RVC_PITCH_SHIFT`, `RVC_INDEX_RATE`,
`RVC_WS_URL`, `RVC_KEEPWARM`, `RVC_ADAPTIVE_PITCH`, `RVC_TARGET_F0`, `CORS_ORIGINS`, `TWILIO_*`, `SERVER_URL`. Never commit `.env` or
model files (`.pth`/`.index`/`.wav`) — they are gitignored.

### Code Style
- **Python**: PEP 8, type hints where useful. Never block the event loop — offload CPU/network
  work to threads/tasks via `asyncio` (e.g. `asyncio.to_thread` for subprocess calls).
- **Frontend**: vanilla HTML5 + ES6, global `LivekitClient` namespace, Web Audio API for tone
  generation / the in-browser latency analyzer.

### Windows Gotchas
- `webrtc-noise-gain` has no prebuilt Windows wheel and needs MSVC build tools. If it won't
  build, the import fallback bypasses noise suppression (logs a warning) — tests still pass.
- `webrtcvad` is no longer imported or used anywhere in `backend/pipeline.py` — the streaming
  rebuild deleted VAD-based chunking entirely, so "falls back to fixed-length chunking without it"
  no longer applies to anything. It's now effectively an unused dependency (still Linux-only in
  `backend/requirements.txt`); nothing in this task removes it, but it's worth a look.
- Use `py`/`.venv\Scripts\...` if bare `python` isn't on PATH.
