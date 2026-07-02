---
title: RVC / Modal cold start behavior
type: concept
sources: [latency-md, subsystem-notes]
updated: 2026-07-02
---

The Modal T4 worker ([modal_deploy/worker.py](../../../modal_deploy/worker.py)) scales
to zero after `scaledown_window=120` (2 minutes of no requests). A cold container must
load the RVC model + HuBERT + FAISS index and run one silent warm-up inference
(`RVCEngine.startup()`) before serving real requests.

**Measured live (2026-07-02), not assumed**: a `/health` ping against an idle container
got *no response at all* for ~75s before it returned
`{"status":"ready","cuda_available":true,"cuda_device":"Tesla T4"}` — much slower than
the 8–30s the code comments (and an earlier version of LATENCY.md) assumed. Once warm,
RVC calls completed in 580–750ms.

**The 2000ms conversion budget (`budget_ms=2000.0` in `_do_start_bot`,
[backend/main.py:194](../../../backend/main.py)) is intentionally shorter than a cold
start**, so a cold GPU *always* fails over to raw voice for the first several chunks —
this is the fail-safe working as designed, not a bug to fix by raising the timeout
(doing so risks dead air instead of raw-voice fallback). See
[[audio-pipeline-latency-budget]] for the full latency budget this trades against.

## Confirmed production incident (2026-07-02, 06:35:41 UTC)
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

## Other cold-start traps
- `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's index directory is empty — looks like a hang from the caller's side, only
  visible in `modal app logs rvc-worker`.
- A single short-timeout `curl` against `/health` is not a reliable "Modal is broken"
  signal on a cold container; use `POST /api/warmup` (30s interval, up to 6 min) instead.
- Keep the GPU warm proactively for back-to-back calls (`/api/warmup` at shift start) —
  the automatic pre-warm ping fired on every bot spawn is meant to overlap cold start
  with call setup/ringing, not to be the only mechanism.
- See also [[modal-render-region-mismatch]] — a transpacific round trip on top of a warm
  request eats into the same 2000ms budget.
