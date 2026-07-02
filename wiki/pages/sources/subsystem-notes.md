---
title: .agents/context/subsystem-notes.md
type: source
sources: [../../../.agents/context/subsystem-notes.md]
updated: 2026-07-02
---

[.agents/context/subsystem-notes.md](../../../.agents/context/subsystem-notes.md) —
load-bearing gotchas per subsystem, the "why" the code alone doesn't convey.

## Key claims
- **Pipeline/playout**: the ordered playout queue exists because the original design
  published every RVC result immediately, causing reordering/gaps. Adaptive standing
  buffer = P95 of last 20 RVC round trips × 1.2, clamped 400–1500ms, refilled every
  speech session. See [[adaptive-playout-buffer]].
- `asyncio.Condition.notify_all()` wakes *every* waiter — `_run_playout`'s wait for a
  specific sequence number must loop against a wall-clock deadline
  (`_REORDER_WAIT_S` = 600ms), not a single `await cv.wait()`, or an unrelated chunk
  resolving can look like your own wait timing out early.
- `MIN_CHUNK_MS` raised 250ms → 450ms for **throughput**, not buffer depth: RVC has a
  fixed ~600–750ms per-request overhead that doesn't amortize over short chunks.
- **Modal worker**: cold start measured at ~75s with *no* `/health` response at all
  beforehand — much slower than the 8–30s the code comments assume. The 2000ms
  conversion budget is intentionally shorter than this (fail-safe by design, not a bug).
  `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's index is empty — looks like a hang, is actually a missing-model error only
  visible in `modal app logs rvc-worker`. See [[rvc-cold-start]].
- Region mismatch (`ap-southeast` pinned, Render actually in Oregon) confirmed open as
  of 2026-07-02. See [[modal-render-region-mismatch]].
- **Render deployment**: `autoDeploy: commit` redeploys on every push to `main`,
  confirmed live to have killed an in-flight call twice in ~4 minutes during a test
  session — symptoms indistinguishable from "Modal not connecting." See
  [[render-autodeploy-kills-live-calls]].
- **Windows dev**: `webrtc-noise-gain` and `webrtcvad` both silently degrade to
  passthrough / fixed-chunking if their native libs are missing — tests still pass, so a
  broken Windows dev setup can look identical to a working one. Check startup logs, not
  test results.
