---
title: .agents/projects/active-backlog.md
type: source
sources: [../../../.agents/projects/active-backlog.md]
updated: 2026-07-07
---

[.agents/projects/active-backlog.md](../../../.agents/projects/active-backlog.md) —
current roadmap and known tech debt.

## Key claims
- Backlog (open, as of 2026-07-07):
  1. **High** — verify the 2026-07-03 standing-buffer/bigger-block fix actually
     eliminates "part by part" audio on a real live call. See
     [[part-by-part-audio-investigation]].
  2. **High** — `TWILIO_AUTH_TOKEN` on Render appears stale/invalid; re-run `/api/setup`.
  3. **High** — confirm the retried outbound call with new SIP trunk ID succeeds.
  4. **High** — flip `_DEBUG_SAVE_RAW_AUDIO` back to `False` and redeploy once voice-identity
     investigation concludes.
  5. **High** — voice-identity mismatch investigation (open). See
     [[voice-identity-mismatch-investigation]].
  6. **High** — **TensorRT migration**: merged to `main` 2026-07-07 (`9c1093a`). C3 GPU
     benchmark passed (median 66ms/p95 68ms). Remaining: C4 (offline A/B WAVs), C5 (listen
     test for breathiness tradeoff), and confirming the live Modal deploy actually serves TRT
     (`/api/health` → `"engine": "trt"`) before assuming it from committed code.
  7. **Medium** — `RVC/` folder: ~195 files tracked in git despite intended gitignore
     (unblocked 2026-07-07 — TRT shims now committed).
  8. **Medium** — avoid Render `autoDeploy: commit` killing in-flight calls mid-test.
     See [[render-autodeploy-kills-live-calls]].
  9. **Low** — compile RNNoise / get `webrtc-noise-gain` MSVC build working on Windows.
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
