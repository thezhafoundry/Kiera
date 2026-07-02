# Invariants, Tech Stack & File Map

## Tech Stack
- **Backend**: FastAPI (`backend/main.py`), async Python, `uvicorn`. Deployed to **Render**
  (service `Kiera`, `srv-d92lh7navr4c738i03a0`), currently in **Oregon (us-west)** —
  see [[subsystem-notes]] for why that matters.
- **Media/Telephony**: LiveKit Cloud (WebRTC room + SIP), Twilio (Elastic SIP Trunk + PSTN
  phone number). Twilio webhook points at the Render server's `/api/call/inbound`.
- **Voice conversion**: RVC v2 model served from a serverless **Modal** GPU worker
  (`modal_deploy/worker.py`, T4, pinned `region="ap-southeast"`). `RVCVoiceConverter`
  (`backend/converters/rvc.py`) talks to it over HTTP.
- **Noise suppression**: `WebRTCNoiseSuppressor` / `RNNoiseSuppressor`
  (`backend/noise/noise_suppressor.py`), both degrade to passthrough if native libs are
  missing (notably on Windows — see [[subsystem-notes]]).
- **Frontend**: vanilla HTML5/ES6 dashboard served statically by FastAPI, `LivekitClient`
  global namespace, Web Audio API for the built-in spectral latency test tone/analyzer.
- **Model training**: RVC v2 trained externally via the Retrieval-based-Voice-Conversion
  WebUI (vendored under `RVC/` — third-party, not part of Keira's own code); weights
  (`.pth`/`.index`) uploaded to a Modal volume (`rvc-models`), never committed to git.

## Hard Invariants
- **Direction of conversion is one-way**: only agent→lead audio goes through RVC.
  Lead→agent is bridged raw. Never route lead audio through the converter.
- **Never drop call audio.** Every conversion call runs inside `asyncio.timeout`; on
  timeout/error the pipeline forwards the raw denoised chunk (resampled 16→48kHz) instead
  of silence. Any pipeline change must preserve this fail-safe path.
- **Sample rates are fixed by contract, not convention**: ingress 16kHz mono 16-bit PCM,
  RVC output 48kHz PCM, published frames are exactly 960 bytes (10ms @ 48kHz mono). A
  converter that returns 16kHz output will be mis-sliced by the pipeline's 3x carry-over
  slicing — match 48kHz out or change the slicing math, don't do one without the other.
- **Noise suppressor frame contract**: `process_frame` must accept/return exactly 320
  bytes (10ms @ 16kHz mono 16-bit PCM) — the VAD/chunking logic assumes this.
- **Sequence numbers in the playout queue are never reset mid-call** — only the buffering
  phase resets on `track_unsubscribed`. Resetting `_next_publish_seq` would make playout
  wait forever for sequence numbers already consumed by the dispatch side. See
  [[subsystem-notes]] for the full ordered-playout design.
- **Don't push to `main` mid-call during manual testing.** Render's `autoDeploy: commit`
  redeploys on every push, killing the LiveKit worker and any in-flight
  `VoiceConversionWorker`, forcing a Modal cold-start on the next call. This has been
  mistaken for "Modal not connecting" before — see [[subsystem-notes]].
- Never commit `.env` or model files (`.pth`/`.index`/`.wav`) — gitignored by design.

## File Map
| Path | Purpose |
|---|---|
| `backend/main.py` | FastAPI app: token broker, Twilio/SIP webhooks, `/api/setup`, warmup & deploy endpoints, WebSocket signaling, static hosting. |
| `backend/pipeline.py` | `VoiceConversionWorker` — the LiveKit bot audio loop (producer/consumer/playout). |
| `backend/converters/base.py` | `VoiceConverter` ABC — pluggability seam #1. |
| `backend/converters/rvc.py` | `RVCVoiceConverter` — HTTP client to the Modal RVC endpoint. |
| `backend/converters/dummy.py` | `DummyVoiceConverter` — ring-mod effect, no external API (local test). |
| `backend/noise/noise_suppressor.py` | `NoiseSuppressor` ABC + WebRTC/RNNoise implementations — pluggability seam #2. |
| `backend/test_pipeline.py` | Offline pipeline smoke tests. |
| `modal_deploy/worker.py` | Serverless RVC GPU worker deployed to Modal. |
| `frontend/` | Vanilla HTML/JS/CSS agent dashboard. |
| `scripts/` | `build_rnnoise.sh`, dataset prep/denoise helpers. |
| `LATENCY.md` | Latency budget, log-reading guide, live troubleshooting log — read before touching pipeline timing. |
