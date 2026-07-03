---
title: .agents/projects/active-backlog.md
type: source
sources: [../../../.agents/projects/active-backlog.md]
updated: 2026-07-03
---

[.agents/projects/active-backlog.md](../../../.agents/projects/active-backlog.md) —
current roadmap and known tech debt.

## Key claims
- Backlog (open, as of 2026-07-03):
  1. **High** — verify the 2026-07-03 standing-buffer/bigger-block fix actually
     eliminates "part by part" audio on a real live call. See
     [[part-by-part-audio-investigation]].
  2. **Medium** — if insufficient, bump the Modal GPU tier (T4 → A10G/L4); no retraining.
  3. **Low** — if still insufficient, ONNX/TensorRT export of the HuBERT/generator path;
     no retraining, but real export/verification effort, and only speeds up the neural-net
     part (not FAISS search or `pm` pitch estimation).
  4. **Medium** — avoid Render `autoDeploy: commit` killing in-flight calls mid-test
     (deploy hooks, manual deploy, or graceful shutdown/drain). See
     [[render-autodeploy-kills-live-calls]].
  5. **Low** — get `webrtc-noise-gain` building on Windows so local dev doesn't
     silently run in passthrough mode.
- **Resolved 2026-07-03** (moved out of the open backlog): Modal/Render region mismatch
  (Render confirmed live in Singapore); Modal worker `max_containers` gap; unreliable
  GPU-side pitch/gender auto-detection; FAISS index re-read-per-call. See
  [[modal-render-region-mismatch]], [[part-by-part-audio-investigation]].
- Tech debt: buffering/pre-buffer logic reverted and re-implemented multiple times
  (see [[buffering-history]]) — treat playout timing changes as high-risk, re-run the
  spectral latency test (LATENCY.md §3) after any edit.
- No automated latency regression test — the spectral tone test is manual (two browser
  tabs); a regression could ship without any CI signal.
- `RVC/` vendored third-party WebUI is checked into the repo tree for offline training
  only, not runtime — worth confirming it's excluded from anything that scans/lints the
  whole repo.
