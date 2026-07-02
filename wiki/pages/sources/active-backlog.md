---
title: .agents/projects/active-backlog.md
type: source
sources: [../../../.agents/projects/active-backlog.md]
updated: 2026-07-02
---

[.agents/projects/active-backlog.md](../../../.agents/projects/active-backlog.md) —
current roadmap and known tech debt.

## Key claims
- Backlog (open, identified 2026-07-02):
  1. **High** — resolve Modal/Render region mismatch. See [[modal-render-region-mismatch]].
  2. **Medium** — avoid Render `autoDeploy: commit` killing in-flight calls mid-test
     (deploy hooks, manual deploy, or graceful shutdown/drain). See
     [[render-autodeploy-kills-live-calls]].
  3. **Low** — get `webrtc-noise-gain` building on Windows so local dev doesn't
     silently run in passthrough mode.
- Tech debt: buffering/pre-buffer logic reverted and re-implemented multiple times
  (see [[buffering-history]]) — treat playout timing changes as high-risk, re-run the
  spectral latency test (LATENCY.md §3) after any edit.
- No automated latency regression test — the spectral tone test is manual (two browser
  tabs); a regression could ship without any CI signal.
- `RVC/` vendored third-party WebUI is checked into the repo tree for offline training
  only, not runtime — worth confirming it's excluded from anything that scans/lints the
  whole repo.
