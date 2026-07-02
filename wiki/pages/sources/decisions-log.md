---
title: .agents/decisions/log.md
type: source
sources: [../../../.agents/decisions/log.md]
updated: 2026-07-02
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
  be colocated — **currently wrong**, Render is in Oregon. Status: unresolved as of
  2026-07-02. See [[modal-render-region-mismatch]].
