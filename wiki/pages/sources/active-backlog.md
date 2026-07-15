---
title: .agents/projects/active-backlog.md
type: source
sources: [../../../.agents/projects/active-backlog.md]
updated: 2026-07-15
---

[.agents/projects/active-backlog.md](../../../.agents/projects/active-backlog.md) —
current roadmap and known tech debt.

## Key claims
- Backlog (open, as of 2026-07-15): P0 items now cover credential rotation, control-plane
  authentication, Modal authentication, managed worker/call ordering, and non-destructive
  `/api/setup`. The code changes are local; provider secrets, deployment, and live-call
  acceptance remain open.
- Remaining high-priority verification includes the stale Twilio token, a retried outbound
  call, confirming the deployed worker keeps debug WAV capture disabled, TRT live/C4/C5 checks, the
  adaptive-pitch listen test, and a real-call test of the bounded playout drain. See the
  linked issue pages in `wiki/index.md`.
- Medium/low work remains: preserve the Singapore SIP edge during future setup, avoid Render
  auto-deploy interruptions, clean tracked `RVC/` files deliberately, and improve Windows
  RNNoise support.
- **Resolved** (moved out of open backlog):
  - Modal/Render region mismatch (Render confirmed live in Singapore, 2026-07-03).
  - Modal worker `max_containers` gap.
  - Unreliable GPU-side pitch/gender auto-detection.
  - FAISS index re-read-per-call.
  - SIP audio isolation field-name bug (confirmed live 2026-07-03).
  - First `modal deploy` of the streaming rebuild (path bugs fixed 2026-07-03).
- Tech debt: buffering/pre-buffer logic reverted and re-implemented **five** times
  (now effectively a sixth with the TRT phase 1 1.25s reduction — see [[buffering-history]]).
  Treat playout timing changes as high-risk, re-run the spectral latency test (LATENCY.md §3)
  after any edit.
- No automated latency regression test — the spectral tone test is manual (two browser
  tabs); a regression could ship without any CI signal.
- `RVC/` vendored third-party WebUI is checked into the repo tree for offline training
  only, not runtime — worth confirming it's excluded from anything that scans/lints the
  whole repo.
