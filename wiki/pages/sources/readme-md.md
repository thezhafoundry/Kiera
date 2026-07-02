---
title: README.md
type: source
sources: [../../../README.md]
updated: 2026-07-02
---

[README.md](../../../README.md) — account setup and end-to-end run guide for Keira.

## Key claims
- High-level architecture diagram: Agent browser mic → LiveKit room → Python bot →
  WebRTC noise suppressor → RVC v2 on Modal T4 GPU → published converted track → LiveKit
  → Twilio SIP trunk → PSTN → lead phone.
- States accumulated chunk size as "300ms" and the fail-safe timeout budget as
  **"5000ms"**.
- One-way conversion (agent→lead only) restated, consistent with all other sources.
- Setup requires LiveKit Cloud, Twilio (Elastic SIP Trunk + Origination URI pointed at
  the LiveKit SIP domain), and Modal accounts; env vars documented in §3.
- §4 covers training an RVC v2 model externally and deploying the Modal worker.

## Contradiction flagged
The **5000ms** fail-safe budget here does not match [[latency-md]] and `.agents/`
(both say 2000ms, matching `backend/main.py`'s actual `budget_ms=2000.0`). This README
number appears stale. See [[readme-latency-budget-contradiction]].
