# Invariants, Tech Stack & File Map

## Tech Stack
- **Backend**: FastAPI (`backend/main.py`), async Python, `uvicorn`. Deployed to **Render**
  (service `Kiera`, `srv-d932m4cvikkc73belt1g`), now in **Singapore** — colocated with the
  Modal `ap-southeast` pin (verified live via Render API 2026-07-03; service ID changed from
  the old `srv-d92lh7navr4c738i03a0`/Oregon deployment, so the region migration tracked in
  [[active-backlog]] is done, not pending — see [[subsystem-notes]]).
- **Media/Telephony**: LiveKit Cloud (WebRTC room + SIP), Twilio (Elastic SIP Trunk + PSTN
  phone number). Twilio webhook points at the Render server's `/api/call/inbound`.
- **Voice conversion**: RVC v2 model served from a serverless **Modal** GPU worker
  (`modal_deploy/worker.py`, T4, pinned `region="ap-southeast"`), now driven as a persistent
  streaming session over a `/ws` FastAPI/Modal ASGI endpoint (alongside the pre-existing
  `/health`/`/convert` HTTP endpoints) — MVP is single-tenant (1 concurrent session, a
  module-level busy flag). `RVCStreamingConverter` (`backend/converters/rvc_stream.py`) is the
  WS client actually selected in `_do_start_bot`; it holds one long-lived duplex connection for
  the whole call. `RVCVoiceConverter` (`backend/converters/rvc.py`, HTTP-per-call) still exists
  but is offline-test-only now, not wired into the running bot.
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
- **Never publish raw/unconverted audio — fail CLOSED, not fail-safe.** This was rewritten in the
  streaming rebuild (2026-07-02): the old "on conversion timeout/error, forward the raw denoised
  chunk" fallback path was deleted structurally, not just avoided. If the converter's backend
  connection drops or errors, the pipeline publishes **silence** (nothing) until real converted
  audio resumes — never the agent's original voice. Both raw-leak sites in
  `backend/converters/rvc.py` (input `<640B`, empty HTTP response) were fixed the same way: yield
  nothing instead of raw PCM. Any pipeline/converter change must preserve this — do not reintroduce
  a "forward raw on failure" path.
- **Sample rates are fixed by contract, not convention**: ingress 16kHz mono 16-bit PCM (fed to
  converters as 20ms/640-byte frames), converter output 48kHz PCM, published frames are exactly
  960 bytes (10ms @ 48kHz mono). The pipeline no longer resamples anything itself — a converter
  that returns 16kHz output will play back at the wrong speed/pitch, not just be mis-sliced.
- **Noise suppressor frame contract**: `process_frame` must accept/return exactly 320
  bytes (10ms @ 16kHz mono 16-bit PCM).
- **There is no playout queue anymore** — the old "sequence numbers are never reset mid-call"
  invariant doesn't apply to anything that still exists (no `_run_playout`, no
  `_next_publish_seq`). The nearest equivalent invariants now: (1) the conversion stream generator
  must always be torn down via `contextlib.aclosing` (a bare `async for ... break` will not
  reliably close it and stop the converter's backend session) — see [[subsystem-notes]]; (2)
  `VoiceConversionWorker.is_ready` must stay a cheap cached-property read, never a live re-probe,
  since Twilio polls `/api/call/wait` roughly every 3s — see [[subsystem-notes]].
- **Don't push to `main` mid-call during manual testing.** Render's `autoDeploy: commit`
  redeploys on every push, killing the LiveKit worker and any in-flight
  `VoiceConversionWorker`, forcing a Modal cold-start on the next call. This has been
  mistaken for "Modal not connecting" before — see [[subsystem-notes]].
- Never commit `.env` or model files (`.pth`/`.index`/`.wav`) — gitignored by design.

## File Map
| Path | Purpose |
|---|---|
| `backend/main.py` | FastAPI app: token broker, Twilio/SIP webhooks, `/api/setup`, warmup & deploy endpoints, WebSocket signaling, static hosting. |
| `backend/pipeline.py` | `VoiceConversionWorker` — the LiveKit bot audio loop; drives the converter as one long-lived duplex stream (`_run_conversion_stream`) feeding a bounded standing playout buffer drained by `_run_playout_consumer` (2026-07-03, see [[subsystem-notes]]/[[log]]). |
| `backend/converters/base.py` | `VoiceConverter` ABC — pluggability seam #1. |
| `backend/converters/rvc.py` | `RVCVoiceConverter` — HTTP client to the Modal RVC `/convert` endpoint. Offline-test-only now; not selected by `_do_start_bot`. |
| `backend/converters/rvc_stream.py` | `RVCStreamingConverter` — WS client to the Modal RVC `/ws` endpoint; one persistent duplex session per call, bounded reconnect buffer + backoff. What `_do_start_bot` actually selects when `RVC_ENDPOINT_URL` is set. |
| `backend/converters/dummy.py` | `DummyVoiceConverter` — ring-mod effect, no external API (local test). |
| `backend/noise/noise_suppressor.py` | `NoiseSuppressor` ABC + WebRTC/RNNoise implementations — pluggability seam #2. |
| `backend/test_pipeline.py` | Offline pipeline smoke tests, incl. `RVCStreamingConverter` reconnect/backoff/buffer-cap tests. |
| `modal_deploy/worker.py` | Serverless RVC GPU worker deployed to Modal — serves `/health`, `/convert`, and the persistent `/ws` streaming session endpoint (1-concurrent-session MVP). |
| `modal_deploy/streaming.py` | Pure numpy/stdlib streaming DSP for the `/ws` path: `BlockAccumulator`, `trim_context`, `sola_crossfade`, `block_rms`. No Modal/GPU import — unit-testable standalone. |
| `modal_deploy/test_streaming.py` | Unit tests for `modal_deploy/streaming.py` (SOLA crossfade, block accumulation) without Modal/GPU. |
| `frontend/` | Vanilla HTML/JS/CSS agent dashboard. |
| `scripts/` | `build_rnnoise.sh`, dataset prep/denoise helpers. |
| `LATENCY.md` | Latency budget, log-reading guide, live troubleshooting log — read before touching pipeline timing. |
