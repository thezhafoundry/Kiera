---
title: Desktop capture never got the 2026-07-08 double-noise-suppression fix
type: issue
status: resolved
sources: [decisions-log, active-backlog]
updated: 2026-08-02
---

The LiveKit/PSTN path (`frontend/app.js`) was fixed 2026-07-08 for a measured muffled-voice
regression — see [[voice-identity-mismatch-investigation]] — caused by the browser's
default `noiseSuppression`/`autoGainControl` stacking with the server-side
`WebRTCNoiseSuppressor(NS_LEVEL)`: input spectral centroid dropped from 720Hz (clean) to
413Hz, −9dB at 6-8kHz, propagating to −3dB presence loss in the converted output.

**The desktop path (`frontend/desktop/desktop.js`) never received the equivalent fix.**
Found 2026-08-02 while investigating an open-place/outdoor ambient-noise complaint on a
WhatsApp desktop test call — the user asked directly about raising `NS_LEVEL` to 3 to
fight background chatter, which surfaced that the desktop capture's `getUserMedia` call
still left the browser's own NS/AGC on by default, stacking with whatever server-side
suppression was already running.

## Fix

Aligned `desktop.js`'s `getUserMedia` constraints with `app.js` (commit `5ddbd03`):
`noiseSuppression: false`, `autoGainControl: false`, `echoCancellation: true` (kept on —
WhatsApp's own call audio plays through system speakers/headphones near this mic, so echo
protection still matters here specifically).

Raising `NS_LEVEL` to 3 was considered first and explicitly deferred in favor of this
lower-risk fix — stacking two suppression stages was already a known-bad pattern from the
2026-07-08 incident, independent of what level either individual stage runs at, so
removing the redundant stage was judged safer than making one stage more aggressive.

## Status

Fixed and deployed. **Not yet field-confirmed** whether this alone resolves the
ambient-noise complaint, or whether `NS_LEVEL` still needs raising afterward — that's a
separate, still-open decision pending a live retest in the same open-place environment.
