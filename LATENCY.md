# Latency Measurement Analysis & Guide

This document presents the latency profile for the real-time voice conversion pipeline
([backend/pipeline.py](backend/pipeline.py)), a detailed latency budget breakdown for the
current RVC v2 / Modal GPU engine, guidelines for running latency verification, and
troubleshooting steps for Modal GPU connection/cold-start issues.

> **2026-07-02 update:** Pulled live Render logs (`Kiera`, `srv-d92lh7navr4c738i03a0`) and
> pinged the Modal endpoint directly while diagnosing a "Modal not connecting / GPU not
> starting" report — see §4 for the confirmed root causes (redeploys killing in-flight calls,
> and cold-start taking much longer than assumed).

---

## 1. Latency Budget Breakdown

In a real-time voice call, total mouth-to-ear latency is composed of multiple steps in the
media transport and conversion pipeline:

$$\text{Mouth-to-Ear Latency} = T_{\text{ingress}} + T_{\text{noise\_suppression}} + T_{\text{VAD\_chunking}} + T_{\text{RVC\_conversion}} + T_{\text{egress}}$$

| Pipeline Step | Duration | Description |
| :--- | :--- | :--- |
| **Ingress Network** | 20 - 45 ms | Browser capture, OPUS encoding, WebRTC transport to LiveKit, and forwarding to the Python worker at 16 kHz mono. |
| **Noise Suppression** | 0.2 - 0.5 ms | `WebRTCNoiseSuppressor` processing of 10ms/320-byte frames. Sub-millisecond on CPU. |
| **VAD Chunk Collection** | 250 - 700 ms | `_conversion_consumer` collects frames until a natural pause (150ms of silence, `webrtcvad`) or the 700ms hard cap, whichever comes first. Minimum 250ms so RVC has enough context for pitch extraction. A 100ms carry-over is prepended to the next chunk to smooth boundaries. |
| **RVC Conversion (warm GPU)** | ~150 - 700 ms | HTTP round trip to the Modal T4 endpoint (`RVC_ENDPOINT_URL`) — see [modal_deploy/worker.py](modal_deploy/worker.py). `pm` pitch extraction (not `rmvpe`) saves ~300ms/chunk. Bounded by a 2000ms budget (`asyncio.timeout`); on timeout it fails over to the raw denoised chunk resampled 16→48kHz instead of dropping audio. |
| **RVC Conversion (cold GPU)** | 30 - 90+ s (first chunk only) | Modal spins up a new T4 container, loads the RVC model + HuBERT + index (`RVCEngine.startup`), and runs one silent warm-up inference before serving real requests. This only happens when no container has served a request in the last 120s (`scaledown_window=120`). **Measured live on 2026-07-02**: a `/health` ping against an idle container got no response at all for ~75s before the container came up as `{"status":"ready","cuda_available":true,"cuda_device":"Tesla T4"}` — noticeably longer than the ~8-30s the code comments assume. The 2000ms conversion budget means a cold GPU **always** fails over to raw voice for the first several chunks — see §4. |
| **One-time Pre-buffer** | 1000 ms (once per speech session) | `VoiceConversionWorker._publish_audio` withholds the first 1s of converted audio and releases it as a burst, to hide RVC/GPU jitter. Paid once per call, not per chunk. |
| **Egress Network** | 20 - 40 ms | Publishing 960-byte (10ms @ 48kHz) frames to LiveKit, then WebRTC + browser jitter-buffer playout to the lead. |
| **Steady-state Total (warm GPU)** | **~450 - 1400 ms** | Per-chunk added latency once the GPU is warm and the pre-buffer has already been paid. |
| **First-chunk Total (cold GPU)** | **30 - 90+ s of raw voice, then fail-over recovers** | The lead hears the agent's real (unconverted) voice for the first several chunks while the GPU boots; conversion kicks in once the container is warm. |

Region note: the Modal function is pinned to `region="ap-southeast"` (Singapore), intended to
sit next to Render/Twilio infrastructure and avoid a transpacific round trip on every chunk —
but see §4.1, the currently deployed Render service is actually in Oregon, so this pin may be
working against itself right now.

---

## 2. Reading Live Latency Logs

Unlike a fixed pipeline, per-chunk latency varies with GPU warm/cold state and chunk length,
so treat the numbers below as **how to read the logs**, not as fixed benchmarks. Run your own
pass (§3) after any pipeline or model change and capture fresh numbers.

Worker-side log lines (`backend/pipeline.py`) to watch during a call:

```text
[Worker] VAD pipeline active. min=250ms silence_cut=150ms max=700ms carry-over=100ms max_age=1.0s max_concurrent_rvc=2
[Worker] Dispatching chunk 0: 480ms (8320 bytes incl. carry-over)
[RVC Client] Server processing time: 612.4 ms
[Latency] 891ms total (conversion: 640ms)
[Worker] Pre-buffering... 0.62s / 1.0s
[Worker] Pre-buffer full — starting smooth playback to lead.
```

- `[Latency] {X}ms total (conversion: {Y}ms)` — `X` is the full added latency for that chunk
  (from first frame captured to publish), `Y` is just the RVC HTTP round trip. `X - Y` is
  chunk-collection + noise-suppression + queueing overhead.
- `[Worker] Fail-safe fallback (latency: {X}ms)` — the RVC call missed its 2000ms budget (or
  errored) and the raw denoised chunk was forwarded instead. Frequent fail-safes usually mean
  the GPU is cold or overloaded — see §4.
- `[Worker] Queue backed up: skipping RVC for stale chunk N` — the input queue is more than
  1s behind; that chunk skips RVC entirely to keep the call from drifting further behind.
- The frontend also receives a `pipeline_latency_ms` / `is_fallback` data-channel message per
  chunk (see `_convert_chunk`), which the agent dashboard can surface live.

---

## 3. How to Run the Mouth-to-Ear Latency Test

The PoC includes a built-in, automated spectral tone latency analyzer that bypasses acoustic
noise and measures delay digitally in the browser.

### Automatic Spectral Test (Recommended)
1. Set up and run the server (see [README.md](README.md)). Confirm `RVC_ENDPOINT_URL` is set
   and `GET /api/health` reports it — otherwise the bot silently falls back to
   `DummyVoiceConverter` and you'll measure the wrong pipeline.
2. Hit `POST /api/warmup` (or wait for the automatic pre-warm ping fired on bot start) so the
   Modal T4 is warm **before** you measure — see §4. A cold-start run will only show you the
   fail-safe path, not real RVC latency.
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
- **Region mismatch worth double-checking:** the Modal worker is pinned to
  `region="ap-southeast"` (Singapore, per the comment in
  [modal_deploy/worker.py](modal_deploy/worker.py)) on the premise that Render/Twilio also run
  in that region — but the deployed Render service is currently in **Oregon** (`us-west`), not
  Singapore. If that's still the case, every RVC call pays a transpacific round trip on top of
  inference time, eating into the 2000ms budget and making timeouts (→ fail-safe fallback) more
  likely under load. Either move the Render service closer to `ap-southeast`, or re-pin the
  Modal function to a US region, so the two aren't fighting each other.

### 4.2 General checklist

If the bot is always falling back to raw voice (no conversion applied), work through these in
order:

1. **Is `RVC_ENDPOINT_URL` actually set?** Check server startup logs or `GET /api/health` →
   `rvc.endpoint`. If it prints "not configured (using dummy converter)", the bot never talks
   to Modal at all — `.env` is missing the value or the deployed URL changed.
2. **Is the Modal app deployed?** `modal deploy modal_deploy/worker.py` (locally, or via
   `POST /api/deploy` if `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` are set in the server env —
   check the response for `MODAL_TOKEN_ID or MODAL_TOKEN_SECRET ... are missing` if it fails
   immediately).
3. **Hit `/health` directly** on the deployed endpoint (`RVC_ENDPOINT_URL` with `/convert`
   swapped for `/health`). `{"status": "loading"}` means the container is up but still inside
   `RVCEngine.startup()` (model + HuBERT + FAISS index + warm-up inference) — expect this to
   take up to ~90s on a cold T4 (see §4.1). `{"status": "ready"}` means it's fully warm.
4. **Use `POST /api/warmup`, not a single health ping.** It polls `/health` every 30s for up to
   6 minutes specifically to ride out a cold start; a single `curl` with a short timeout will
   very likely time out on a cold container and is not a reliable "Modal is broken" signal.
5. **Check the Modal volume.** `RVCEngine.startup()` raises `RuntimeError("No FAISS index
   found.")` if `/root/rvc-models/logs/mi-test/*.index` is empty on the `rvc-models` volume —
   this looks like a hang/failure from the caller's side but is actually a missing-model error
   visible in the Modal container logs (`modal app logs rvc-worker`).
6. **Budget is too tight for a cold start, by design.** `budget_ms=2000.0` in `_do_start_bot`
   means the pipeline will *always* fail over to raw voice while a cold T4 boots (30-90+s) —
   this is not a bug, it's the fail-safe working as intended so the lead isn't left in silence.
   Real conversion resumes automatically once the container reports `ready`.
7. **Confirm GPU visibility inside the container**, not just that the container started: the
   `/health` response includes `cuda_available` and `cuda_device` — if `cuda_available` is
   `false` on a `gpu="T4"` function, that's a Modal-side scheduling/image issue, not an
   application bug (check `modal app logs rvc-worker` for CUDA init errors).
8. **Keep it warm proactively.** The `scaledown_window=120` means the container shuts down 2
   minutes after the last request — for back-to-back calls, ping `/api/warmup` at shift start
   and rely on the automatic pre-warm ping fired in `_do_start_bot` on every bot spawn to
   overlap cold start with call setup/ringing rather than debugging a "slow first chunk" as if
   it were an outage.
