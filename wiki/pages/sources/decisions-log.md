---
title: .agents/decisions/log.md
type: source
sources: [../../../.agents/decisions/log.md]
updated: 2026-07-15
---

[.agents/decisions/log.md](../../../.agents/decisions/log.md) — the agent's terse
architecture-decision and migration history for Keira.

## Key claims
- **2026-07-15 control-plane hardening**: operator routes require a bearer token, Twilio
  callbacks require signature validation, Modal conversion endpoints require the RVC key,
  worker tasks are managed explicitly, outbound dialing is gated until the worker and agent
  audio are ready, inbound bridging is gated on confirmed SIP isolation, and setup is
  non-destructive. Live provider configuration and acceptance tests are still pending.
- **2026-07-15 playout correction**: the consumer now drains bounded 100ms chunks after the
  initial target fill, avoiding whole-buffer gulping during sustained speech.
- Pipeline/architecture migration history: direct-publish → one-shot pre-buffer →
  discard-fail-safe-during-prebuffer → remove `age_before` check + 4s timeout fallback →
  adaptive standing playout buffer → 2026-07-02 streaming rebuild (persistent WS, no
  playout buffer) → 2026-07-03 standing playout buffer reintroduced (~3s target) →
  2026-07-07 TRT migration phase 1 (1.25s target, partially reverses the 2026-07-03
  "latency not a priority" decision). See [[buffering-history]].
- Four full revert/re-fix cycles before the current design — flagged as high-risk.
- Explicit rejected alternatives: bidirectional conversion, fixed larger pre-buffer, longer
  RVC budget, causal/streaming-native model.
- **Modal region**: `ap-southeast` pin confirmed correct; Render colocated (Singapore,
  resolved 2026-07-03). See [[modal-render-region-mismatch]].
- **2026-07-03 additions**: `max_containers=1`, pitch auto-detection reverted to manual UI,
  FAISS index monkeypatch cache, latency-for-quality tradeoff (standing buffer + bigger
  blocks).
- **2026-07-05**: TensorRT migration planned; phased playout-buffer reversal approved
  (1.25s Phase 1, 0.25s Phase 2 benchmark-gated). Different model implementing; resident
  agent reviews.
- **2026-07-07**: TensorRT migration merged to `main` (`9c1093a`). C3 benchmark passed
  (median 66ms). C4/C5 and live deploy confirmation pending.
- **2026-07-03 (later)**: SIP audio isolation field-name bug root-caused (wrong protobuf
  field), fixed, and confirmed live.
- **2026-07-03 (later still)**: voice-identity mismatch investigation — five hypotheses
  ruled out; `convert_file_chunked` diagnostic tooling built; the current temporary
  `_DEBUG_SAVE_AUDIO` flag remains to be disabled after field verification (tracked in
  [[active-backlog]]); GPU tier found stale (T4 deployed despite L4 committed).
