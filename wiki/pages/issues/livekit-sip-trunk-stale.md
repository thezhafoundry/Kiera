---
title: LiveKit outbound SIP trunk went stale — dial failed with "object cannot be found"
type: issue
status: open
sources: [decisions-log, active-backlog]
updated: 2026-07-03
---

First live outbound-call attempt after the Modal worker came back up ([[modal-deploy-path-bugs]])
failed at the dial step, not the voice-conversion step — a different, unrelated bug.

## Symptom

Warm gate passed correctly (GPU confirmed ready, bot spawned with the new
`RVCStreamingConverter` — see `.agents/decisions/log.md`'s 2026-07-02 fail-closed-gate entry),
then `POST /api/call/outbound` 500'd:

```
livekit.api.twirp_client.TwirpError: TwirpError(code=not_found,
message=twirp error unknown: object cannot be found, status=404)
```

at `lk.sip.create_sip_participant(...)` in `backend/main.py`. The preceding trunk-lookup-by-name
call (`list_outbound_trunk`) had already succeeded and returned a trunk ID
(`ST_ruQpmqBLhYbj`) — so the trunk existed well enough to be *listed*, but LiveKit rejected it
at actual dial time as not found. Root cause not fully diagnosed (plausibly connected to
whatever infra changes happened around the Render→Singapore migration — see
[[modal-render-region-mismatch]] — but not confirmed).

## Fix applied

Re-ran `POST /api/setup` (`backend/main.py`), which is designed to be idempotently re-runnable:
it deletes the outbound trunk by name ("Keira Twilio Outbound") and recreates it fresh, same
for the inbound trunk + dispatch rule. Result:
- Outbound trunk: recreated as `ST_kFVkcpf5j8vh` (confirms the old `ST_ruQpmqBLhYbj` really was
  stale/orphaned — a different ID came back).
- Inbound trunk: `ST_U6rGLvrRy53H`, no conflicts found, reused cleanly.
- Dispatch rule: recreated as `SDR_AmuRZmcQCRE7` (old `SDR_2mZbMeutJgPj` deleted).
- `TWILIO_SIP_TRUNK_ID` updated to the new value both locally (`.env`) and in Render's
  environment variables (the app only does the by-name dynamic lookup as a *fallback* when this
  env var is unset — if it had stayed set to the old ID, today's fix wouldn't have taken effect
  without this env var update too).

## Still open

`/api/setup`'s Twilio-webhook-configuration step failed separately:
```
HTTP 401 {"code":20003,"message":"Authenticate", ...}
```
— meaning the `TWILIO_AUTH_TOKEN` configured on Render is currently invalid or was rotated in
the Twilio console without Render being updated. This did **not** block the trunk fix above, but
it does mean the inbound-call webhook was not re-verified/re-pointed by this run. Needs a manual
credential check in Render's dashboard. See [[active-backlog]].

The actual live outbound call has not yet been confirmed to succeed end-to-end after the trunk
ID update (session ended with the fix applied, retry pending). Whoever picks this up next should
check for a successful `POST /api/call/outbound` (no 500) as the first thing, then fall through
to the pipeline-level checks the other open issues already care about (never-raw invariant, SIP
audio isolation, playout smoothness) on the same call if it connects.
