# Latency Measurement Analysis & Guide

This document presents the latency profile for the real-time voice conversion pipeline
([backend/pipeline.py](backend/pipeline.py)), a detailed latency budget breakdown for the
current RVC v2 / Modal GPU engine, guidelines for running latency verification, and
troubleshooting steps for Modal GPU connection/cold-start issues.

> **Authoritative 2026-07-16 RVC-first snapshot:** Modal v11 is deployed with the stable
> baseline profile (320ms block / 400ms context / 80ms SOLA / 250ms playout), TensorRT on
> an NVIDIA L4, hot TRT cache, and an artifact-derived model/index fingerprint. A 9.6s
> synthetic persistent-WebSocket run produced 30 blocks with inference median/p95
> **50.75/51.61ms**, converter-wait median/p95 **1207.11/1358.56ms**, estimated network
> median/p95 **837.05/988.91ms**, no drops, and **-211.46ms** output-duration delta. The
> benchmark originated from a developer laptop, so it is not a production Render or PSTN
> mouth-to-ear result. The legacy Modal function is still the Render-selected stable path;
> a parallel `fastapi_app_ap` function (Mumbai input routing, broad AP placement) is deployed
> but must be benchmarked from Render Singapore before any switch. LLVC is paused and disabled.

> **2026-07-02 update:** Pulled live Render logs (`Kiera`, `srv-d92lh7navr4c738i03a0`) and
> pinged the Modal endpoint directly while diagnosing a "Modal not connecting / GPU not
> starting" report — see §4 for the confirmed root causes (redeploys killing in-flight calls,
> and cold-start taking much longer than assumed).

> **2026-07-02 streaming-rebuild update:** `backend/pipeline.py`, the Modal worker
> (`modal_deploy/worker.py` + new `modal_deploy/streaming.py`), and the converters
> (`backend/converters/rvc_stream.py`) were rewritten from the VAD-chunked, HTTP-per-request
> design this document originally described to a persistent-WebSocket streaming design — see
> [CLAUDE.md](CLAUDE.md) and `.agents/decisions/log.md` for the full rationale. **§1's budget
> table below has been rewritten to describe the new pipeline's stages, but the numbers in it
> are design targets from the rebuild plan, not live measurements** — there is no deployed
> Modal `/ws` worker or live call available in this environment to re-measure against (that
> deploy is a separate, still-pending task). §2's log-reading guide has been rewritten to match
> the actual log lines/messages in the current code. §3 (the spectral-tone test method) is
> unchanged but needs a fresh live pass once a `/ws` deployment exists. §4's pre-rebuild
> investigation (cold-start timing, the raw-voice incident) is preserved as historical record,
> with notes added where the fail-closed rebuild changes the failure mode going forward. §5
> (the old ordered-playout-queue design) is retitled and kept as historical record — that
> machinery no longer exists in the code.

> **Historical 2026-07-03 / 2026-07-07 update:** a bounded standing playout buffer was
> reintroduced on 2026-07-03 and reduced from ~3s to 1.25s during TRT phase 1. Phase 2 then
> reduced it to the **current 250ms baseline target** (5s cap, 100ms steady drain). The Modal
> worker also moved from a
> T4 to an **L4** GPU (2026-07-03) and now runs an optional **TensorRT** path
> (`USE_TRT=1`) using RMVPE pitch tracking instead of `pm` — see
> `.agents/context/subsystem-notes.md`'s TensorRT/ONNX migration section for the C3 benchmark
> (median 66ms/p95 68ms inference on a live L4). The Render↔Modal region mismatch described in
> §4.1/Region note below was **resolved 2026-07-03** — Render is now confirmed live in
> Singapore. The current dual-edge routing experiment and 2026-07-16 measurements are recorded
> in the authoritative snapshot above and §1; older region/buffer prose is historical.

> **2026-07-16 LLVC decision:** the LLVC service/training path is paused because Keira is a
> multi-client SaaS where each client supplies a target voice; training a separate causal LLVC
> model from scratch per voice is not the desired onboarding model. Keep
> `LLVC_PILOT_ENABLED=false`. The converter, fake service, watchdog, and dataset/benchmark tools
> remain test scaffolding, not a deployed performance claim. Optimize RVC first, then evaluate a
> zero-shot or lightweight voice-conditioning streaming model.

---

## 1. Current RVC latency budget and measured converter baseline

In a real-time voice call, total mouth-to-ear latency is composed of multiple steps in the
media transport and conversion pipeline. The stages below reflect the streaming pipeline
(`backend/pipeline.py` + `modal_deploy/streaming.py`'s `/ws` engine); there is no more VAD
chunking, but a standing playout buffer **was** reintroduced 2026-07-03 (see the top-of-document
banner) — it is the single largest term in the budget below and is not optional.

$$\text{Mouth-to-Ear Latency} = T_{\text{ingress}} + T_{\text{noise\_suppression}} + T_{\text{frame\_batch}} + T_{\text{block\_accumulate}} + T_{\text{inference}} + T_{\text{SOLA\_crossfade}} + T_{\text{jitter\_buffer}} + T_{\text{egress}}$$

| Pipeline Step | Target Duration | Description |
| :--- | :--- | :--- |
| **Ingress Network** | 20 - 45 ms | Browser capture, OPUS encoding, WebRTC transport to LiveKit, and forwarding to the Python worker at 16 kHz mono. Unchanged by this rebuild (frontend/LiveKit transport untouched) — carried forward from the pre-rebuild estimate. |
| **Noise Suppression** | 0.2 - 0.5 ms | `WebRTCNoiseSuppressor` processing of 10ms/320-byte frames. Sub-millisecond on CPU. Unchanged by this rebuild. |
| **Input Frame Batching** | up to 20 ms | `_frame_pairs()` in `backend/pipeline.py` pairs two consecutive 10ms denoised frames into one 20ms (640-byte) input frame before it's sent into `convert_stream` — the plan's "Input frame to WS" parameter. |
| **Inference Block Accumulation** | up to 320 ms (RVC) / **0 ms (LLVC)** | `modal_deploy/streaming.py::BlockAccumulator` only pops a block once 320ms of *new* audio has arrived (`BLOCK_MS`/`BLOCK_SAMPLES_IN`) for RVC. LLVC has no block accumulation step (processes 20 ms frames in real-time). |
| **GPU Inference (warm)** | RVC baseline median **50.75ms**, p95 **51.61ms** (30 live L4/TRT blocks, 2026-07-16) | `RVCEngine.convert_block` / `TRTVoicePipeline` inside the `/ws` handler. The earlier C3/Candidate-Phase measurements (66/68ms and 54/55ms) remain historical comparisons. |
| **GPU Inference (cold)** | **72.51s active readiness** in the 2026-07-16 baseline run; scheduling varies | The fail-closed warm gate blocks call progression until the engine is ready. Modal logged delayed L4 acquisition for the narrow Singapore placement and recommended broader `ap` placement. Startup also logs a non-fatal `F0Predictor` import failure during its nominal warm-up, which remains open. |
| **SOLA Crossfade** | 80ms baseline | `modal_deploy/streaming.py::sola_crossfade` holds a tail and aligns adjacent converted blocks. The finite benchmark returned 211.46ms less audio than its 9.6s input, more than the nominal 80ms held tail; duration preservation is an open gate. |
| **Standing Playout Buffer** | **250ms baseline target**, 5s cap; 100ms steady drain | `VoiceConversionWorker` applies the profile's `playout_ms` after readiness metadata. This is no longer the stale 1.25s Phase-1 value. |
| **Egress Network** | 20 - 40 ms | Publishing 960-byte (10ms @ 48kHz) frames to LiveKit, then WebRTC + browser jitter-buffer playout to the lead. Unchanged by this rebuild. |
| **Measured converter path** | Baseline converter-wait median **1207.11ms**, p95 **1358.56ms** from the developer-laptop run | This is not mouth-to-ear. Its estimated network component was 837.05ms median / 988.91ms p95 because the legacy web function uses Modal's default Virginia input routing. Re-measure from Render Singapore before using it for production decisions. |
| **Steady-state mouth-to-ear** | **Not yet measured for the 2026-07-16 checkout** | Requires browser → LiveKit → Render → Modal → LiveKit SIP/Twilio → PSTN spectral/physical measurement. Do not add the converter benchmark to the 250ms playout target and label the sum as mouth-to-ear without that test. |
| **Cold-GPU Total** | **Call blocked (503 outbound / held inbound) until ready, no audio published** | Replaces the old "30-90+s of raw voice, then fail-over recovers" row — there is no raw-voice period anymore; see the Inference (cold) row above and §4.2. |

Region note: Render is confirmed in Singapore. Modal v11 exposes two functions from the same
worker code and model volume:

- `fastapi_app` — stable/legacy endpoint, compute pinned `ap-southeast`, default Modal input
  routing through `us-east`.
- `fastapi_app_ap` — experimental endpoint, `routing_region="ap-south"` and broad
  `region="ap"`; its first health request selected `ap-northeast-1` (Tokyo).

The second endpoint improves scheduling options and changes the input route, but Tokyo is not
colocated with Render Singapore. Keep Render on the stable endpoint until both are benchmarked
from Render using identical input. Laptop geography is not a substitute.

---

## 2. Reading Live Latency Logs

Per-block latency varies with GPU warm/cold state and load, so treat the log lines below as
**how to read the logs**, not as fixed benchmarks. Run your own pass (§3) after any pipeline or
model change and capture fresh numbers. The 2026-07-16 values in §1 are live converter-path
measurements; transport estimates and historical examples elsewhere are labelled separately.

These are the actual log lines/messages in the current streaming pipeline, not the old
chunk/playout-buffer lines (which no longer exist in the code).

Worker-side (`backend/pipeline.py`) log lines to watch during a call:

```text
[Worker] Connecting to room: wss://...
[Worker] Connected. Identity: voice-converter-bot-<roomName>
[Worker] Subscribed to agent track TR_xxx from participant agent-1
[Worker] Published converted-audio track to the room.
[Worker] Started reading remote audio stream @ 16kHz mono
[Worker Error in conversion stream] <exception text>
[Worker] Readiness probe failed: <exception text>
```

- `[Worker] Subscribed to agent track ...` — confirms the pipeline started reading the agent's
  mic track; if this never appears, no audio is flowing into the converter at all (check the
  agent participant's identity contains `"agent"`, per `on_track_subscribed`).
- `[Worker Error in conversion stream] ...` / a traceback — the conversion stream task hit an
  unhandled exception (not a normal reconnect, which is handled silently inside
  `RVCStreamingConverter` — see below). Output is silence until this task is retried/reset.
- `[Worker] Readiness probe failed: ...` — the one-shot `start_readiness_probe()` background
  task raised; `is_ready`/`wait_until_ready` will report not-ready. Check the exception text for
  the underlying cause (usually a WS connect failure to the converter's backend).
- There is no more `[Latency] {X}ms total (conversion: {Y}ms)` per-chunk line, no fail-safe
  fallback log, no "queue backed up" log, and no adaptive-playout-buffer logs — that entire
  logging surface was deleted along with the machinery it described.

`RVCStreamingConverter` (`backend/converters/rvc_stream.py`) logs via the standard `logging`
module (logger name `backend.converters.rvc_stream`), not `print`, so make sure your logging
config surfaces `WARNING`-level output to see these:

```text
[RVCStreamingConverter] WS connection lost/failed: <exception text>
[RVCStreamingConverter] reconnect buffer full (500ms cap) — dropped oldest input frame (640 bytes)
[RVCStreamingConverter] WS send failed — requeuing frame, will reconnect
[RVCStreamingConverter] server error: <message>
[RVCStreamingConverter] wait_ready failed: <exception text>
```

- `WS connection lost/failed` — the persistent session's socket dropped; `_connection_loop`
  will retry with exponential backoff (0.5s → 5s cap) while incoming audio buffers only the
  newest 500ms (oldest dropped first) — the lead hears silence for the outage, never raw voice,
  and stale speech is not replayed after reconnect.
- `reconnect buffer full ... dropped oldest input frame` — the outage lasted long enough that
  the 500ms input buffer filled and started dropping the oldest buffered audio; that audio is lost
  (never converted), but nothing raw is ever leaked to the lead.
- `server error: <message>` — the server sent `{"type":"error",...}` for one inference block
  (see the worker-side messages below); that block emits no audio and the session stays open.

Server-side (`modal_deploy/worker.py`'s `/ws` handler) sends these JSON messages over the
socket, which the client logs/acts on:

```text
{"type": "ready"}                                  # handshake success, session accepted
{"type": "busy"}                                   # a session is already active (1-concurrent MVP)
{"type": "error", "message": "..."}                 # engine-not-ready at handshake, or one block's inference failed
{"type": "stats", "infer_ms": 210.4, "block_ms": 320}   # per-block timing, after every processed block (voiced or silence-bypassed)
{"type": "pong"}                                    # keepalive reply to a client "ping"
```

- `{"type": "stats", "infer_ms":.., "block_ms":..}` is the latency source of truth on the
  client: `VoiceConversionWorker._on_converter_stats` sums `infer_ms + block_ms` into
  `pipeline_latency_ms`, published to the frontend over the room data channel (same
  `pipeline_latency_ms` / `is_fallback` JSON shape the dashboard has always parsed). Note
  `is_fallback` no longer means "raw audio was sent" (that path is gone) — it now doubles as a
  "HOLDING" indicator: `VoiceConversionWorker._is_holding` flips true if no converted chunk has
  arrived for 750ms (`_HOLD_TIMEOUT_S`), e.g. mid-reconnect, and flips false again once real
  audio resumes.
- `RVCEngine.run_conversion` (shared by both `/convert` and the `/ws` handler, unchanged by this
  rebuild) still prints its own per-call timing breakdown to the Modal container logs:
  `[Timing] file-write: ...ms | pitch=... index_rate=...`, `[Timing] vc_single: ...ms | <info>`,
  `[Timing] total run_conversion: ...ms → N bytes` — useful for isolating GPU inference time from
  the block-accumulation/network overhead around it.

---

## 3. How to Run the Mouth-to-Ear Latency Test

> **Note (2026-07-02 streaming rebuild):** this method itself is unchanged — the frontend/browser
> test tooling wasn't touched by the rebuild — but every number in §1 is a design target, not a
> live result. Re-run this test end-to-end against a real `/ws` deployment (once that's deployed
> — a separate, still-pending task) and replace §1's target numbers with what you actually
> measure before trusting them for real capacity/SLA planning.

The PoC includes a built-in, automated spectral tone latency analyzer that bypasses acoustic
noise and measures delay digitally in the browser.

### Automatic Spectral Test (Recommended)
1. Set up and run the server (see [README.md](README.md)). Confirm `RVC_ENDPOINT_URL` is set
   and `GET /api/health` reports it — otherwise the bot silently falls back to
   `DummyVoiceConverter` and you'll measure the wrong pipeline.
2. Hit `POST /api/warmup` (or wait for the automatic pre-warm ping fired on bot start) so the
   Modal GPU (L4 as of 2026-07-03) is warm **before** you measure — see §4. With the fail-closed warm gate, a
   still-cold GPU won't give you a bad measurement to sanity-check — it'll just block the call
   (outbound 503, or inbound stuck on hold), so there's nothing to measure until the engine
   reports ready.
3. Open two separate browser tabs (or use two devices to prevent speaker-to-mic feedback loop).
4. Click **Spawn Bot** in the Room Setup panel.
5. On Tab 1 (or Device 1), click **Join as Agent**.
6. On Tab 2 (or Device 2), click **Join as Listener**. Make sure speakers are on.
7. In the Agent panel (Tab 1), click **Play Latency Test Tone**.
   - This mutes the microphone and injects a clean, 100ms sinusoidal pulse at exactly 1kHz into
     the WebRTC stream.
8. The Listener browser (Tab 2) runs an FFT (Fast Fourier Transform) on both the incoming raw
   agent track and the converted bot track:
   - When the 1kHz peak is detected on the raw track, it marks $T_1$.
   - When the 1kHz peak is detected on the converted track, it marks $T_2$.
   - It calculates the exact difference: $\Delta T = T_2 - T_1$.
9. The calculated **Mouth-to-Ear Latency** is displayed instantly on the Listener screen in
   milliseconds.

### Manual Physical Test (Clap/Tap Test)
If you wish to perform a physical mouth-to-ear test:
1. Ensure the Agent and Listener are physically in the same room.
2. The Agent claps their hands sharply near their microphone.
3. Use a recording device (e.g. your smartphone) to record the room audio.
4. The recording will capture:
   - The original physical clap (sound 1).
   - The delayed converted clap coming from the Listener's speakers (sound 2).
5. Load the recording into a free audio editor (like Audacity) and measure the time distance
   between the peaks of sound 1 and sound 2. This represents the true physical mouth-to-ear
   latency.

---

## 4. Troubleshooting: Modal Not Connecting / GPU Not Starting

### 4.1 Confirmed root causes (from a live 2026-07-02 investigation)

Pulled Render logs for the `Kiera` service (`srv-d92lh7navr4c738i03a0`) and pinged the Modal
endpoint directly. Two concrete issues showed up:

- **Render redeploys kill the bot mid-call.** The service has `autoDeploy: commit` — every
  push to `main` triggers a full redeploy (`pip install` → new process → old process
  `Shutting down`). Logs show this happening **twice within ~4 minutes** (04:58:40 and
  05:02:22 UTC) during an active test call. Each redeploy tears down the LiveKit worker,
  drops the in-flight `VoiceConversionWorker`, and forces the next call to cold-start Modal
  from scratch — this alone can look exactly like "GPU never starts" if you're iterating on
  code and testing calls in the same session. Avoid pushing to `main` while a test call is
  running, or expect to re-warm after every deploy.
- **Cold start is slower than the code assumes.** A direct `/health` ping against an idle
  container got **no response for ~75 seconds** before it came back `{"status":"ready",
  "cuda_available":true,"cuda_device":"Tesla T4"}`. The code comments (and previous version of
  this doc) assumed 8-30s. If you're testing with a short timeout (e.g. a bare `curl` with a
  15-20s cap, rather than `POST /api/warmup`'s 30s-interval retry loop), you will see nothing
  but timeouts and reasonably conclude Modal is broken when it's actually just slow to wake up.
  During the same window, RVC calls that *did* land while the GPU was warm completed in
  580-750ms — the engine itself works fine once the container is up.
- **Region mismatch — resolved 2026-07-03:** the Modal worker is pinned to
  `region="ap-southeast"` (Singapore, per the comment in
  [modal_deploy/worker.py](modal_deploy/worker.py)) on the premise that Render/Twilio also run
  in that region. This was open as of this 2026-07-02 investigation (Render was in Oregon at
  the time), but the Render service was confirmed live in Singapore via the Render API on
  2026-07-03 (service ID changed from `srv-d92lh7navr4c738i03a0` to `srv-d932m4cvikkc73belt1g`)
  — see `.agents/context/stack-and-rules.md`. The transpacific-round-trip risk described below
  no longer applies; kept as historical record of what to check if the region ever drifts again.

### 4.2 Confirmed production incident: lead heard the agent's raw voice for a whole call

Traced a report of "the lead was getting the agent's original voice, not the trained voice"
to a specific outbound call at **06:35:41 UTC on 2026-07-02** (Render logs, room
`outbound_916281686616_1782974101`):

- The bot's pre-warm ping fired at call start, but the bot began publishing audio only ~30s
  later — nowhere near the ~75s+ cold start measured in §4.1.
- **Every single chunk for the entire call** logged `Conversion TIMEOUT after ~2000ms →
  Triggering fail-safe!`, i.e. permanent fallback to raw voice, not just the first chunk or
  two as the fail-safe is meant to cover.
- A second bot spawn fired ~2s after the first (same lead number), which likely reset/duplicated
  the warm-up cycle instead of reusing it.

**Fix applied**: `POST /api/call/outbound` ([backend/main.py](backend/main.py)) now calls the
new `_wait_for_rvc_ready()` helper — polling `/health` every 5s for up to 90s — **after**
spawning the bot but **before** creating the SIP participant that rings the lead. The lead's
phone no longer starts ringing until the RVC engine reports `ready` (or the 90s cap is hit, in
which case it dials anyway and logs a warning rather than blocking outbound calling outright).
`/api/warmup` was refactored to call the same helper (unchanged behavior: 30s interval, 6
minute cap) so the polling logic isn't duplicated between the two call sites.

The duplicate bot-spawn observed in this incident is still open — worth a follow-up look if
"call twice in quick succession for the same lead" is reproducible.

> **2026-07-02 streaming-rebuild note:** this incident is preserved here as historical record of
> what happened under the **old** fail-safe/fail-open design. The streaming rebuild is the
> structural fix for this exact class of incident: the raw-audio-fallback path
> (`_convert_chunk`'s timeout → raw denoised chunk) was deleted from the code, not just avoided,
> and the pre-dial/pre-bridge warm gate is now fail-**closed** (`worker.wait_until_ready` /
> `is_ready` — see §4.3 point 6 and CLAUDE.md "Telephony & SIP"). A stuck-unready GPU can no
> longer produce "lead hears raw voice for the whole call" — the equivalent failure now is
> "outbound dial returns 503" or "inbound caller stays on hold music," which is an availability
> problem, not a voice-leak problem. This has not been exercised against a live deployed `/ws`
> worker yet (see the top-of-document banner), so treat "can no longer recur" as a structural
> claim about the code, not yet a field-verified one.

### 4.3 General checklist

If outbound calls are returning 503 ("Voice engine not ready"), or an inbound caller is stuck on
hold music past a normal cold-start window, or a live call is publishing nothing but silence,
work through these in order:

1. **Is `RVC_ENDPOINT_URL` actually set?** Check server startup logs or `GET /api/health` →
   `rvc.endpoint`. If it prints "not configured (using dummy converter)", the bot never talks
   to Modal at all — `.env` is missing the value or the deployed URL changed. (With no
   `RVC_ENDPOINT_URL`, the bot uses `DummyVoiceConverter`, which is always "ready" — the warm
   gate/503 behavior below only applies when an RVC endpoint is configured.)
2. **Is the Modal app deployed?** `modal deploy modal_deploy/worker.py` (locally, or via
   `POST /api/deploy` if `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` are set in the server env —
   check the response for `MODAL_TOKEN_ID or MODAL_TOKEN_SECRET ... are missing` if it fails
   immediately).
3. **Hit `/health` directly** on the deployed endpoint (`RVC_ENDPOINT_URL` with `/convert`
   swapped for `/health`). `{"status": "loading"}` means the container is up but still inside
   `RVCEngine.startup()` (model + HuBERT + FAISS index + warm-up inference) — expect this to
   take up to ~90s on a cold GPU (see §4.1; the live worker is an L4 as of 2026-07-03, plus a
   one-time ~22s TRT engine build/warmup if `USE_TRT=1` and the volume cache is cold). `{"status": "ready"}` means it's fully warm.
4. **Use `POST /api/warmup`, not a single health ping.** It polls `/health` every 30s for up to
   6 minutes specifically to ride out a cold start; a single `curl` with a short timeout will
   very likely time out on a cold container and is not a reliable "Modal is broken" signal.
5. **Check the Modal volume.** `RVCEngine.startup()` raises `RuntimeError("No FAISS index
   found.")` if `/root/rvc-models/logs/mi-test/*.index` is empty on the `rvc-models` volume —
   this looks like a hang/failure from the caller's side but is actually a missing-model error
   visible in the Modal container logs (`modal app logs rvc-worker`).
6. **A cold GPU now blocks the call, by design — it does not degrade to raw voice.** The old
   per-chunk `budget_ms=2000.0`/raw-fallback design is gone entirely (see the banner and §4.2).
   Instead: `_do_start_bot` kicks off a single background readiness probe via
   `worker.start_readiness_probe()`; outbound (`POST /api/call/outbound`) `await`s
   `worker.wait_for_readiness_probe()`, which shares that same background probe (rather than
   opening a second, independent one) and returns HTTP 503 without dialing the lead if the
   converter's backend isn't ready in time; inbound (`POST /api/call/wait`) only bridges to
   LiveKit SIP once `worker.is_ready` is true (the cached result of that same background probe),
   otherwise it keeps returning hold-music TwiML and re-polling. This is the gate working as
   intended — fail-closed, not the old fail-safe raw fallback — a cold/unready GPU produces a
   bounded wait or a clean 503, never a period of the lead hearing the agent's real voice. Note
   `wait_until_ready`/`wait_ready` for
   `RVCStreamingConverter` opens a short-lived **`/ws` probe connection** (not a `/health` HTTP
   check) that expects `{"type":"ready"}` — if the server is already serving a real call (the
   1-concurrent-session MVP), that probe gets `{"type":"busy"}` and is treated as not-ready, so
   "warm gate keeps failing" can also mean "a session is already active," not just "GPU is cold."
   Real conversion resumes automatically once the container reports ready and a session is free.
7. **Confirm GPU visibility inside the container**, not just that the container started: the
   `/health` response includes `cuda_available` and `cuda_device` — if `cuda_available` is
   `false` on the `gpu="L4"` function (was `"T4"` before 2026-07-03), that's a Modal-side
   scheduling/image issue, not an application bug (check `modal app logs rvc-worker` for CUDA
   init errors). Committing a `gpu=` change doesn't deploy it — `modal deploy` must actually run
   before `/health`'s `cuda_device` reflects it; see `.agents/context/subsystem-notes.md`.
8. **Keep it warm proactively.** The `scaledown_window=120` means the container shuts down 2
   minutes after the last request — for back-to-back calls, ping `/api/warmup` at shift start
   and rely on the automatic pre-warm ping fired in `_do_start_bot` on every bot spawn to
   overlap cold start with call setup/ringing rather than debugging a "slow first chunk" as if
   it were an outage.

---

## 5. (Historical, pre-streaming-rebuild) Ordered Playout Queue

> **Superseded 2026-07-02 by the streaming rebuild.** Everything in this section — VAD chunking,
> `_conversion_consumer`, `_run_playout`, the adaptive standing playout buffer, the reorder-wait
> window — was deleted from `backend/pipeline.py`, not just changed. It's kept here as historical
> record of the design that preceded the current persistent-WebSocket streaming pipeline
> (described in §1/§2 above and `.agents/decisions/log.md`). Do not use anything below as current
> behavior.

The original pipeline had a one-shot pre-buffer: hold back the first 1s of converted audio,
release it as a burst, then publish every subsequent chunk directly to LiveKit the moment its
RVC call finished. That masked startup jitter but did nothing for the rest of the call — if RVC
fell behind mid-conversation the lead would hear gaps or reordered audio with no buffer left to
absorb it. `backend/pipeline.py` now separates *producing* converted audio from *playing it out*:

- **`_conversion_consumer`** dispatches VAD-cut chunks to RVC (up to 2 concurrent, unchanged)
  and hands each result — success, fail-safe raw fallback, or an explicit "skipped" marker for
  a chunk too stale to bother with — to `_enqueue_chunk`, keyed by a monotonically increasing
  sequence number. It never touches `capture_frame` directly.
- **`_run_playout`** is the only thing that calls `capture_frame`. It has two phases per speech
  session:
  1. **Filling** — accumulate *contiguous* ready chunks (no gaps) until the adaptive target
     (`_buffer_target_bytes`, §1) is met. This is the "standing buffer": unlike the old
     pre-buffer it refills every session (triggered by `stop_pipeline` flipping
     `_playout_active` back to `False`), not just the first one for the whole call.
  2. **Draining** — publish chunks strictly in sequence. If the next expected chunk isn't ready,
     wait up to 600ms (`_REORDER_WAIT_S`) for it before skipping past it, so one slow RVC call
     can delay by at most 600ms rather than stalling the call indefinitely.
- **Sequence numbers are never reset mid-call.** `stop_pipeline` (fired on `track_unsubscribed`)
  only resets the *buffering phase*, not `_next_publish_seq` or `_pending_chunks` — the dispatch
  counter in `_conversion_consumer` runs for the worker's whole lifetime, so resetting the
  playout side out from under it would make the playout task wait forever for sequence numbers
  that were already consumed.
- **Buffer depth is adaptive** (`_recompute_buffer_target`): each session's target is the P95 of
  the last 20 RVC round trips × 1.2, clamped to 400-1500ms. A call with a consistently fast GPU
  gets a smaller, lower-latency buffer; one with more variance gets a deeper buffer automatically
  rather than a fixed value that's wrong in one direction or the other.
- **Chunking was widened from 250ms to 450ms minimum** (`MIN_CHUNK_MS` in `_conversion_consumer`)
  because the real driver of "queue backed up" in the logs wasn't buffering depth at all — it was
  throughput: VAD was cutting chunks almost every natural breath, and RVC's ~600-750ms fixed
  per-request overhead doesn't amortize over chunks that short. Larger minimum chunks mean fewer,
  cheaper-per-byte RVC round trips for the same amount of speech.

**A known asyncio gotcha this design had to account for:** `asyncio.Condition.notify_all()` wakes
*every* waiter, not just the one whose data actually arrived. `_run_playout`'s wait for a specific
sequence number therefore loops against a wall-clock deadline (`time.monotonic()`) rather than
doing a single `await cv.wait()` — otherwise an unrelated chunk resolving while we're waiting for
a different one would look like our own wait timing out early, silently shrinking the effective
reorder window.
