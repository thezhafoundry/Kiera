# Manual Dialer (replaces mock Leads Directory) — Design

**Date:** 2026-07-11
**Status:** Approved pending user review

## Goal

Replace the dashboard's hardcoded mock Leads Directory with a manual dialer: the agent
types a phone number, clicks Call (or presses Enter), and talks to the lead through the
existing outbound-call flow. The mock `LEADS` list is removed entirely — the dialer is
the only outbound-dial surface.

## What already exists (unchanged)

The entire call flow after "a phone number is chosen" is already built and stays
untouched:

- `startOutboundCall(phone, name)` (`frontend/app.js:272`) — POSTs
  `/api/call/outbound` with `{phoneNumber, agentIdentity, agentGender: 'male'}`, then
  joins the agent into the LiveKit room (mic publish, active-call console, timer, mute,
  end-call, latency badge).
- `POST /api/call/outbound` (`backend/main.py`) — fail-closed warm gate → LiveKit SIP
  dial via Twilio trunk → SIP-audio isolation. Accepts any E.164 `phoneNumber` string.
- **Zero backend changes.**

## Changes

### Removed (frontend)

- `frontend/app.js`: the `LEADS` mock array, `renderLeads()`, and the `renderLeads()`
  call in `init()`.
- `frontend/index.html`: the `lead-list` container and its "Select a lead…" copy inside
  the left glass card.
- `frontend/style.css`: `.lead-list`, `.lead-item`, `.lead-info`, `.lead-name-text`,
  `.lead-company-text`, `.call-action-btn` rule blocks (dead after the removal). The
  `.lead-card` card-shell rule is kept (the dialer card reuses it).

### Added (frontend)

**HTML** — the left glass card becomes the Dialer card:

- Heading "Dialer" + one-line helper copy.
- `<input type="tel" id="dial-input">` with placeholder `+91XXXXXXXXXX or 10-digit
  number`, autocomplete off.
- `<button id="btn-dial">📞 Call</button>` — reuses an existing button style from
  `style.css` (exact class picked at implementation from the variants already there,
  e.g. the one the old `.call-action-btn` visually matched).
- `<div id="dial-error">` inline error line (hidden by default).

**JS** (`frontend/app.js`):

- `normalizeDialNumber(raw) -> string | null`:
  1. Strip spaces, dashes, dots, parentheses.
  2. Starts with `+` and 8–15 digits follow → return as-is.
  3. Exactly 10 digits → prepend `+91` (default country code, per user decision).
  4. 12 digits starting with `91` → prepend `+`.
  5. Anything else → `null` (invalid).
- Dial handler on `#btn-dial` click and Enter in `#dial-input`: normalize; on `null`
  show inline error ("Enter a 10-digit number or full +country format") and do not
  call; on success clear the error and call `startOutboundCall(number, number)` — the
  dialed number doubles as the display name in the active-call console.
- The during-call button disabling at `app.js:511`/`527` switches from
  `.call-action-btn` (gone) to `#btn-dial`.

**CSS**: input styled to match the glass theme (dark translucent background, existing
border/focus accent variables); error line in the existing warning/danger color.

## Error handling

- Invalid input → inline error under the input, no network call.
- Backend/dial failures → unchanged, `startOutboundCall`'s existing catch + alert.

## Testing / verification

The repo has no JS test harness (vanilla frontend, manually verified — existing
convention). Verification: run `uvicorn backend.main:app`, open the dashboard,
(1) dial a 10-digit number → console shows `+91…` calling; (2) dial garbage → inline
error, no request; (3) confirm the dial button disables during an active call.
`python -m backend.test_pipeline` still passes (no backend change — sanity only).
