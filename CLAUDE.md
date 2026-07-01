# CLAUDE.md — Keira Developer Guidelines

Keira is a real-time voice-conversion softphone. An agent speaks from a browser, their
voice is denoised and converted to a consistent "brand voice" (RVC v2 on a Modal GPU),
and streamed to a lead over the PSTN via Twilio + LiveKit SIP. Conversion is applied
**only** to the agent→lead direction; lead→agent audio is bridged unmodified.

See [README.md](README.md) for account setup and the end-to-end run guide, and
[LATENCY.md](LATENCY.md) for the latency methodology.

---

## Project Layout

```
backend/
  main.py                     FastAPI app: token broker, Twilio/SIP webhooks, /api/setup,
                              warmup & deploy endpoints, WebSocket signaling, static hosting.
  pipeline.py                 VoiceConversionWorker — the LiveKit bot audio loop.
  converters/
    base.py                   VoiceConverter ABC.
    rvc.py                    RVCVoiceConverter — HTTP to the Modal RVC endpoint.
    dummy.py                  DummyVoiceConverter — ring-mod effect, no API needed (local test).
  noise/noise_suppressor.py   NoiseSuppressor ABC + WebRTCNoiseSuppressor / RNNoiseSuppressor.
  test_pipeline.py            Offline pipeline smoke tests.
modal_deploy/                 Serverless RVC GPU worker (worker.py) deployed to Modal.
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
   - Input is raw **16 kHz** mono 16-bit PCM.
   - `RVCVoiceConverter` returns **48 kHz** PCM (the published track is 48 kHz). The pipeline
     slices the converted carry-over at 3× (16→48 kHz) — a converter returning 16 kHz will
     be mis-sliced, so match the output rate the pipeline expects (48 kHz for RVC-style engines).
   - Register the engine in `main.py`'s `_do_start_bot`. Selection is by env: an `RVC_ENDPOINT_URL`
     picks `RVCVoiceConverter`, otherwise it falls back to `DummyVoiceConverter`.

2. **Noise Suppression** — subclass `NoiseSuppressor` in [backend/noise/noise_suppressor.py](backend/noise/noise_suppressor.py)
   and implement `def process_frame(self, frame_bytes: bytes) -> bytes`.
   - Expect and return exactly **320 bytes** (10 ms of 16 kHz mono 16-bit PCM).
   - Both built-in suppressors degrade gracefully to passthrough if their native lib is missing.

### Audio Pipeline & Chunking ([backend/pipeline.py](backend/pipeline.py))
- **Producer**: reads the agent track at 16 kHz, denoises each 10 ms frame, and enqueues it.
- **VAD chunking**: the consumer does **not** cut on a fixed interval. It uses `webrtcvad`
  to cut at natural pauses — min chunk 300 ms, cut after 200 ms of silence, hard cap 2500 ms,
  with a 200 ms carry-over prepended to the next chunk to smooth boundaries. If `webrtcvad`
  is unavailable it falls back to fixed max-length chunks.
- **Parallelism**: up to 2 concurrent RVC requests (semaphore); an ordered buffer republishes
  chunks in sequence. Chunks older than 2 s are discarded to bound latency.
- **Fail-safe**: each conversion runs inside `asyncio.timeout(budget_seconds)`. On timeout or
  error, fall back to forwarding the raw denoised chunk (resampled 16→48 kHz) — never drop audio.
- **Publishing**: push exact 10 ms frames of 960 bytes (480 samples @ 48 kHz mono). LiveKit's
  `AudioSource.capture_frame()` handles pacing/backpressure. LiveKit resamples inbound tracks,
  so we subscribe at `sample_rate=16000` and avoid manual resampling on ingress.

### Telephony & SIP ([backend/main.py](backend/main.py))
- **One-time setup**: `POST /api/setup` creates the LiveKit↔Twilio SIP trunks + dispatch rule
  and points the Twilio number's inbound webhook at this server. Run it once after filling env vars.
- **Outbound**: `POST /api/call/outbound` spawns the bot then dials the lead via
  `create_sip_participant` on the LiveKit outbound trunk (LiveKit → Twilio → PSTN).
- **Inbound**: Twilio hits `/api/call/inbound` → caller is held (TwiML) while `/api/call/wait`
  polls until the agent accepts (`/api/call/accept`), then bridges to LiveKit SIP. This avoids
  bridging before LiveKit is ready.
- **Bot identity/subscription**: the bot joins as `voice-converter-bot-<roomName>` and subscribes
  to participants whose identity contains `agent`.
- **Health**: `GET /api/health` reports live integration status; `POST /api/warmup` pings the
  RVC `/health` to warm the GPU (call it at shift start to hide cold-start latency).

### Environment
Config is read from `.env` (see [.env.example](.env.example)): `LIVEKIT_*`, `RVC_ENDPOINT_URL`,
`RVC_API_KEY`, `RVC_PITCH_SHIFT`, `TWILIO_*`, `SERVER_URL`. Never commit `.env` or model files
(`.pth`/`.index`/`.wav`) — they are gitignored.

### Code Style
- **Python**: PEP 8, type hints where useful. Never block the event loop — offload CPU/network
  work to threads/tasks via `asyncio` (e.g. `asyncio.to_thread` for subprocess calls).
- **Frontend**: vanilla HTML5 + ES6, global `LivekitClient` namespace, Web Audio API for tone
  generation / the in-browser latency analyzer.

### Windows Gotchas
- `webrtc-noise-gain` has no prebuilt Windows wheel and needs MSVC build tools. If it won't
  build, the import fallback bypasses noise suppression (logs a warning) — tests still pass.
- `webrtcvad` is likewise optional; without it the pipeline uses fixed-length chunking.
- Use `py`/`.venv\Scripts\...` if bare `python` isn't on PATH.
