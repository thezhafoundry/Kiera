---
title: .agents/context/subsystem-notes.md
type: source
sources: [../../../.agents/context/subsystem-notes.md]
updated: 2026-07-07
---

[.agents/context/subsystem-notes.md](../../../.agents/context/subsystem-notes.md) —
load-bearing gotchas per subsystem, the "why" the code alone doesn't convey.

## Key claims
- **Pipeline/playout (current)**: the converter is driven as one long-lived duplex WS
  stream (`_run_conversion_stream`) — frames arrive strictly in order, so there is nothing
  to reorder (no sequence numbers, no `_run_playout`). Since 2026-07-03, converted output
  feeds a bounded standing playout buffer (**1.25s target** / 5s cap as of 2026-07-07 TRT
  phase 1, down from an original ~3s target; drop-oldest overflow) drained by a separate
  `_run_playout_consumer` task. See [[adaptive-playout-buffer]], [[buffering-history]],
  [[tensorrt-migration]].
- `contextlib.aclosing(gen)` around the conversion generator is load-bearing, not
  optional — a bare `async for ... break` won't reliably call the generator's `aclose()`,
  which is what tears down `RVCStreamingConverter`'s WS connection and background tasks.
- **Modal worker**: cold start measured at ~75s with *no* `/health` response at all
  beforehand (historical, T4 measurement). GPU is now an **L4** (since 2026-07-03).
  `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's index is empty. See [[rvc-cold-start]].
- **TensorRT/ONNX migration — merged to main 2026-07-07** (commit `9c1093a`):
  3 static-shape TRT engines (HuBERT FP16 / generator FP32 due to Myelin bug / RMVPE FP16)
  on the L4, with RMVPE pitch tracking re-enabled. Vendored ONNX-export shims now committed
  (`4bdbe5f`, `cd7749c`). C3 GPU benchmark passed: median 66ms/p95 68ms (2026-07-06, L4
  ap-southeast). C4 (offline A/B WAVs), C5 (listen test), and live deploy confirmation
  still pending.
- **FAISS index re-read from disk every call — fixed 2026-07-03**: monkeypatched
  `faiss.read_index` in `worker.py`, no vendored-code edits.
- **`max_containers` gap — fixed 2026-07-03**: added `max_containers=1`.
- **Gender/pitch auto-detection unreliable — reverted 2026-07-03**: back to manual UI
  `agentGender` toggle.
- Region mismatch (`ap-southeast` pinned) — **resolved 2026-07-03**, Render confirmed
  live in Singapore.
- **Render deployment**: `autoDeploy: commit` redeploys on every push to `main`,
  kills in-flight calls. See [[render-autodeploy-kills-live-calls]].
- **Windows dev**: `webrtc-noise-gain` silently degrades to passthrough if its native lib
  is missing. `webrtcvad` is gone entirely post-rebuild — unused dependency.
