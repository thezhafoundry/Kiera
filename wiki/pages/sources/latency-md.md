---
title: LATENCY.md
type: source
sources: [../../../.agents/context/subsystem-notes.md]
updated: 2026-07-16
---

**`LATENCY.md` was removed from the repo on 2026-07-16** — its still-needed content
(budget table, log reference, test procedure, troubleshooting checklist) was merged into
the "Latency budget, log reference, and troubleshooting" section of
[.agents/context/subsystem-notes.md](../../../.agents/context/subsystem-notes.md); its
explicitly-marked-historical §5 (the old pre-rebuild ordered-playout-queue design,
already deleted from the code) was dropped rather than carried forward. This page's key
claims below remain accurate as facts, just re-sourced.

## Key claims
- Current RVC baseline: 320ms block / 400ms context / 80ms SOLA / 250ms playout target.
  The playout cap is 5s and the consumer drains bounded 100ms chunks.
- Modal v11 live baseline confirmed L4/TensorRT with 50.75ms median / 51.61ms p95 inference
  over 30 blocks. Cold active readiness was 72.51s.
- The developer-laptop route measured converter wait 1207.11/1358.56ms median/p95 and an
  estimated network component of 837.05/988.91ms. It is not a Render/PSTN mouth-to-ear result.
- Output was 211.46ms shorter than 9.6s input despite zero reported drops; duration
  preservation is an open promotion gate.
- Modal worker moved from **T4 → L4** GPU (2026-07-03).
- Render's compute-region mismatch is resolved. A separate stable-vs-AP Modal input-routing
  A/B is open, and Render remains on the stable endpoint.
- Raw-voice fallback is gone structurally — the fail-closed warm gate blocks the call (503
  outbound / hold-music inbound) rather than degrading to raw audio.
- LLVC training/deployment is paused; its code was removed from `main` the same day
  (2026-07-16) and set aside on branch `codex/llvc-pilot` — see
  `.agents/projects/active-backlog.md` for current status.
- The old VAD-chunked ordered-playout-queue design (formerly §5, historical even before
  removal) described machinery already deleted from the code; not carried into the merge.
- A real browser→LiveKit→Render→Modal→SIP/PSTN spectral/listening measurement remains open.
