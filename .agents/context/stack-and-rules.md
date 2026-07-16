# Invariants, Tech Stack & File Map

## Tech Stack
- **Backend**: FastAPI (`backend/main.py`), async Python, `uvicorn`. Deployed to **Render**
  (service `Kiera`, `srv-d932m4cvikkc73belt1g`), now in **Singapore** — colocated with the
  stable Modal function's `ap-southeast` compute pin (verified live via Render API 2026-07-03;
  service ID changed from
  the old `srv-d92lh7navr4c738i03a0`/Oregon deployment, so the region migration tracked in
  [[active-backlog]] is done, not pending — see [[subsystem-notes]]).
- **Media/Telephony**: LiveKit Cloud (WebRTC room + SIP), Twilio (Elastic SIP Trunk + PSTN
  phone number). Twilio webhook points at the Render server's `/api/call/inbound`.
- **Voice conversion**: RVC-first production path; LLVC scaffolding retained but paused.
  - **RVC v2 (Stable Default)**: Served from a Modal **L4/TensorRT** worker (`modal_deploy/worker.py`) over one persistent `/ws` session. Modal v11 exposes the legacy `fastapi_app` (`region="ap-southeast"`, default US input routing) and experimental `fastapi_app_ap` (`routing_region="ap-south"`, broad `region="ap"`). Render still selects the legacy endpoint; do not switch it until both are benchmarked from Render Singapore. Each function allows at most two containers; each container admits one active stream.
  - **LLVC (Paused)**: `LLVC_PILOT_ENABLED=false`. `LLVCStreamingConverter`, the fake server, bounded reconnect logic, and the 2s fail-closed watchdog remain verified scaffolding. Do not train/deploy a per-voice LLVC model for the SaaS onboarding path; reassess a zero-shot or lightweight-conditioned streaming model after RVC optimization. RVC HTTP `RVCVoiceConverter` remains offline-test-only.
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
- **The standing playout buffer is load-bearing.** `_run_conversion_stream` appends converted
  48kHz PCM to bounded `_playout_buffer`; `_run_playout_consumer` drains it at a steady pace.
  The current target is 0.25s with a 5s cap; preserve drop-oldest overflow, the wake-up event,
  and exact 960-byte publication frames unless a live quality/latency test approves a change.
  The old sequence-number queue (`_run_playout` / `_next_publish_seq`) is gone, but the current
  byte-buffer queue is present — see [[subsystem-notes]].
- **Always close the conversion generator.** Use `contextlib.aclosing`; a bare
  `async for ... break` will not reliably stop the converter's backend session.
- **Readiness polling must stay cheap.** `VoiceConversionWorker.is_ready` is a cached property,
  never a live network probe, because Twilio polls `/api/call/wait` roughly every 3s.
- **LLVC 2.0-second Watchdog**: If LLVC is the active voice engine, the pipeline monitors the stream's output. If no chunks are received for 2.0 seconds, the fatal watchdog triggers to terminate the call programmatically (hanging up Twilio and deleting the LiveKit room) to prevent any raw voice leakage.
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
| `backend/converters/llvc_stream.py` | `LLVCStreamingConverter` — WS client to the LLVC model server `/ws` endpoint; one persistent duplex session per call, bounded reconnect buffer + backoff. |
| `backend/converters/llvc_fake_server.py` | `llvc_fake_ws_handler` — Local mock LLVC server used for pipeline integration testing and concurrency limit validation. |
| `backend/converters/dummy.py` | `DummyVoiceConverter` — ring-mod effect, no external API (local test). |
| `backend/noise/noise_suppressor.py` | `NoiseSuppressor` ABC + WebRTC/RNNoise implementations — pluggability seam #2. |
| `backend/test_pipeline.py` | Offline pipeline smoke tests, incl. `RVCStreamingConverter` reconnect/backoff/buffer-cap tests. |
| `modal_deploy/worker.py` | RVC GPU worker deployed to Modal — shared `/health`, `/convert`, and persistent `/ws` ASGI app behind stable `fastapi_app` and experimental AP-routed `fastapi_app_ap`; one stream per container, maximum two containers per function. |
| `modal_deploy/modal_defs.py` | Single source of truth for the Modal `volume`/`image`/`trt_image` definitions (TRT migration). |
| `modal_deploy/trt_pipeline.py` | `TRTVoicePipeline` — 3-engine (HuBERT/generator/RMVPE) static-shape TensorRT inference wrapper. |
| `modal_deploy/export_onnx.py` / `modal_deploy/compile_trt.py` | ONNX exporters with parity gates, and TRT engine-cache priming with a fatal ≤400ms benchmark gate. |
| `modal_deploy/streaming.py` | Pure numpy/stdlib streaming DSP for the `/ws` path: `BlockAccumulator`, `trim_context`, `sola_crossfade`, `block_rms`. No Modal/GPU import — unit-testable standalone. |
| `modal_deploy/test_streaming.py` / `modal_deploy/test_trt_pipeline.py` | Unit tests for `modal_deploy/streaming.py` and the TRT pipeline, without live Modal/GPU. |
| `frontend/` | Vanilla HTML/JS/CSS agent dashboard. |
| `scripts/rvc_stream_benchmark.py` | Synthetic call-long benchmark for the real RVC WebSocket path; reports readiness, inference/network estimates, duration delta, profile/model metadata, and drop counters. |
| `scripts/` | `build_rnnoise.sh`, dataset prep/denoise helpers, second-brain close-out tooling. |
| `LATENCY.md` | Latency budget, log-reading guide, live troubleshooting log — read before touching pipeline timing. |
