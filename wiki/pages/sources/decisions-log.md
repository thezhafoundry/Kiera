---
title: .agents/decisions/log.md
type: source
sources: [../../../.agents/decisions/log.md]
updated: 2026-07-03
---

[.agents/decisions/log.md](../../../.agents/decisions/log.md) — the agent's terse
architecture-decision and migration history for Keira.

## Key claims
- Pipeline/architecture migration history (no DB, so this stands in for schema
  migrations): direct-publish → one-shot pre-buffer → discard-fail-safe-during-prebuffer
  → remove `age_before` check + 4s timeout fallback → adaptive standing playout buffer
  (current, `fe678d6`). See [[buffering-history]].
- The buffer/pre-buffer design went through **at least one full revert cycle**
  (`eb016f3`/`da46c48` "Reverted: Buffer Changed for voice issue") before landing on the
  current adaptive approach — flagged as a "check before re-deriving" trap.
- Explicit rejected alternatives recorded:
  - Bidirectional voice conversion (adds latency + distorts agent's own listening
    experience) — one-way agent→lead conversion was chosen instead.
  - A fixed, larger pre-buffer duration globally, instead of the adaptive per-session
    buffer — rejected because it would tax every call to cover the worst case.
  - A longer RVC conversion budget to ride out cold starts — rejected because it trades
    a worse failure mode (dead air) for a better one only in the rare cold-start case.
- Confirms the Modal region (`ap-southeast`) was pinned on the assumption Render would
  be colocated. **Resolved 2026-07-03**: Render confirmed live in Singapore, colocated.
  See [[modal-render-region-mismatch]].

## 2026-07-03 additions
- `max_containers=1` added to the Modal worker (autoscaler was spinning up extra paid GPU
  containers per connection attempt — the in-process single-tenancy lock doesn't prevent
  this at the infra level).
- Reverted GPU-side pitch/gender auto-detection back to the manual UI toggle — confirmed
  unreliable in production logs (misdetected a known-male agent as female twice).
- FAISS index caching (monkeypatch, not a vendored-code edit) — fixed ~1.4-2.0s/block of
  redundant disk I/O in the RVC conversion path.
- Explicit product decision: **call latency is not a priority, voice continuity is** —
  reverses part of the 2026-07-02 rebuild's latency-first framing. Drove a bigger
  `BLOCK_MS`/`CONTEXT_MS` and a standing playout buffer replacing the one-shot jitter fill.
  See [[part-by-part-audio-investigation]], [[adaptive-playout-buffer]].
- Considered and rejected: swapping to a causal/streaming-native model to eliminate
  chunking entirely — would require retraining the voice from scratch on a different
  architecture (HuBERT has no incremental mode); deferred unless inference-speed
  engineering (GPU tier, ONNX/TensorRT) proves insufficient.
