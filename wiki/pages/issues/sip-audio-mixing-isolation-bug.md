---
title: SIP audio isolation silently failing — lead heard raw+converted voice mixed
type: issue
status: open
sources: [decisions-log, subsystem-notes, active-backlog]
updated: 2026-07-15
---

User reported a live call where the voice "was not clear and it was breaking" while
speaking. Unlike the [[part-by-part-audio-investigation]] (a pipeline/latency problem),
this turned out to be a completely different bug: the lead was hearing **two
simultaneous copies of the same speech** — the agent's raw mic *and* the bot's RVC-converted
track — mixed together by LiveKit's SIP bridge for the entire call.

## Background: why a "SIP isolation" step exists at all

LiveKit's SIP bridge runs server-side and ignores browser-level
`setTrackSubscriptionPermissions()` — the SIP participant (the lead, over the phone) gets a
mix of *every* audio track published in the room by default, not just the one it should
hear. A fix for this (`_restrict_sip_audio` in
[backend/main.py](../../../backend/main.py), commit `1324fe2`, "made changes for getting
mixed voices") had already been written and deployed *before* this session started: a
fire-and-forget background task that finds the SIP participant and the agent's raw track,
then calls LiveKit's `update_subscriptions` to unsubscribe the SIP leg from the raw track.

## Root cause: wrong protobuf field name, 100% failure rate

The fix itself never worked. It called:

```python
api.UpdateSubscriptionsRequest(
    room=room_name,
    participant_identity=resolved_sip_identity,   # wrong field name
    track_sids=raw_agent_track_sids,
    subscribe=False,
)
```

`UpdateSubscriptionsRequest`'s real field is `identity`, not `participant_identity`
(confirmed directly: `livekit.protocol.room.UpdateSubscriptionsRequest.DESCRIPTOR
.fields_by_name` → `['room', 'identity', 'track_sids', 'subscribe', 'participant_tracks']`).
Every attempt — checked across every call sampled in Render's production logs
(`srv-d932m4cvikkc73belt1g`) since the fix was deployed — threw:

```
Protocol message UpdateSubscriptionsRequest has no "participant_identity" field.
```

after retrying 8 times. Not one call in the sampled log history shows the intended
`[SIP Isolation] ✅ ... unsubscribed` success line. So the exact mixing bug this helper was
built to prevent was live in production the entire time it was "fixed."

Note: a *different* message, `CreateSIPParticipantRequest` (used for the outbound dial
itself, `main.py:527`), genuinely does have a field called `participant_identity` — that
call site was already correct and wasn't touched. The bug was specifically in the
`UpdateSubscriptionsRequest` call.

## Fix

Changed the one call site to `identity=resolved_sip_identity` (commit `cf60ca5`). Single
line change — the detection logic (finding the SIP participant and the raw track SIDs) was
already working correctly per the logs, so no other rework was needed.

## Side finding: CLAUDE.md had drifted from `.agents/`

While cross-referencing the pipeline's current behavior during this investigation, found
that `CLAUDE.md` (the top-level, always-loaded doc) still described the old one-shot 100ms
jitter buffer and claimed "no playout queue" — both false since the 2026-07-03 standing
playout buffer was reintroduced (see [[adaptive-playout-buffer]]). The commit that synced
`.agents/` and this wiki for that change (`1b35d59`) never touched `CLAUDE.md`. Fixed by
updating `CLAUDE.md`'s "Audio Pipeline & Streaming" section in place to match
`.agents/context/subsystem-notes.md`.

## Historical fix status

**The protobuf field fix was confirmed live 2026-07-03.** The fix was pushed and deployed, and Render logs
(`srv-d932m4cvikkc73belt1g`) now show `[SIP Isolation] ✅ 'sip-lead' unsubscribed from raw
agent tracks [...]` on every outbound call sampled since (15:05:27, 15:07:21, 16:10:22,
17:05:19, 17:07:43) — zero failure lines in that window.

## Current fail-closed gate

The checkout now requires the agent track before outbound dialing, then waits for
`_restrict_sip_audio` to succeed before reporting the call as initiated. Inbound callers remain
held until the same isolation event is confirmed. Keep this issue open until those gates are
deployed and verified on real inbound and outbound calls.
