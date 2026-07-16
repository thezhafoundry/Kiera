---
title: .agents/context/subsystem-notes.md
type: source
sources: [../../../.agents/context/subsystem-notes.md]
updated: 2026-07-16
---

[.agents/context/subsystem-notes.md](../../../.agents/context/subsystem-notes.md) —
load-bearing gotchas per subsystem, the "why" the code alone doesn't convey.

## Key claims
- **Pipeline/playout (current)**: the converter is driven as one long-lived duplex WS
  stream (`_run_conversion_stream`) — frames arrive strictly in order, so there is nothing
  to reorder (no sequence numbers, no `_run_playout`). Since 2026-07-03, converted output
  feeds a bounded standing playout buffer (**0.25s target** / 5s cap as of 2026-07-13 TRT
  phase 2, down from an original ~3s target; drop-oldest overflow) drained by a separate
  `_run_playout_consumer` task. After the initial target fill, the consumer drains bounded
  100ms chunks rather than the entire queue at once. See [[adaptive-playout-buffer]], [[buffering-history]],
  [[tensorrt-migration]].
- `contextlib.aclosing(gen)` around the conversion generator is load-bearing, not
  optional — a bare `async for ... break` won't reliably call the generator's `aclose()`,
  which is what tears down `RVCStreamingConverter`'s WS connection and background tasks.
- **Modal worker**: cold start measured at ~75s with *no* `/health` response at all
  beforehand historically; the current L4/TensorRT active-session measurement is 72.51s.
  `RVCEngine.startup()` raises `RuntimeError("No FAISS index found.")` if the Modal
  volume's index is empty. See [[rvc-cold-start]].
- **TensorRT/ONNX migration — merged to main 2026-07-07** (commit `9c1093a`):
  3 static-shape TRT engines (HuBERT FP16 / generator FP32 due to Myelin bug / RMVPE FP16)
  on the L4, with RMVPE pitch tracking re-enabled. Vendored ONNX-export shims now committed
  (`4bdbe5f`, `cd7749c`). Modal v11 live baseline is now confirmed at 50.75/51.61ms
  median/p95 inference. C4 (offline A/B WAVs) and C5 (listen test) remain open.
- **FAISS index re-read from disk every call — fixed 2026-07-03**: monkeypatched
  `faiss.read_index` in `worker.py`, no vendored-code edits.
- **`max_containers` gap — fixed**: current approved two-call configuration is two
  containers per edge and one active stream per container.
- **Gender/pitch auto-detection unreliable — reverted 2026-07-03**: back to manual UI
  `agentGender` toggle.
- Region mismatch (`ap-southeast` pinned) — **resolved 2026-07-03**, Render confirmed
  live in Singapore. The newer input-routing A/B is separate and production remains stable.
- **2026-07-16 RVC baseline:** a 9.6s authenticated stream had zero drops and
  50.75/51.61ms median/p95 inference, but -211.46ms duration delta. Stable/AP routing must
  be measured from Render, and the nominal startup warm-up has a non-fatal `F0Predictor`
  import failure. See [[rvc-baseline-routing-and-duration]].
- **LLVC paused:** keep disabled; retain only safety/test scaffolding while RVC onboarding
  and latency are optimized.
- **Render deployment**: `autoDeploy: commit` redeploys on every push to `main`,
  kills in-flight calls. See [[render-autodeploy-kills-live-calls]].
- **Windows dev**: `webrtc-noise-gain` silently degrades to passthrough if its native lib
  is missing. `webrtcvad` is gone entirely post-rebuild — unused dependency.
- **Call-control safety (2026-07-15)**: `/api/call/outbound` prepares but does not dial;
  `/api/call/outbound/dial` waits for an agent audio track and verified SIP restriction.
  Inbound `/api/call/wait` hangs up instead of bridging when worker readiness or isolation
  fails. These gates are local until a deployed live call confirms them.
