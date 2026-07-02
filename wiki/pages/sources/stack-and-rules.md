---
title: .agents/context/stack-and-rules.md
type: source
sources: [../../../.agents/context/stack-and-rules.md]
updated: 2026-07-02
---

[.agents/context/stack-and-rules.md](../../../.agents/context/stack-and-rules.md) —
tech stack inventory, hard invariants, and file map.

## Key claims
- Stack: FastAPI backend on **Render** (service `Kiera`,
  `srv-d92lh7navr4c738i03a0`, **Oregon/us-west**); LiveKit Cloud (room + SIP) + Twilio
  (Elastic SIP Trunk + PSTN number); RVC v2 on a serverless **Modal** T4 GPU worker
  pinned `region="ap-southeast"`; noise suppression via WebRTC/RNNoise (degrade to
  passthrough); vanilla HTML/ES6 frontend; RVC v2 model trained externally via the
  vendored `RVC/` WebUI, weights uploaded to a Modal volume, never committed.
- Hard invariants: one-way conversion only (agent→lead); never drop call audio
  (fail-safe raw fallback on timeout); fixed sample-rate contract (16kHz in, 48kHz RVC
  out, 960-byte/10ms published frames); noise suppressor frame contract (exactly 320
  bytes); playout sequence numbers never reset mid-call; don't push to `main` mid-call
  (Render auto-redeploys); never commit `.env`/model files.
- Restates the Render/Modal region mismatch as a hard fact to check before trusting the
  region comment in `modal_deploy/worker.py`. See [[modal-render-region-mismatch]].
