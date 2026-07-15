# Keira Control Plane and Session Close Implementation Plan

**Goal:** Secure the public control plane, make call startup and SIP isolation fail closed, make setup non-destructive, and provide a repeatable end-of-session audit command.

**Architecture:** Use one bearer token for operator-only HTTP routes, a WebSocket subprotocol handshake for the browser control channel (so the token is not in the URL), Twilio signature validation for Twilio-owned callbacks, and a small in-process rate limiter as a first guard. Split outbound calling into prepare/join/dial phases so the agent publishes audio before the SIP leg is created; inbound callers remain held until worker readiness and SIP isolation are confirmed.

**Tech Stack:** FastAPI, Pydantic v2, LiveKit API, Twilio RequestValidator, vanilla browser JavaScript, Python `unittest`, and a Makefile-backed Python session-close command.

## Global Constraints

- Never expose raw/unconverted audio; failures must stop or publish silence.
- Never log or return bearer tokens, Twilio auth tokens, or provider secrets.
- Do not delete unrelated LiveKit trunks or dispatch rules from `/api/setup`.
- Do not stage, commit, push, deploy, or revoke external credentials automatically.
- Preserve existing user changes in `.gitignore`, `.codex/`, `AGENTS.md`, and `graphify-out/`.

## Task 1: Control-plane auth, validation, and rate limiting

**Files:**
- Modify: `backend/main.py`
- Modify: `README.md`, `render.yaml`, `CLAUDE.md`, `AGENTS.md`
- Test: `backend/test_control_plane.py`

- [x] Add tests for bearer-token parsing, strict request validation, redaction, and rate limiting.
- [x] Add `KEIRA_CONTROL_TOKEN`/`CONTROL_PLANE_TOKEN` configuration, `Authorization: Bearer` verification, Twilio signature verification, bounded in-process rate limiting, and generic 401/403/429 errors.
- [x] Require operator auth on control routes; require Twilio signatures on inbound, wait, and status callbacks; keep health read-only and public.
- [x] Redact phone numbers and provider secrets from health responses and error payloads.
- [x] Run `python -m unittest backend.test_control_plane -v`.

## Task 2: Worker lifecycle, two-phase outbound dialing, and cleanup

**Files:**
- Modify: `backend/main.py`
- Modify: `frontend/app.js`
- Test: `backend/test_control_plane.py`

- [x] Replace FastAPI `BackgroundTasks` worker startup with managed `asyncio` tasks and startup events/errors.
- [x] Add cleanup that stops workers, cancels lifecycle tasks, removes process state, and deletes failed LiveKit rooms.
- [x] Make `/api/call/outbound` prepare the room and return an agent token without dialing; add authenticated `/api/call/outbound/dial` that dials only after the browser has published its agent track.
- [x] Make the frontend call the new dial endpoint after joining/publishing and call `/api/call/end` if browser connection fails.
- [x] Require worker-started/readiness state before inbound bridging and outbound dialing.

## Task 3: Fail-closed SIP isolation and safe setup reconciliation

**Files:**
- Modify: `backend/main.py`
- Modify: `wiki/pages/issues/sip-audio-mixing-isolation-bug.md`, `README.md`, `.agents/projects/active-backlog.md`
- Test: `backend/test_control_plane.py`

- [x] Make `_restrict_sip_audio` return a boolean, retry quickly, and expose an isolation event per call.
- [x] Gate inbound Twilio bridging on confirmed isolation; abort/clean up when isolation cannot be confirmed.
- [x] In the outbound dial phase, require an agent track before dialing and require isolation before returning success.
- [x] Change `/api/setup` to reuse/create only Keira-named resources and report conflicts without deleting unrelated resources.

## Task 4: End-of-session command

**Files:**
- Create: `scripts/session_close.py`
- Create: `Makefile`
- Modify: `.agents/context/identity.md`, `wiki/WIKI.md`, `README.md`
- Test: `backend/test_control_plane.py`

- [x] Add `make session-close` to run diff checks, wiki-link/frontmatter lint, stale-claim scans, secret-pattern scans, and a changed-file summary.
- [x] Keep the default command read-only; add explicit `--write-report` for an `.agents/session-reports/` handoff file.
- [x] Document the command as the normal final step and keep live Render/Modal/Twilio verification visibly separate.

## Verification

- [x] Run focused unit tests; existing pipeline tests are blocked here by missing `numpy`.
- [x] Run `make session-close` and inspect its complete output.
- [x] Run `python -m py_compile backend/main.py scripts/session_close.py`.
- [x] Run `git diff --check` and review the final diff without printing secrets.
