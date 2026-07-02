# Keira Streaming Voice-Conversion Rebuild

## Context

Keira converts a sales agent's voice to a trained "brand voice" (RVC v2 on a Modal T4) in real time during PSTN calls (browser agent → LiveKit → Twilio SIP → lead). Today the pipeline chops speech into VAD phrases (450–700 ms), sends each as an HTTP POST to Modal, and re-orders responses through an adaptive playout buffer ([backend/pipeline.py](d:/Kiera/backend/pipeline.py)).

**The problem:** the lead sometimes hears the agent's **original** voice. Root causes (all confirmed in code):/
1. The fail-safe in `_convert_chunk` (pipeline.py ~line 431) publishes the **raw denoised voice** whenever RVC misses its 2 s budget — which always happens during Modal cold start (~75 s measured) and under backlog.
2. The pre-RVC stale-skip (pipeline.py ~line 295) also publishes raw audio.
3. `RVCVoiceConverter.convert_stream` ([backend/converters/rvc.py](d:/Kiera/backend/converters/rvc.py) lines 66–91) leaks raw **16 kHz** PCM through the *success* path on tiny chunks (<640 B) or empty responses — played as 48 kHz, i.e. 3× speed.
4. Architecture floor: phrase-wait + ~600–750 ms fixed per-HTTP-request overhead + Oregon↔Singapore round trip ⇒ ~1.5–2.5 s mouth-to-ear at best.

**Decisions already made by the product owner (do not re-litigate):**
- Full rebuild to a **streaming architecture** (no interim patches to the old chunked path).
- **Never publish raw voice. Ever.** On any conversion failure: hold/wait (brief silence), never fall back to the original voice.
- **Fail-closed warm gate:** a call must not be bridged/dialed until the GPU session is verified ready.
- **Region: Singapore.** GPU stays `ap-southeast`; the Render backend migrates to Singapore.
- **Capacity: 1 concurrent call (MVP).** One streaming session owns the GPU.
- **Idle policy:** keep Modal `scaledown_window=120` (cheap); accept a ~75 s gate wait after idle gaps. `/api/warmup` at shift start covers the first call.
- Target: **~400–550 ms mouth-to-ear**, converted from the first word.

## Target architecture

```
agent mic ──LiveKit──► pipeline.py (denoise 10ms frames)
        ──20ms PCM frames over ONE persistent WebSocket──► Modal worker
Modal worker: accumulate 320ms blocks (+160ms left context)
              → RVC inference (pm pitch method) → SOLA crossfade
        ──converted 48kHz PCM frames──► pipeline.py
        → ~100ms jitter buffer → AudioSource.capture_frame → lead
```

No VAD phrase-chunking, no per-request HTTP, no reorder machinery, no raw-fallback path (it ceases to exist structurally).

### Parameters (constants; name them clearly, they will be tuned)

| Param | Value | Where |
|---|---|---|
| Input frame to WS | 20 ms (640 B @16 kHz int16 mono) | pipeline |
| Inference block | 320 ms new audio | worker |
| Left context | 160 ms of prior input prepended | worker |
| SOLA crossfade | 80 ms @48 kHz (3840 samples) | worker |
| SOLA search window | 10 ms @48 kHz (480 samples) | worker |
| Jitter buffer before first playout | 100 ms | pipeline |
| Outage hold cap (input buffered during reconnect) | 5 s, drop oldest beyond | converter |
| WS reconnect backoff | 0.5 s → 5 s exponential | converter |
| Warm-gate timeout (outbound) | 150 s (cold start is ~75 s; headroom) | main.py |
| Silence bypass threshold | block RMS < 150 (int16) ⇒ skip GPU, emit silence | worker |

---

## Task 1 — Modal worker: WebSocket streaming endpoint (`modal_deploy/worker.py`)

Keep the existing `/health` and `/convert` HTTP endpoints (used by warmup and offline tests). Add `/ws` to the same FastAPI ASGI app (Modal supports WebSockets in `@modal.asgi_app()`).

### Protocol (client = Keira backend)
- Client connects, first message is text JSON:
  `{"type":"config","pitch_shift":12,"index_rate":0.75,"rms_mix_rate":0.75,"protect":0.33}`
  (`pitch_shift: -1` = auto-detect from the first ~1 s of non-silent audio using the existing `_auto_detect_pitch`, then fixed for the session.)
- Server replies `{"type":"ready"}` **only when** `engine.ready` is true and no other session is active. This reply is the warm-gate handshake. If another session is active: send `{"type":"busy"}` and close (MVP = 1 session; enforce with a module-level flag/lock).
- Client → server binary messages: raw 16 kHz mono int16 PCM (any size; backend sends 20 ms).
- Server → client binary messages: converted 48 kHz mono int16 PCM, emitted per processed block.
- Text `{"type":"ping"}` ⇒ `{"type":"pong"}` (keepalive).
- Server may send `{"type":"stats","infer_ms":<float>,"block_ms":320}` after each block (backend forwards to the UI latency badge).
- On client disconnect: clean up session state, release the busy flag.

### Streaming engine (per-session state inside the WS handler)
1. Append incoming PCM to an input buffer. Whenever ≥ 320 ms of *new* audio: form `context(160 ms of previous input) + block(320 ms)`, run inference, then:
   - **Trim context from output proportionally** (output length ≠ exactly 3× input; same insight as the proportional slice in current pipeline.py lines 419–430): `slice_len = round(len(out) * ctx_len/total_in)`, force even.
   - **SOLA align + crossfade:** hold back the last 3840 samples of the previous emitted output as a "pending tail". In the new block's first `3840 + 480` samples, find offset `k ∈ [0, 480)` maximizing normalized cross-correlation with the pending tail; overlap-add tail and `out[k : k+3840]` with a Hann (or linear) ramp; emit `crossfaded + out[k+3840 : -3840]`, keep the new last 3840 samples as the next pending tail.
   - Run inference via `asyncio.to_thread`, serialized by an `asyncio.Lock` (GPU is single-tenant).
2. **Silence bypass:** if the new block's RMS < threshold, skip the GPU and emit an equal duration of silence at 48 kHz (3× samples). Prevents RVC artifacts on silence and saves GPU time.
3. Inference input: reuse `engine.run_conversion` (tmpfs file write is ~0.1 ms, acceptable per existing timing logs). Optionally refactor to accept an `np.ndarray` directly — nice-to-have, not required.
4. If inference raises: send `{"type":"error","message":...}`, emit nothing (never raw), keep the session alive.

**Fallback if SOLA proves hard:** proportional context-trim (already proven in the current code) + a simple 10 ms linear crossfade between consecutive block outputs. Ship that first if needed; SOLA is the quality upgrade.

Region stays `region="ap-southeast"` (Render is moving there — Task 6). Update the stale comment.

## Task 2 — Streaming converter client (`backend/converters/rvc_stream.py`, new file)

`RVCStreamingConverter(VoiceConverter)` using the `websockets` library (add `websockets>=12` to `backend/requirements.txt`). Derive the WS URL from `RVC_ENDPOINT_URL`: `https→wss`, path `/convert`→`/ws` (also handle `web-convert`/`web_convert` name variants like `_wait_for_rvc_ready` in main.py does). Allow explicit override via new optional env `RVC_WS_URL`.

- `async def wait_ready(timeout: float) -> bool`: connect, send config, await `{"type":"ready"}`. Used by the warm gate.
- `convert_stream(in_audio) -> AsyncIterator[bytes]` (keeps the existing `VoiceConverter` ABC, but now **long-lived duplex**): a sender task pumps `in_audio` frames to the WS; the generator yields each received binary message. Forward `stats` messages via an optional callback attribute (`on_stats`) so the pipeline can publish latency metrics.
- **Hold-don't-leak reconnect:** on WS drop mid-call, buffer incoming frames in a bounded deque (5 s cap, drop oldest with a log), reconnect with 0.5→5 s backoff, re-send config, await ready, flush the buffer. The output generator simply pauses during the outage (lead hears silence — acceptable per decision; raw voice is not).
- `close()`: send a close frame, cancel tasks.

## Task 3 — Pipeline rewrite (`backend/pipeline.py`)

Keep unchanged: room connect/subscribe handlers (the `"agent"`-identity subscription logic, lines 68–146), the denoise producer (`_run_audio_pipeline`), publishing a 48 kHz track, and the data-channel latency metric (now sourced from converter `stats`; reuse the existing JSON shape `{"pipeline_latency_ms":…, "is_fallback":…}` — set `is_fallback=true` only while holding/reconnecting so the existing frontend badge doubles as a "HOLDING" indicator, [frontend/app.js](d:/Kiera/frontend/app.js) lines 376–410 need no change).

Replace the consumer machinery entirely:
- **Delete:** VAD chunking, the RVC semaphore, `convert_and_enqueue`, `_enqueue_chunk`, `_pending_chunks`/`_next_publish_seq`/`_playout_cv`, `_recompute_buffer_target`, `_REORDER_WAIT_S`, `_convert_chunk`, `_resample_16k_to_48k` raw-fallback usage, `budget_ms`. Frames arrive in order on one TCP stream — ordering is free.
- **New consumer:** aggregate two 10 ms denoised frames → 20 ms → an async generator feeding `converter.convert_stream`. A receiver task takes yielded 48 kHz bytes → jitter buffer: hold until 100 ms accumulated at stream start (once), then `_publish_frames` continuously (`capture_frame` paces playback; keep `queue_size_ms=400` or lower to 200).
- Add `async def wait_until_ready(timeout) -> bool` on `VoiceConversionWorker`: delegates to `converter.wait_ready` if it exists (RVC streaming), else returns True immediately (dummy).
- Input shedding: if the input queue exceeds the 5 s hold cap during an outage, the converter's bounded deque already drops oldest — the pipeline itself needs no shed loop.

**`backend/converters/dummy.py`:** update `DummyVoiceConverter` to output 48 kHz (after ring-mod, `np.repeat(samples, 3)`) so it satisfies the new pipeline contract (pipeline no longer resamples anything). Give it a no-op `wait_ready`. Local no-GPU testing must keep working.

**`backend/converters/rvc.py`:** keep the file (offline tests use it) but it is no longer selected by the bot. While touching it, fix the raw-leak: on `<640 B` input or empty response, yield **nothing** instead of raw PCM.

## Task 4 — Fail-closed warm gate (`backend/main.py`)

- `_do_start_bot`: select `RVCStreamingConverter` when `RVC_ENDPOINT_URL` is set (else Dummy). Keep the gender→pitch mapping (`male→12`, else `0`). Remove the `budget_ms`/`chunk_ms` converter-type switch (obsolete).
- **Outbound (`/api/call/outbound`):** replace the current fail-open `_wait_for_rvc_ready(90 s)` with fail-closed: after spawning the bot, `ok = await worker.wait_until_ready(150.0)`. If not ready: stop the bot, `raise HTTPException(503, "Voice engine not ready — GPU still warming. Try again shortly.")` and **do not dial**. (The frontend already surfaces `detail` in an alert — app.js lines 285–295.)
- **Inbound (`/api/call/wait`):** bridge to LiveKit SIP only when `status == "accepted"` **and** `active_workers[room].converter is ready` (expose a `worker.is_ready` property). Otherwise keep returning the hold TwiML. Caller hears hold music during warmup; the existing loop already re-polls.
- Keep `/api/warmup` (shift-start warm; frontend "Start Shift" button uses it) and `_wait_for_rvc_ready` (it polls `/health`, still valid).

## Task 5 — Tests (`backend/test_pipeline.py` + new)

- **`RVCStreamingConverter` against a fake WS server** (in-process `websockets.serve`): fake replies `ready`, echoes each binary frame ×3-upsampled. Assert: handshake, ordered output, `close()` cleanliness. **Reconnect test:** kill the fake server mid-stream, restart it, assert the converter reconnects, replays ≤ 5 s of buffered input, and never yields anything during the outage.
- **SOLA unit test:** factor SOLA/crossfade into a pure function in `modal_deploy/worker.py` (importable without GPU/Modal — guard Modal imports). Feed two overlapping sine blocks; assert no sample-to-sample discontinuity above a threshold at the seam.
- Update `test_rvc_converter_mocked` for the rvc.py leak fix (empty response ⇒ no yield).
- Keep the runner style (`python -m backend.test_pipeline`); tests must pass on Windows where `webrtc-noise-gain`/`webrtcvad` are absent (existing graceful-degradation pattern).

## Task 6 — Deployment & migration

1. `modal deploy modal_deploy/worker.py` (from repo root; needs `Retrieval-based-Voice-Conversion-WebUI/` present and the `rvc-models` volume populated — see `RVCEngine.startup`; a missing FAISS index raises `RuntimeError("No FAISS index found.")` visible only in `modal app logs rvc-worker`).
2. Smoke-test `/ws` with a local script (Task 5's client pointed at the real endpoint): stream a WAV in 20 ms frames, write output WAV, listen for boundary artifacts; confirm real-time factor < 1.
3. **Render → Singapore (manual, dashboard):** Render cannot change a service's region in place — create a new web service in Singapore from the same repo, copy env vars, then update `SERVER_URL` and run `POST /api/setup` to repoint the Twilio inbound webhook and recreate trunks. Decommission the Oregon service after a successful test call.
4. Gotcha (from `.agents/context/subsystem-notes.md`): Render `autoDeploy: commit` redeploys on every push to `main` and kills in-flight calls — don't push during a live test call.

## Task 7 — Documentation updates

Update to match the new architecture: `CLAUDE.md` (pipeline/chunking sections), `.agents/context/stack-and-rules.md`, `.agents/context/subsystem-notes.md` (new WS/SOLA gotchas; remove obsolete reorder-window notes), `.agents/decisions/log.md` (new entry: streaming rebuild, never-raw policy, fail-closed gate, Singapore), and `LATENCY.md` (re-measure with the method already described there).

## Out of scope (explicitly)

Multi-call concurrency, `min_containers` keep-warm, auth/security hardening of the API endpoints (tracked separately), frontend redesign.

## Verification (end-to-end)

1. `python -m backend.test_pipeline` — all pass (Windows-tolerant).
2. Local no-GPU: `uvicorn backend.main:app --port 8000` without `RVC_ENDPOINT_URL` → dummy converter; browser test call flow works; converted (ring-mod) audio at 48 kHz, no crashes.
3. Worker: deploy, run the WS smoke script → listen to output; `infer_ms` per 320 ms block < ~250 ms warm.
4. Live call (the real test): warm via "Start Shift", place an outbound call to a test number and verify **(a)** the very first audible word is in the trained voice, **(b)** listening for the whole call: the original voice is never heard, **(c)** mouth-to-ear latency ≈ 400–600 ms (LATENCY.md method), **(d)** mid-call `modal app stop rvc-worker` → lead hears a brief hold (silence), never raw voice; after redeploy/warm the stream resumes.
5. Gate: with a cold GPU, outbound returns 503 until warm (frontend alert), inbound caller stays on hold music; call bridges only after ready.
