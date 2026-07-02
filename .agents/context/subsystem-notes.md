# Subsystem Notes & Load-Bearing Gotchas

<!-- One section per subsystem. Capture the WHY and the traps that are not obvious
     from reading the code — this is what the wiki/codebase cannot tell you. -->

## Pipeline latency & playout (`backend/pipeline.py`)
- The ordered playout queue (`_run_playout`) exists because the original design published
  every RVC result the instant it finished, which meant a slow chunk could reorder audio
  or leave gaps with nothing to absorb the jitter. It now separates producing conversions
  from playing them out, with an **adaptive standing buffer** (P95 of last 20 RVC round
  trips × 1.2, clamped 400-1500ms) refilled every speech session, not just once per call.
- `asyncio.Condition.notify_all()` wakes every waiter, not just the one whose data
  arrived. `_run_playout`'s wait for a specific sequence number loops against a wall-clock
  deadline instead of a single `await cv.wait()` — otherwise an unrelated chunk resolving
  can look like your own wait timing out early, silently shrinking the reorder window
  (`_REORDER_WAIT_S`, 600ms). If you touch this wait loop, keep the deadline-based retry.
- `MIN_CHUNK_MS` was raised from 250ms to 450ms not for buffering-depth reasons but
  throughput: RVC has ~600-750ms fixed per-request overhead that doesn't amortize over
  chunks that short, so "queue backed up" in the logs was a chunk-size problem, not a
  buffer-depth problem. If this warning reappears, check chunk size before touching
  buffer targets.
- Full history of these tuning decisions and the several revert/re-fix cycles around
  the pre-buffer and 4s timeout fallback lives in recent git log — `git log --oneline`
  around commits `523e6d9`..`fe678d6` — read those commit messages before re-deriving
  the same fix.

## Modal RVC GPU worker (`modal_deploy/worker.py`)
- Cold start is much slower than the code comments assume: measured live at ~75s with
  no `/health` response at all before `{"status":"ready"}` (see LATENCY.md §4.1), not the
  8-30s originally assumed. Any timeout/budget tuning must account for this, and the
  2000ms conversion budget in `_do_start_bot` is *intentionally* too short to survive a
  cold start — that's the fail-safe working as designed, not a bug to "fix" by raising it
  carelessly (raising it risks the lead sitting in dead air instead of raw voice).
- `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's `logs/mi-test/*.index` is empty — looks like a hang from the caller's side but
  is a missing-model error, only visible in `modal app logs rvc-worker`.
- **Region mismatch (open as of 2026-07-02):** the Modal function is pinned to
  `region="ap-southeast"` on the assumption Render/Twilio are nearby, but the deployed
  Render service is actually in Oregon (us-west) — every call currently pays a
  transpacific round trip on top of inference. Fixing this (repin Modal to a US region,
  or move Render) is tracked in [[active-backlog]].

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
  degrades to passthrough (logged as a warning). `webrtcvad` is similarly optional —
  without it, chunking falls back to fixed max-length instead of VAD-cut. Both failure
  modes are silent and tests still pass, so a Windows dev environment can look identical
  to a fully-working one while actually running degraded audio processing — check startup
  logs for the warnings, don't assume from green tests.
