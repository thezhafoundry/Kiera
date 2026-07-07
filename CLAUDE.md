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
- **Producer**: reads the agent track at 16 kHz, denoises each 10 ms (320-byte) frame with the
  active `NoiseSuppressor`, and enqueues it with an ingress timestamp.
- **No chunking, no VAD.** `webrtcvad` is not imported or used anywhere in `pipeline.py` anymore —
  the whole "cut at natural pauses" concept from the old design is gone. `_frame_pairs()` simply
  pairs two consecutive denoised 10 ms frames into one 20 ms (640-byte) input frame and feeds it
  continuously into `converter.convert_stream(...)`, which is driven as a single long-lived duplex
  stream for the life of the worker's active pipeline.
- **No reordering, no semaphore.** `_run_conversion_stream()` republishes whatever the converter
  yields, in arrival order — a single ordered stream (WS or local generator) has nothing to
  reorder, so the old parallel-RVC-request semaphore no longer exists.
- **Standing playout buffer (reintroduced 2026-07-03).** The original rebuild's one-shot ~100ms
  jitter fill only smoothed the *start* of a call and starved on any slow block. It's since been
  replaced by a producer/consumer split: `_run_conversion_stream` appends converted audio to a
  bounded `self._playout_buffer` (1.25s target/5s cap as of the TRT migration phase 1, down
  from an earlier 3s target — see `.agents/context/subsystem-notes.md`; drop-oldest on
  overflow), and a separate
  `_run_playout_consumer` task drains it into `_publish_frames` at a steady pace — a slow RVC
  block now grows delay instead of producing "part by part" audio. This reflects a deliberate
  2026-07-03 product decision that call latency is not a priority, voice continuity is — see
  `.agents/context/subsystem-notes.md` for the full mechanism and `.agents/decisions/log.md` for
  the tradeoff reasoning.
- **Fail-CLOSED, never raw.** There is no raw-audio-fallback path — it was removed structurally,
  not just avoided. If the converter's backend connection drops or errors, output is **silence**
  (nothing published) until real converted audio resumes; the lead never hears the agent's
  original, unconverted voice. See `RVCStreamingConverter` in `backend/converters/rvc_stream.py`
  for the bounded (5 s) reconnect-buffer + exponential-backoff behavior backing this.
- **Publishing**: push exact 10 ms frames of 960 bytes (480 samples @ 48 kHz mono). LiveKit's
  `AudioSource.capture_frame()` handles pacing/backpressure. LiveKit resamples inbound tracks,
  so we subscribe at `sample_rate=16000` and avoid manual resampling on ingress.

### Telephony & SIP ([backend/main.py](backend/main.py))
- **One-time setup**: `POST /api/setup` creates the LiveKit↔Twilio SIP trunks + dispatch rule
  and points the Twilio number's inbound webhook at this server. Run it once after filling env vars.
- **Outbound**: `POST /api/call/outbound` spawns the bot then dials the lead via
  `create_sip_participant` on the LiveKit outbound trunk (LiveKit → Twilio → PSTN). **Fail-closed
  warm gate**: after spawning the bot it `await`s `worker.wait_until_ready(150.0)` before dialing;
  if the converter's backend isn't ready in time, the bot is stopped and the endpoint returns
  HTTP 503 instead of ringing the lead — a cold/unready GPU now blocks the call rather than
  proceeding and leaking anything.
- **Inbound**: Twilio hits `/api/call/inbound` → caller is held (TwiML) while `/api/call/wait`
  polls until the agent accepts (`/api/call/accept`) **and** the bot's `worker.is_ready` is true,
  then bridges to LiveKit SIP. `is_ready` is a cheap cached property backed by a one-shot
  background readiness probe started when the bot spawns (not a fresh network probe on every
  ~3s poll). This avoids bridging before LiveKit — or the voice engine — is ready.
- **Bot identity/subscription**: the bot joins as `voice-converter-bot-<roomName>` and subscribes
  to participants whose identity contains `agent`.
- **Health**: `GET /api/health` reports live integration status; `POST /api/warmup` pings the
  RVC `/health` to warm the GPU (call it at shift start to hide cold-start latency).

### Environment
Config is read from `.env` (no `.env.example` is checked in — see
[README.md §3](README.md#3-environment-variables-reference) for the reference list):
`LIVEKIT_*`, `RVC_ENDPOINT_URL`, `RVC_API_KEY`, `RVC_PITCH_SHIFT`, `RVC_INDEX_RATE`,
`RVC_WS_URL`, `RVC_KEEPWARM`, `CORS_ORIGINS`, `TWILIO_*`, `SERVER_URL`. Never commit `.env` or
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
