---
title: RVC / Modal cold start behavior
type: concept
sources: [latency-md, subsystem-notes]
updated: 2026-07-16
---

> **Superseded 2026-07-02, GPU tier changed 2026-07-03:** the "2000ms budget → raw-voice
> fail-safe" mechanism described below no longer exists — the streaming rebuild deleted the
> raw-voice-fallback path structurally. A cold GPU today blocks the call instead (fail-closed
> warm gate: outbound returns 503, inbound stays on hold) — see CLAUDE.md "Telephony & SIP"
> and [[audio-pipeline-latency-budget]]. The worker is also an **L4** as of 2026-07-03, not a
> T4. This page's cold-start timing measurement and incident writeup are kept as historical
> record; don't treat the fail-safe/budget prose as current behavior.

The Modal worker ([modal_deploy/worker.py](../../../modal_deploy/worker.py)) scales
to zero after `scaledown_window=120` (2 minutes of no requests). A cold container must
load the RVC model + HuBERT + FAISS index and run one silent warm-up inference
(`RVCEngine.startup()`) before serving real requests. As of the TRT migration
(2026-07-07), `USE_TRT=1` also pays a one-time ~22s TRT engine build/warmup on a cold
volume cache — see [[tensorrt-migration]].

**Measured live (2026-07-02), not assumed**: a `/health` ping against an idle container
got *no response at all* for ~75s before it returned
`{"status":"ready","cuda_available":true,"cuda_device":"Tesla T4"}` — much slower than
the 8–30s the code comments (and an earlier version of LATENCY.md) assumed. Once warm,
RVC calls completed in 580–750ms. This measurement predates the 2026-07-03 T4→L4 move, so
treat the absolute number as historical, not a current benchmark.

**Measured live on the current L4/TensorRT baseline (2026-07-16):** an authenticated
call-session readiness check took **72.51s** from cold. The narrow stable placement logged
delayed L4 scheduling. A parallel broad-AP function was added for measurement, but its first
container landed in Tokyo; broader placement is not the same as Singapore colocation. The
startup sequence also logs a non-fatal `F0Predictor` import failure during its nominal
warm-up before TRT becomes ready. See [[rvc-baseline-routing-and-duration]].

**(Historical, pre-2026-07-02 rebuild) The 2000ms conversion budget** (`budget_ms=2000.0`
in `_do_start_bot`, `backend/main.py`) was intentionally shorter than a cold start, so a
cold GPU *always* failed over to raw voice for the first several chunks — that was the
fail-safe working as designed at the time. This entire mechanism (the budget, the raw
fallback, `RVCVoiceConverter` being on the live call path at all) was removed in the
streaming rebuild; see [[audio-pipeline-latency-budget]] for the current latency budget.

## Confirmed production incident (2026-07-02, 06:35:41 UTC) — historical, pre-rebuild
A lead heard the agent's raw voice for an **entire call** (room
`outbound_916281686616_1782974101`). Root cause: every chunk logged a 2000ms conversion
timeout — not just the first one or two the fail-safe is meant to cover — while the bot
began publishing audio only ~30s after a pre-warm ping, well short of the ~75s+ cold
start actually measured. A second bot spawn fired ~2s after the first for the same lead
number, likely resetting/duplicating the warm-up cycle (still open — worth a follow-up
if "call twice in quick succession" is reproducible).

**Fix applied**: `POST /api/call/outbound` now calls `_wait_for_rvc_ready()` — polling
`/health` every 5s for up to 90s — *after* spawning the bot but *before* creating the
SIP participant that rings the lead, so the lead's phone doesn't start ringing until RVC
reports `ready` (or the 90s cap is hit, in which case it dials anyway with a warning).
`/api/warmup` was refactored to share this helper.

## Other cold-start traps (current)
- `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's index directory is empty — looks like a hang from the caller's side, only
  visible in `modal app logs rvc-worker`.
- A single short-timeout `curl` against `/health` is not a reliable "Modal is broken"
  signal on a cold container; use `POST /api/warmup` (30s interval, up to 6 min) instead.
- Keep the GPU warm proactively for back-to-back calls (`/api/warmup` at shift start, or
  `RVC_KEEPWARM=1` for an env-gated keep-warm loop) — the automatic pre-warm ping fired on
  every bot spawn is meant to overlap cold start with call setup/ringing, not to be the
  only mechanism.
- A cold/unready GPU today blocks the call via the fail-closed warm gate
  (`worker.wait_until_ready`/`is_ready`) rather than degrading to raw voice — see the
  top-of-document banner and CLAUDE.md "Telephony & SIP".
- See also [[modal-render-region-mismatch]] for the resolved historical Render compute
  migration. Modal input routing and broad-AP placement are a separate current experiment
  tracked in [[rvc-baseline-routing-and-duration]].
