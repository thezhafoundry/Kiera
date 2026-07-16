---
title: README.md
type: source
sources: [../../../README.md]
updated: 2026-07-16
---

[README.md](../../../README.md) — account setup and end-to-end run guide for Keira.
Updated 2026-07-16 with the RVC-first rollout state and reproducible WebSocket benchmark.

## Key claims
- High-level architecture: agent browser mic → LiveKit room → Python bot → WebRTC noise
  suppressor → persistent WebSocket → RVC v2 on Modal **L4** GPU (optionally TensorRT) →
  standing playout buffer → published converted track → LiveKit → Twilio SIP trunk → PSTN.
- **Fail-closed, never raw**: no raw-voice fallback (removed structurally). On any
  conversion outage the bot publishes silence and recovers when real converted audio
  resumes.
- Operator routes require `KEIRA_CONTROL_TOKEN`; Twilio webhooks require signed callbacks;
  Modal conversion endpoints require the shared `RVC_API_KEY` secret. Outbound calls prepare
  the room before dialing, and inbound calls bridge only after readiness and isolation checks.
- The session-close check is available as `make second-brain-close`; `--write-report` creates a
  dated local handoff report without contacting providers.
- One-way conversion (agent→lead only) restated, consistent with all other sources.
- RVC baseline remains 320/400/80 with a 250ms playout target. Render remains on the stable
  Modal endpoint; the AP-routed endpoint is experimental and LLVC remains disabled.
- The first 9.6s live converter benchmark verified TensorRT and reported 50.75/51.61ms
  median/p95 inference, but also -211.46ms duration drift. It is explicitly labelled as a
  developer-laptop result, not production mouth-to-ear.
- Setup requires LiveKit Cloud, Twilio (Elastic SIP Trunk), and Modal accounts.
  Environment variable reference expanded to include `RVC_INDEX_RATE`, `RVC_WS_URL`,
  `RVC_KEEPWARM`, `CORS_ORIGINS`, `TWILIO_SIP_USERNAME`/`PASSWORD`, `SERVER_URL`,
  `MODAL_TOKEN_ID`/`SECRET`.
- The old "5000ms fail-safe budget" is gone — the mechanism it described no longer exists
  (see [[readme-latency-budget-contradiction]], resolved 2026-07-07).
