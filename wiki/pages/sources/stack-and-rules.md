---
title: .agents/context/stack-and-rules.md
type: source
sources: [../../../.agents/context/stack-and-rules.md]
updated: 2026-07-03
---

[.agents/context/stack-and-rules.md](../../../.agents/context/stack-and-rules.md) —
tech stack inventory, hard invariants, and file map.

## Key claims
- Stack: FastAPI backend on **Render** (service `Kiera`, `srv-d932m4cvikkc73belt1g`, now
  **Singapore** — colocated with Modal, verified live 2026-07-03; service ID changed from
  the old Oregon deployment); LiveKit Cloud (room + SIP) + Twilio (Elastic SIP Trunk +
  PSTN number); RVC v2 on a serverless **Modal** T4 GPU worker pinned
  `region="ap-southeast"`, capped `max_containers=1` (2026-07-03); noise suppression via
  WebRTC (degrade to passthrough); vanilla HTML/ES6 frontend; RVC v2 model trained
  externally via the vendored `RVC/` WebUI, weights uploaded to a Modal volume, never
  committed.
- Hard invariants (current, post-2026-07-02-rebuild): one-way conversion only
  (agent→lead); **never publish raw/unconverted audio — fail CLOSED** (silence on outage,
  not a raw-audio fallback; the old "fail-safe raw fallback on timeout" invariant this page
  used to state was deleted structurally in the rebuild, not just avoided); fixed
  sample-rate contract (16kHz in, 48kHz RVC out, 960-byte/10ms published frames); noise
  suppressor frame contract (exactly 320 bytes); the conversion generator must always be
  torn down via `contextlib.aclosing` (no playout sequence numbers exist anymore to reset —
  that invariant is gone along with the old reorder-buffer design, see
  [[buffering-history]]); don't push to `main` mid-call (Render auto-redeploys); never
  commit `.env`/model files.
- Region mismatch — **resolved 2026-07-03**, Render confirmed colocated with Modal in
  Singapore. See [[modal-render-region-mismatch]].
