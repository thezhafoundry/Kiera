---
title: Control-plane security and call-ordering audit
type: issue
status: open
sources: [stack-and-rules, subsystem-notes, active-backlog, readme-md]
updated: 2026-07-15
---

The checkout now contains the first hardening pass for the previously unauthenticated control
plane. Live provider configuration and deployment still require verification.

## Implemented locally

- Operator routes require `Authorization: Bearer KEIRA_CONTROL_TOKEN`; the browser keeps the token
  in memory only. `/api/health` remains read-only and public.
- Twilio inbound, wait, and status callbacks validate `X-Twilio-Signature`.
- Modal `/convert` and `/ws` validate `RVC_API_KEY` from the `rvc-api-key` Modal secret.
- Outbound calls prepare a room, wait for the browser's agent track, then dial and confirm SIP
  isolation. Inbound calls remain held until worker readiness and isolation are confirmed.
- Worker startup uses managed tasks rather than FastAPI `BackgroundTasks`; failure cleanup removes
  worker/call state and deletes failed rooms.
- `/api/setup` only reuses/creates Keira-named resources and never deletes unrelated rules/trunks.
- `make second-brain-close` audits wiki structure, stale claims, credential patterns, and diff hygiene.

## Still required outside the checkout

- Set `KEIRA_CONTROL_TOKEN` in Render and create the Modal `rvc-api-key` secret.
- Deploy both services, revoke the previously exposed Render MCP credential, and run a live inbound
  and outbound call test.
- Replace process-local state/rate limiting with shared infrastructure before multi-instance scaling.
