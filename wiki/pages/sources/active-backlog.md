---
title: .agents/projects/active-backlog.md
type: source
sources: [../../../.agents/projects/active-backlog.md]
updated: 2026-07-16
---

[.agents/projects/active-backlog.md](../../../.agents/projects/active-backlog.md) —
current roadmap and known tech debt.

## Key claims
- Backlog (open, as of 2026-07-16): the RVC-first P0 gates are duration preservation,
  stable/AP routing measurement from Render Singapore, and a warm staff PSTN baseline.
  Modal authentication/deployment is resolved; production Render routing is unchanged.
- Remaining high-priority verification includes the startup `F0Predictor` warm-up import,
  Candidate B artifacts/quality after baseline gates, the stale Twilio token, a retried outbound
  call, confirming the deployed worker keeps debug WAV capture disabled, TRT live/C4/C5 checks, the
  adaptive-pitch listen test, and a real-call test of the bounded playout drain. See the
  linked issue pages in `wiki/index.md`.
- Medium/low work remains: preserve the Singapore SIP edge during future setup, avoid Render
  auto-deploy interruptions, clean tracked `RVC/` files deliberately, and improve Windows
  RNNoise support.
- **Resolved** (moved out of open backlog):
  - Modal/Render region mismatch (Render confirmed live in Singapore, 2026-07-03).
  - Modal worker `max_containers` gap (current cap two per edge, one stream per container).
  - Modal worker authentication deployment.
  - Unreliable GPU-side pitch/gender auto-detection.
  - FAISS index re-read-per-call.
  - SIP audio isolation field-name bug (confirmed live 2026-07-03).
  - First `modal deploy` of the streaming rebuild (path bugs fixed 2026-07-03).
- Tech debt: buffering/pre-buffer logic reverted and re-implemented **five** times
  (now effectively a sixth with the TRT phase 1 1.25s reduction — see [[buffering-history]]).
  Treat playout timing changes as high-risk, re-run the spectral latency test (LATENCY.md §3)
  after any edit.
- `scripts/rvc_stream_benchmark.py` automates converter-path measurements, but is not yet a
  CI gate and cannot replace the manual PSTN mouth-to-ear test.
- `RVC/` vendored third-party WebUI is checked into the repo tree for offline training
  only, not runtime — worth confirming it's excluded from anything that scans/lints the
  whole repo.
