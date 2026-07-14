# Subsystem Notes & Load-Bearing Gotchas

<!-- One section per subsystem. Capture the WHY and the traps that are not obvious
     from reading the code — this is what the wiki/codebase cannot tell you. -->

## Streaming pipeline (`backend/pipeline.py`, streaming rebuild 2026-07-02, playout buffer reintroduced 2026-07-03)
The old VAD-chunked, semaphore'd, reorder-buffer design (`_conversion_consumer`,
`_run_playout`) was removed entirely in the 2026-07-02 rebuild, not just tuned —
`webrtcvad` is no longer imported anywhere in this file. The converter is still driven as
one long-lived duplex stream for the life of the worker's active pipeline
(`_run_conversion_stream`); frames arrive back **in order**, so there's nothing to reorder.
**However**, a standing playout buffer was reintroduced on 2026-07-03 (see the new
"Playout buffer" subsection below) — don't assume "no chunking/no buffering anywhere" from
the 2026-07-02 framing above; that only ever applied to the *input* side (no VAD chunking
of the agent's mic audio) and briefly to the *output* side too, until the 2026-07-03 product
decision that latency isn't a priority reversed that specific piece. Load-bearing gotchas in
the duplex-stream design itself (still current):
- **`contextlib.aclosing(gen)` around the conversion generator is not optional.**
  `_run_conversion_stream` wraps `converter.convert_stream(...)` in
  `async with contextlib.aclosing(gen):` specifically because a bare `async for chunk in gen`
  that exits via `break`, an exception, or task cancellation does **not** reliably call the
  generator's `aclose()` — and for `RVCStreamingConverter`, `aclose()` is what tears down the
  pump/connection background tasks and the WS socket (see `convert_stream`'s `finally:
  await self._teardown()` in `backend/converters/rvc_stream.py`). Skip `aclosing` and a
  cancelled/abandoned stream can leak a live WS connection and background tasks per call.
  This was a real cross-task requirement surfaced during the rebuild, not a hypothetical.
- **`is_ready` is a cached property backed by a one-shot background probe, deliberately not a
  live re-probe.** `VoiceConversionWorker.start_readiness_probe()` kicks off exactly one
  background task per worker that calls `wait_until_ready()` once and caches the result in
  `self._ready`; the `is_ready` property just reads that cached bool. This matters because
  Twilio polls `/api/call/wait` roughly every 3 seconds while a caller is on hold — if
  `is_ready` opened a fresh probe connection to the converter's backend on every poll instead
  of reading a cache, that's a new WS handshake every 3s for the whole hold duration, and
  worse, `RVCStreamingConverter.wait_ready()` is a *separate* short-lived probe connection
  from the long-lived session, so a synchronous re-probe would also contend with (or be
  confused for) the real per-call session under the 1-concurrent-session MVP limit.
- **SOLA crossfade seam-testing trap** (`modal_deploy/streaming.py::sola_crossfade`, found
  during Task 5 review): the Hann fade-in ramp's `fade_in[0] == 0` means the very first sample
  of any crossfaded seam is structurally continuous (equal to the tail's own last sample)
  **regardless of which offset the correlation search picks** — a naive test that only checks
  "the seam has no big jump" will pass even with a broken correlation search that always picks
  offset 0. `modal_deploy/test_streaming.py` instead checks full-block reconstruction accuracy
  against a known-continuous source, which actually exercises the search. Keep this in mind
  before writing a new SOLA-adjacent test — boundary continuity alone proves nothing about
  whether the alignment search works.

### Playout buffer (`backend/pipeline.py`, reintroduced 2026-07-03)
2026-07-03 product decision: call **latency is explicitly not a priority** for this app —
voice **continuity/quality** is. This directly reverses part of the 2026-07-02 rebuild's
implicit latency-first design, so don't assume the newer decision is a regression of the
older one; it's a deliberate tradeoff change, see [[log]]. Same decision also drove
`modal_deploy/streaming.py`'s `BLOCK_MS`/`CONTEXT_MS` going from 320/160 to 1000/400 — bigger
inference blocks give HuBERT/pitch tracking more context and cut the SOLA crossfade seam
rate (fixed 80ms crossfade per block, so ~3x fewer seams per second of audio at the new
size), at the cost of more per-block delay, which this buffer is what absorbs.
- `_run_conversion_stream`'s old one-shot 100ms jitter fill (`_JITTER_TARGET_BYTES`) only
  smoothed the *start* of a call — once drained, any converter block slower than its
  real-time budget produced a silence gap, because LiveKit's own `AudioSource` queue
  (`queue_size_ms=200`) is far too small to cover it. Confirmed in production: ~460-590ms to
  convert a 320ms block even after the FAISS caching fix below (a ~1.5-1.8x real-time
  factor), which reliably starved the old design.
- Replaced with a producer/consumer split: `_run_conversion_stream` only appends converted
  audio to `self._playout_buffer` (bounded, `_PLAYOUT_BUFFER_TARGET_BYTES` **0.25s as of
  commit `b38070c`** — "revert(latency): restore 320ms block geometry, 80ms SOLA, 0.25s
  cushion", the state as of 2026-07-13/14; went 3s→1.25s (TRT phase 1)→0.25s (TRT phase 2,
  2026-07-07) and has been reverted/re-landed at least once since — always verify the live
  constant in `backend/pipeline.py` rather than trusting a remembered number, this file has
  been wrong before) / `_PLAYOUT_BUFFER_MAX_BYTES` ~5s cap, drop-oldest on overflow — same
  policy as `RVCStreamingConverter`'s reconnect buffer, `backend/converters/rvc_stream.py`,
  `_MAX_BUFFER_BYTES`). A separate `_run_playout_consumer` task drains it into
  `_publish_frames` at a steady pace, decoupled from how bursty/delayed the converter's
  arrival timing actually is. A slow block now grows delay instead of producing "part by
  part" audio.
- **Open finding (2026-07-14, unconfirmed as audible, not yet fixed): the consumer's "steady
  pace" isn't actually steady-draining — it gulps the ENTIRE current buffer in one
  `_publish_frames` call per wake, then relies on `capture_frame`'s backpressure (LiveKit's
  AudioSource queue, ~200ms deep) to pace the gulp back out in real time.** Live call
  telemetry (`[Worker][LatencySummary]` block-level `playout_buffer_bytes`, a 2026-07-14
  22:23 IST call) showed this oscillating far past the 0.25s (24000-byte) target — growing to
  121-182KB (1.3-1.9s) over 5-6 blocks, then dropping back near 0 — **three times within one
  single unbroken 7-second voiced utterance**, with GPU inference itself fine throughout
  (56-64ms/block). Hypothesis: because LiveKit's queue has room, the first ~200ms of a large
  gulp gets accepted by `capture_frame` faster than real-time before pacing kicks in, i.e. a
  small burst of time-compressed audio every cycle — while the python-side buffer quietly
  refills underneath for the next gulp. User-reported symptom the same night: "if I talk a
  big sentence the voice is getting blurred at the last" — plausible match (progressive
  distortion over a long continuous utterance, not a hard dropout) but NOT yet confirmed by
  ear against this specific mechanism. Fix direction not yet chosen: either drain the buffer
  in small steady increments instead of one gulp, or re-tune target/consumer pacing. See
  [[active-backlog]].
- **`rtc.AudioFrame.data` is an int16-typed `memoryview` — `len()` on it returns *sample*
  count, not byte count.** (`len(frame.data)` on a 960-byte/480-sample frame returns 480,
  not 960.) Wrap it as `bytes(frame.data)` first if you need the real byte length — this is
  exactly what `_run_audio_pipeline` already does before handing frames to the noise
  suppressor. Tripped up a first draft of the playout-buffer tests in
  `backend/test_pipeline.py` (a byte-count assertion silently checked half the real total).
- **Presence EQ sits between the converter and the playout buffer (added 2026-07-07).**
  `_run_conversion_stream` passes every converted chunk through
  `backend/audio_eq.py::PresenceEQ` (+4dB, 1.2–3.4kHz, `PRESENCE_EQ_GAIN_DB` env, 0
  disables) before `self._playout_buffer.extend(...)` — so if you're comparing the
  published spectrum against raw Modal `/ws` output and see a ~4dB midrange bump, that's
  this, not the converter. It's a numpy-only linear-phase FIR whose (taps−1)-sample input
  tail makes chunked processing byte-identical to single-pass; when writing tests that
  drive `_run_conversion_stream` with a fake converter, remember the generator must stay
  alive (e.g. trailing `await asyncio.sleep(...)`) or the `finally:` block cancels the
  playout consumer before anything is published.

## Modal RVC GPU worker (`modal_deploy/worker.py`)
- Cold start is much slower than the code comments assume: measured live at ~75s with
  no `/health` response at all before `{"status":"ready"}` (see LATENCY.md §4.1), not the
  8-30s originally assumed. This is still true post-rebuild (same Modal worker, same
  cold-start problem) — the difference now is what a cold/unready GPU *does* to a call: the
  old per-chunk 2000ms conversion budget/raw-fallback machinery in `_do_start_bot` is gone
  entirely; instead the caller (`backend/main.py`) blocks the whole call behind the
  fail-closed warm gate (`worker.wait_until_ready` / `is_ready`, see "Streaming pipeline"
  above and [[stack-and-rules]]) until the GPU is actually ready.
- **1-concurrent-session MVP**: `/ws` enforces single-tenancy with module-level
  `_session_active`/`_session_lock` state (`modal_deploy/worker.py`) — a second WS connection
  gets `{"type":"busy"}` and is closed immediately, no queueing. This is a deliberate scope
  limit for this rebuild (multi-call concurrency is explicitly out of scope), not a bug to
  silently "fix" by adding a queue.
- **Two distinct kinds of readiness connection** — don't conflate them: `wait_ready(timeout)`
  (`RVCStreamingConverter`, `backend/converters/rvc_stream.py`) opens a short-lived standalone
  probe connection purely to confirm the server can hand back `{"type":"ready"}`, then closes;
  the actual call audio flows over a separate, later, long-lived `/ws` connection opened by
  `convert_stream`'s `_connection_loop`. Because the server is single-tenant, a probe and a
  real session can't both be open at once — `wait_ready` is meant to complete and close before
  the real session connects.
- `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's `logs/mi-test/*.index` is empty — looks like a hang from the caller's side but
  is a missing-model error, only visible in `modal app logs rvc-worker`.
- **Region mismatch: RESOLVED as of 2026-07-03 (was open 2026-07-02).** The Modal function
  is pinned `region="ap-southeast"`; the Render service is now confirmed live in
  **Singapore** (`srv-d932m4cvikkc73belt1g`, verified via Render API), colocated with Modal.
  The old "Render is in Oregon" claim was stale — the Render service ID also changed
  (from `srv-d92lh7navr4c738i03a0`), consistent with a migration having happened. Don't
  re-open this in [[active-backlog]] without re-checking Render's current region first.
- **FAISS index re-read from disk on every conversion call — fixed 2026-07-03.**
  `RVC/infer/modules/vc/pipeline.py` (vendored, not Keira's own code) calls
  `faiss.read_index(file_index)` then `index.reconstruct_n(0, index.ntotal)`
  **unconditionally on every `vc_single()`/`pipeline()` call** — fine for the original
  WebUI's one-shot-per-file use case, catastrophic for streaming, where it was running once
  per ~480ms audio block. Confirmed in production `[Timing]` logs: ~1.4-2.0s of the ~3.0-3.8s
  total per-block conversion time was this alone (a 221MB index file). Fixed *without*
  touching the vendored file: `worker.py` monkeypatches `faiss.read_index` at container
  startup with an `lru_cache`-backed wrapper that also caches the `reconstruct_n` result
  (overrides the returned index object's `reconstruct_n` method to return the precomputed
  array). The existing GPU warm-up pass in `RVCEngine.startup()` naturally primes the cache
  before any real caller connects. Post-fix: `npy` time dropped to ~0.05s, total
  `run_conversion` to ~0.46-0.59s per block. If you ever see the old ~1.4-2.0s `npy` number
  again, suspect the monkeypatch didn't survive a `worker.py` refactor, not a new bottleneck.
- **`max_containers` was never set on `fastapi_app` — fixed 2026-07-03.** The in-process
  `_session_active`/`_session_lock` single-tenancy gate (above) only enforces "1 session"
  *inside a single already-running container* — it does nothing to stop Modal's autoscaler
  from booting an additional (paid) GPU container for a connection attempt that arrives
  while an existing container is still cold-starting (~75s) or mid-call. Confirmed live: 4
  simultaneous `rvc-worker` containers were running at once in the Modal dashboard, each a
  full GPU replica (~1.7-1.8GB loaded model each) — traced to WS reconnects/retries during
  active test-calling landing on different containers rather than queuing against one. Fixed
  by adding `max_containers=1` to the `@app.function(...)` decorator on `fastapi_app`
  (`worker.py`) so extra connection attempts queue instead of spinning up parallel GPUs.
- **Gender/pitch auto-detection is unreliable — reverted 2026-07-03.** `_auto_detect_pitch`'s
  autocorrelation F0 estimate (male/female boundary at 145Hz) was intended to replace the
  manual UI gender toggle (`a0f3c42`, "more accurate" at the time) but confirmed misdetecting
  a known-male agent as female **twice** in production logs the same day (F0=222Hz/166Hz,
  both classified Female, pitch_shift=0 applied instead of the correct +12) — feeding audio
  outside the trained model's pitch range produces a "wrong identity" sounding voice, not
  just a pitch error. It's also re-run from scratch on every WS reconnect (the detected
  pitch is never reported back to/persisted by the client), so a single call could even
  change identity mid-call. `backend/main.py`'s `_do_start_bot` now drives `pitch_shift`
  from the UI's `agentGender` toggle again (`12 if male else 0`) instead of `-1` (GPU
  auto-detect). The GPU-side `_auto_detect_pitch` code itself is untouched/still selectable
  via `pitch=-1` — just no longer what the live call path uses.
- **Adaptive per-call pitch lock (added 2026-07-13, spec
  `docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md`).** The fixed
  `RVC_MALE_PITCH_SHIFT` went stale when the agent's live F0 moved (152-158Hz on
  2026-07-13 vs the 137-138Hz that +7 was calibrated on → output 1.5-2 st above the
  model center = wrong identity). `ws_stream` now owns a `PitchLock`
  (`modal_deploy/pitch_lock.py`): engines expose their PRE-shift F0 track via the
  `f0_sink` kwarg on `convert_block`, the session accumulates voiced frames
  (60-400Hz window) and, at ≥2s voiced, locks `12·log2(target_f0/median)` (float,
  clamped ±12) for the rest of the session. The locked value rides the existing
  `stats` messages as `locked_pitch`; `RVCStreamingConverter` adopts it and flips
  its own `adaptive_pitch` off, so a WS **reconnect resumes the locked identity**
  — the exact failure that got `_auto_detect_pitch` reverted on 2026-07-03 cannot
  recur (that legacy path still exists behind `pitch_shift == -1`, unused live).
  Kill switch: `RVC_ADAPTIVE_PITCH=0` on Render (backend env; default on). Offline
  replay: `modal run modal_deploy/worker.py::main_chunked --pitch 7 --adaptive 1`.
  Trap: `pitch_lock.py` must stay numpy+stdlib-only (it imports into the container
  via `add_local_python_source("pitch_lock")` in `modal_defs.py` — same mount rule
  as streaming.py).
  **Deployed and field-confirmed 2026-07-14** on two live calls (`modal app logs
  rvc-worker`): `[AdaptivePitch] locked shift=+3.33 st (median F0=171.6Hz → target
  208Hz, 2.0s voiced, prior +7.0)` and `locked shift=+5.67 st (median F0=149.9Hz →
  target 208Hz, 2.1s voiced, prior +7.0)` — both math-exact against the formula,
  confirming the mechanism and the reconnect-resume design work as built. Two open
  items surfaced by actually listening: (1) the prior→locked transition is one
  audible pitch **jump** roughly 20-26s into the call — never listen-tested before
  shipping (the design's own lock-once-not-continuous tradeoff, now confirmed to have
  a real audible cost, not just a hypothetical one); (2) `RVC_TARGET_F0=208` is a
  single 2026-07-08 reference-output measurement, not derived from the model's
  training data — worth re-validating if a user's identity complaint persists after
  ruling out the input-muffling regression noted above. See [[active-backlog]].

## TensorRT/ONNX migration (merged to main 2026-07-07, merge commit `9c1093a`)
Full spec: `implementation_plan.md` / `TRT_ROLLOUT_STEPS.md` (repo root). `trt-migration`
branch is merged into `main`; C4 (A/B WAVs), C5 (listen test) and live-rollout confirmation
(plan Task 10, USER-RUN) are still pending — see [[active-backlog]].

### C3 benchmark results (2026-07-06, NVIDIA L4, ap-southeast)
Block geometry: 1400ms audio in (BLOCK_MS=1000 + CONTEXT_MS=400), 48kHz out.
Engine build + warmup (cold cache): **22.1s** (one-time cost, then cached on volume).

| Metric | ms | vs gate |
|---|---|---|
| min | 65 | — |
| **median** | **66** | ✅ ≤ 400ms |
| p95 | 68 | — |
| max | 68 | — |

All three sessions confirmed on TensorrtExecutionProvider (hubert FP16, generator FP32, rmvpe FP16).
**21× real-time** — gate PASSED. Phase 1 complete.

### Phase 2 D benchmark results (2026-07-07, NVIDIA L4, ap-southeast)
Block geometry: 720ms audio in (BLOCK_MS=320 + CONTEXT_MS=400 = CANONICAL_IN=11520 samples), 48kHz out.
Engine build + warmup (cold cache, after stale purge): **327.7s** (one-time cost).

| Metric | Phase 1 (1000ms block) | Phase 2 (320ms block) | Δ |
|---|---|---|---|
| min | 65ms | 53ms | −18% |
| **median** | **66ms** | **54ms** | **−18%** ✅ |
| p95 | 68ms | 55ms | −19% |
| max | 68ms | 55ms | |
| Real-time ratio | 21× | 13× | still >3× gate |

All 3 sessions on TensorrtExecutionProvider. Gate PASSED (54ms ≤ 400ms).
Merged to main (`b9df41f`). `modal deploy` required to go live on worker.


### Shims committed to trt-migration branch
- `attentions_onnx.py`: `torch.clamp(int, min=int)` → `max(int, int)` — `.size()` dims are Python
  ints in PyTorch 2.5 eager mode, `torch.clamp` requires a Tensor first arg.
- `models_onnx.py` (3 sites, `4bdbe5f`): `torch.randn_like`/`torch.rand` →
  `torch.zeros_like`/`torch.zeros` in `PosteriorEncoder.forward`, `SineGen._f02sine`,
  `SineGen.forward`. These emitted ONNX `RandomNormal`/`RandomUniform` nodes that TRT Myelin
  cannot compile (`randomFill.cpp::replaceFillNodesForMyelin` assertion).
- **Listen-test finding, found and fixed same day (`d463c41`, 2026-07-07): zeroing
  `SineGen.forward`'s unvoiced-frame noise caused audible hissing/garbled consonants**
  (unvoiced frames went fully silent instead of noisy). Fixed by externalizing that noise:
  `sine_noise` is now a real `numpy` `N(0,1)` tensor (shape `[1, OUT_PADDED_48K, 1]`)
  generated in `trt_pipeline.py` per block and passed in as an ONNX **input** rather than
  generated inside the graph — same trick already used for the generator's `rnd` reparam
  input, sidesteps TRT Myelin's RandomNormal restriction without losing the stochasticity.
  `SineGen._f02sine`'s initial-phase offset (`rand_ini`) is still zeroed/deterministic
  (lower perceptual impact than the unvoiced-noise bug; not yet revisited).

### Load-bearing gotchas
Load-bearing gotchas found during the three review rounds:
- **TensorRT cannot compile ONNX random ops** (`RandomNormal`/`RandomUniform` from
  `torch.rand`/`randn_like`). The generator's NSF `SineGen` uses both internally, so the
  tree carries edits to **vendored** `RVC/infer/lib/infer_pack/models_onnx.py` and
  `attentions_onnx.py` (int-vs-tensor guard). These shims are what make `generator.onnx`
  TRT-compilable — they are **committed** (`4bdbe5f`, `cd7749c`, `d463c41`) with `.gitignore`
  negation entries keeping them tracked despite `RVC/` being ignored, so the `RVC/`
  `git rm -r --cached` cleanup ([[active-backlog]]) is unblocked. **Zeroing an internal
  random op and externalizing it as an ONNX input are not equivalent fixes** — the first
  attempt zeroed SineGen's unvoiced-frame noise outright (silent unvoiced frames → audible
  hissing/garbled consonants, caught by ear the same day); the working fix generates real
  `N(0,1)` noise outside the graph and feeds it in as a model input instead. Any future
  TRT-incompatible random op should default to the externalize-as-input pattern, not zero.
- **TRT Myelin FP16 compiler bug on the generator**: `trt_fp16_enable` is deliberately
  `False` for the generator session (fp16 stays on for hubert/rmvpe) — see
  `trt_pipeline.py`'s provider options. Don't "optimize" it back to fp16 without re-testing;
  the flag split is intentional.
- **ORT silently falls back to CPU when an EP fails to load** — `TRTVoicePipeline.__init__`
  hard-fails via `sess.get_providers()` checks for exactly this reason. The base ONNX
  fallback sessions in `worker.py` do NOT have this check yet (open finding). Any new
  `InferenceSession` in this codebase should assert its expected provider.
- **Modal image layering**: Modal forbids build steps (`pip_install`/`env`/`run_commands`)
  after any `add_local_*` — that's why `modal_deploy/modal_defs.py` (single source of truth
  for `volume`/`image`/`trt_image`) splits build bases from final images and attaches local
  sources last. It also mounts itself (`add_local_python_source("modal_defs")`) so
  containers can import it — same class of trap as the 2026-07-03 `streaming.py` incident.
- **`env=` IS a valid `@app.function(...)` kwarg in Modal 1.5.1** (verified via
  `inspect.signature`) — don't flag it as an error in review; older Modal docs/memory may
  suggest otherwise.
- **`modal run` from repo root, pytest too**: `modal_deploy/test_streaming.py` imports the
  `modal_deploy` package — run `python -m pytest modal_deploy/...` from the repo root, not
  from inside `modal_deploy/` (collection fails there).

## SIP audio isolation (`backend/main.py::_restrict_sip_audio`, added 2026-07-03)
- **Why this exists**: LiveKit's SIP bridge runs server-side and ignores browser-level
  `setTrackSubscriptionPermissions()` — the SIP participant (the lead) receives a mix of
  every audio track in the room by default, including the agent's raw mic, not just the
  bot's converted track. `_restrict_sip_audio` is a fire-and-forget background task
  (`asyncio.create_task`, up to 8 attempts / 2s apart) that calls LiveKit's
  `update_subscriptions` to explicitly unsubscribe the SIP participant from the raw agent
  track once both it and the raw track are visible in the room.
- **Field-name trap, confirmed 100% failure in production before the fix**: the call built
  `api.UpdateSubscriptionsRequest(participant_identity=..., ...)`, but that message's real
  field is `identity` (verify with
  `livekit.protocol.room.UpdateSubscriptionsRequest.DESCRIPTOR.fields_by_name`). Every one of
  8 retries threw `Protocol message UpdateSubscriptionsRequest has no "participant_identity"
  field` on every call sampled in Render logs — meaning the raw+converted mixing this helper
  exists to prevent was still happening on every single call despite the helper being
  deployed. Fixed 2026-07-03 (see [[log]]) by using `identity=`. **Don't confuse this with**
  `CreateSIPParticipantRequest`'s `participant_identity` field (`main.py`, outbound dial) —
  that's a different message where the field genuinely is named `participant_identity`.
- **Confirmed live 2026-07-03**: `[SIP Isolation] ✅ ... unsubscribed` appears on every
  outbound call sampled after deploy (15:05, 15:07, 16:10, 17:05, 17:07) — zero failure
  lines in that window. The mixing bug is resolved.

## Modal deploy: local imports succeeding proves nothing about the remote container (2026-07-03)
The streaming rebuild's first real `modal deploy modal_deploy/worker.py` (it had only ever been
merged, never actually deployed, until this session) failed twice in a row:
1. **Stale folder-name reference.** `worker.py`/`app.py`/`local_server.py`/`.gitignore` all
   still said `Retrieval-based-Voice-Conversion-WebUI` after that folder was renamed to `RVC/` —
   `modal deploy` failed immediately with `local dir ... does not exist`, on *any* machine, since
   the code looked for the wrong name regardless of where it ran. Fixed by updating all four
   references to `RVC` (commit `d82f22c`).
2. **Sibling module never bundled into the container.** `worker.py` imports the sibling
   `modal_deploy/streaming.py` at the top (`from modal_deploy import streaming as st`, falling
   back to `import streaming as st`). That import succeeding *locally* while running
   `modal deploy` (it does — cwd is on `sys.path`) says **nothing** about whether Modal actually
   ships the file into the remote container. It didn't, by default, and the container
   crash-looped with `ModuleNotFoundError: No module named 'streaming'`. Fixed by adding
   `.add_local_python_source("streaming")` to the `Image` chain (commit `904757f`) — confirmed by
   the deploy's mount list showing `Created mount PythonPackage:streaming` and a subsequent
   `/health` returning `{"status":"ready",...}`.
   **General rule**: every local file/module a Modal function needs beyond the entrypoint script
   itself must be explicitly declared (`add_local_dir` / `add_local_file` /
   `add_local_python_source`) — Modal does not auto-trace imports the way a normal Python
   deployment might. If `worker.py` grows another sibling module, mount it the same way rather
   than assuming a clean local `modal deploy` run proves it'll be there remotely. See
   [wiki/pages/issues/modal-deploy-path-bugs.md](../../wiki/pages/issues/modal-deploy-path-bugs.md)
   for the full incident writeup.
- Side effect of bug 1: because `.gitignore` never matched the *current* folder name, it had
  silently stopped excluding `RVC/` — confirmed ~195 files under `RVC/` are already tracked in
  git. The `.gitignore` fix doesn't retroactively untrack them; see [[active-backlog]].

## LiveKit SIP outbound trunk went stale — `404 object cannot be found` on dial (2026-07-03, open)
First live outbound-call attempt after the Modal fixes above failed at the dial step (not the
voice-conversion step): `lk.sip.create_sip_participant(...)` in `backend/main.py` raised
`TwirpError(code=not_found, message=twirp error unknown: object cannot be found, status=404)`,
even though the immediately-preceding `list_outbound_trunk` call had just successfully found the
trunk by name and returned its ID (`ST_ruQpmqBLhYbj`). Root cause not fully diagnosed (possibly
connected to whatever infra changes happened around the Render→Singapore migration, but not
confirmed). **Fix**: `POST /api/setup` is designed to be safely re-runnable — it deletes the
outbound/inbound trunks and dispatch rule by name and recreates them fresh. Re-running it
produced new IDs (`ST_kFVkcpf5j8vh` outbound, `ST_U6rGLvrRy53H` inbound,
`SDR_AmuRZmcQCRE7` dispatch rule), confirming the old ones really were stale. **Important**: if
`TWILIO_SIP_TRUNK_ID` is set as an env var (locally or on Render), `_do_start_bot`'s outbound
flow uses it directly and skips the dynamic by-name lookup entirely — recreating the trunk via
`/api/setup` alone does *not* take effect until that env var is also updated to the new ID (or
removed, which is more robust against this recurring, since the code falls back to the by-name
lookup when it's unset). See
[wiki/pages/issues/livekit-sip-trunk-stale.md](../../wiki/pages/issues/livekit-sip-trunk-stale.md)
— still open as of this writing
(Twilio-webhook-config step of the same `/api/setup` run 401'd separately — see [[active-backlog]]
— and the actual retried call hasn't been confirmed successful yet).

## Twilio media edge pinning (2026-07-08)
- **The plain `{trunk}.pstn.twilio.com` termination domain does NOT geo-route** — it resolves
  to Twilio US1/Virginia (`54.172.60.x`). With LiveKit/Render in Singapore this put a
  transpacific hop in every call's RTP path. Confirmed consequence via the 2026-07-08 10:16 UTC
  call analysis: 5 dropouts of 0.18–0.40s of *loud* speech (~1.4s total in 68s) present in
  Twilio's own recording with flat out→Twilio delay — i.e. packet loss upstream of Twilio on
  the LiveKit→Twilio leg, not playout underrun (that would step the delay up) and not the
  lead's mobile network.
- Fixed 2026-07-08: LiveKit outbound trunk `ST_BMamqedncjzb` address updated **in place**
  (`update_outbound_trunk_fields`, ID unchanged) to
  `thezhafoundrykeira.pstn.singapore.twilio.com` (`54.169.127.x`, AWS ap-southeast-1); local
  `.env` and Render env `TWILIO_SIP_URI` updated to match; Twilio origination URL updated to
  `sip:3iv76uh14ps.sip.livekit.cloud;edge=singapore` (inbound direction).
- **Drift trap**: `/api/setup` (main.py) recreates the origination URL **without** the
  `;edge=` parameter — the outbound side survives a setup re-run (it reads `TWILIO_SIP_URI`),
  the inbound pin does not, until main.py is patched to append the edge param.
- Not yet field-verified: needs one post-fix demo call re-analyzed (compare dropout count
  against the 5-dropout baseline).
- Render service also got `PYTHONUNBUFFERED=1` (2026-07-08) — python prints were previously
  block-buffered, flushing a whole call's log lines at once under a single timestamp, which
  made log-timing forensics impossible.

## Offline diagnostic tooling (`modal_deploy/worker.py`, added 2026-07-03)
Built while investigating "converted voice doesn't match the trained voice on live calls" —
these are lasting tools, not throwaway debug code:
- **`convert_file_chunked` / `main_chunked`**: replays the *exact* logic `ws_stream` uses
  (block-accumulate via `st.BlockAccumulator`, per-block `run_conversion`, `trim_context`,
  `sola_crossfade`) against a static WAV file instead of a live WebSocket — optionally also
  running the same Level-3 `WebRTCNoiseSuppressor` processing the live path applies. Run via
  `modal run modal_deploy/worker.py::main_chunked --input-file <path> --pitch <n>`. Use this
  for any future "is it the pipeline's audio processing, or something live-only" question
  before touching production — it isolates chunking/SOLA/noise-suppression from
  network/SIP/telephony entirely. `pitch=-1` (default) auto-detects once from the *whole*
  file, matching `main()`'s own reference behavior — **not** `ws_stream`'s per-first-block
  detection, since production no longer uses `-1` live at all (see the pitch-detection
  gotcha above). If you test a file that opens with a stretch of silence, prefer an explicit
  `--pitch` over `-1` — `_auto_detect_pitch` has no silence check on its 1-second analysis
  window and will produce an effectively random result on near-zero-amplitude input.
- **`_DEBUG_SAVE_AUDIO` per-call in/out WAV capture (added 2026-07-08; replaces
  `_DEBUG_SAVE_RAW_AUDIO`, which the TRT migration silently dropped)**: each `/ws` session
  saves up to 120s of its input (16 kHz post-denoise, `call_<ts>_in16k.wav`) **and** its
  converted output (48 kHz post-SOLA, **pre-PresenceEQ** — the backend EQs after this point)
  to `/root/rvc-models/debug/` when the session closes — download with
  `modal volume get rvc-models debug/<filename> <local-path>`. Default ON; disable with
  `DEBUG_SAVE_AUDIO=0` + redeploy. Same "temporary while investigating" caveat as its
  predecessor (per-call overhead, volume storage). Pairs with the Twilio trunk recording
  (`record-from-answer-dual`, enabled 2026-07-08 on trunk `TK8958...da94`) for 3-point
  clarity isolation on outbound demo calls: raw input → converted output → what the lead
  actually heard (post-PSTN). See [[active-backlog]].

## GPU tier: a committed code change isn't a deployed one (2026-07-03)
`fastapi_app`'s `gpu=` was changed from `"T4"` to `"L4"` in a commit that landed on `main`,
but `git push`/commit and `modal deploy` are two entirely separate actions — nothing about
committing or pushing code touches what's actually running on Modal (unlike Render, which
auto-deploys on every push). Confirmed via `/health` that the live container was still
reporting `"Tesla T4"` well after the `L4` code was on `main`. An unrelated deploy (for the
`_DEBUG_SAVE_RAW_AUDIO` capture above) incidentally picked up the pending change — the live
worker is now genuinely on an `NVIDIA L4`. Lesson: always verify actual deployed state
(`/health`'s `cuda_device`) rather than trusting the committed code when GPU tier or any
other `@app.function` decorator setting matters to a live investigation — the two can silently
diverge for as long as nobody runs `modal deploy`.

## Render deployment
- `autoDeploy: commit` means **every push to `main` redeploys immediately**, tearing down
  the LiveKit worker and any in-flight `VoiceConversionWorker` mid-call. This was
  confirmed live on 2026-07-02: two redeploys within ~4 minutes during an active test call
  produced symptoms indistinguishable from "Modal not connecting." When iterating on
  pipeline code, either avoid pushing during a live test call or expect to re-warm Modal
  (`POST /api/warmup`) after every deploy.

## Windows dev environment
- `webrtc-noise-gain` (used by `WebRTCNoiseSuppressor`) has no prebuilt Windows wheel and
  needs MSVC build tools; without them the import fails and the suppressor silently
  degrades to passthrough (logged as a warning). This failure mode is silent and tests
  still pass, so a Windows dev environment can look identical to a fully-working one while
  actually running degraded audio processing — check startup logs for the warning, don't
  assume from green tests.
- `webrtcvad` is no longer imported or used anywhere in `backend/pipeline.py` post-rebuild
  (VAD-based chunking was deleted, not just made optional) — the old "without it, chunking
  falls back to fixed max-length" note no longer applies to anything. It's effectively an
  unused dependency now regardless of platform.

## Voice identity & clarity: pitch range + input-chain fidelity (root-caused 2026-07-08)
Why live calls never sounded like the trained voice AND were muffled — two *independent*
knobs, both upstream of the model, both now env-gated (commit `f748a89`). This is the
authoritative "why"; [[log]] 2026-07-08 has the decision framing.
- **RVC identity depends on landing the input in the model's trained pitch range.** The
  mi-test model centers ~208Hz (measured from a known-good output). Feeding audio outside
  that range produces a *wrong-identity* voice, not just a pitch error (same failure class
  as the reverted GPU auto-detect, above). The hardcoded `pitch_shift = 12 if male else 0`
  in `_do_start_bot` doubled the user's ~137Hz fundamental to ~274Hz (~5 semitones too
  high). Correct shift is **agent-F0-specific** (~137Hz → +7); now `RVC_MALE_PITCH_SHIFT`
  env (default 12). A future robust fix would target the model's pitch regardless of input
  F0 (what `pitch=-1` auto-detect *tried* but did unreliably), not a per-agent constant.
- **Never noise-suppress the agent voice twice.** The input reached the model NS'd twice:
  browser `getUserMedia({audio:true})` enables noiseSuppression + autoGainControl by
  default, THEN `WebRTCNoiseSuppressor(ns_level=3)` ran server-side. Both strip the HF
  detail (sibilance/consonants) HuBERT reads — measured live-input spectral centroid 413Hz
  vs 720Hz on clean `test1.wav`, −9dB at 6-8kHz, and it propagates to a duller output. Fix:
  frontend `app.js` now requests raw capture (`noiseSuppression:false`,
  `autoGainControl:false`, `echoCancellation:true`); server level is `NS_LEVEL` env
  (default 3, live 1). **Browser caches `app.js`** — the raw-capture change only takes
  effect after a hard refresh (tell: mic-permission re-prompt).
- **Regression re-observed 2026-07-13/14, likely the same browser-cache gotcha.** Three
  consecutive field calls (07-13 x2, 07-14 22:03) measured input spectral centroid back down
  to 250-360Hz — *worse* than the original 413Hz pre-fix measurement, not close to the ~720Hz
  the fix restored on 07-11 (628Hz that day). Both sides of the code were re-verified correct
  (server logged `Noise Suppressor (Level 1)`; `frontend/app.js:429-431` still requests
  `noiseSuppression:false, autoGainControl:false`), so this isn't a code regression — leading
  hypothesis is a stale cached `app.js` on the agent's browser tab still requesting the old
  defaults. Compounding structural risk found the same session: `backend/main.py:1249` serves
  the frontend via plain `StaticFiles` with **no cache-control headers at all**, so nothing
  prevents this from recurring for any agent whose tab loaded the dashboard before a future
  frontend fix. Not yet confirmed by a hard-refresh test call. See [[active-backlog]].
- **Adaptive per-call pitch lock (2026-07-13/14) superseded the fixed-shift half of this
  fix** — see the decision entry in [[log]] and the dedicated note under "Modal RVC GPU
  worker" below for the mechanism. Field-confirmed working on two live calls 2026-07-14
  (locked +3.33st and +5.67st, both landing the converted output ≈208Hz as designed) — so the
  *pitch* axis of this original bug is now handled adaptively per-call rather than by a
  static constant. The muffled-input axis (this bullet) is separate and, per the regression
  note above, needs its own fresh field verification.
- **Diagnostic method (reusable).** Autocorrelation F0 of an output WAV (voiced frames,
  lag r/60..r/400) checks pitch targeting; per-file speech-frame power spectrum (centroid,
  85% rolloff, band ratios rel 0.3-1kHz) localizes muffle to input vs model. Offline replay
  via `modal run worker.py::main_chunked --input-file … --pitch N` (now also takes
  `--index-rate/--rms-mix-rate/--protect/--output-file`) reproduces the exact live
  block/SOLA path on a static WAV — isolates engine from network/telephony. The pre-TRT
  engine can be replayed from git `326098e` (scratch copy, force `import streaming`) to rule
  the migration in/out.

## Call-analysis 3-point capture (built 2026-07-08)
To assess any demo call the user supplies only the approximate call time; pull three
artifacts and diff stage-by-stage:
1. **Twilio recording** = ground truth of what the lead heard. Trunk `TK8958…da94` has
   `record-from-answer-dual`. `Calls.json?StartTime>=…` → `Recordings.json?CallSid=…` →
   `.wav?RequestedChannels=2` (ch0 converted voice, ch1 lead — verify by envelope corr).
2. **Modal debug WAVs** = `modal volume ls rvc-models debug/` → `call_<ts>_in16k.wav`
   (post-denoise input) + `call_<ts>_out48k.wav` (post-SOLA, **pre-PresenceEQ** output).
   `_DEBUG_SAVE_AUDIO` flag in `worker.py`, default ON — **temporary**, disable via
   `DEBUG_SAVE_AUDIO=0` + redeploy once the identity/clarity work is field-verified.
3. **Render logs** via Render MCP (`srv-d932m4cvikkc73belt1g`, workspace
   `tea-d91lambtqb8s7398mfd0`); legible now that `PYTHONUNBUFFERED=1` is set.
Envelope method: 20ms RMS envelopes; windowed xcorr out48k↔twilio-conv gives delay(t)
(flat lag ⇒ downstream loss is packet loss; step-ups ⇒ playout underruns); replaying the
320ms/RMS<150 silence gate over in16k reproduces out48k's zero mask (~97% agreement).
