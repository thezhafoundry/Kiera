---
title: Render autoDeploy kills in-flight calls
type: issue
status: open
sources: [subsystem-notes, latency-md, active-backlog]
updated: 2026-07-02
---

Render's `autoDeploy: commit` setting means **every push to `main` triggers an
immediate redeploy** — `pip install` → new process → old process shutdown — which tears
down the LiveKit worker and any in-flight `VoiceConversionWorker` mid-call.

**Confirmed live (2026-07-02)**: Render logs for the `Kiera` service
(`srv-d92lh7navr4c738i03a0`) show this happening **twice within ~4 minutes** (04:58:40
and 05:02:22 UTC) during an active test call. Each redeploy forces the next call to
cold-start Modal from scratch (see [[rvc-cold-start]]) — this alone can look exactly
like "GPU never starts" if you're iterating on code and testing calls in the same
session, and was in fact mistaken for that before this root cause was traced.

**Workaround (no fix shipped yet)**: avoid pushing to `main` while a test call is
running, or expect to re-warm Modal (`POST /api/warmup`) after every deploy.

**Open in [[active-backlog]]** (Medium priority): consider deploy hooks/manual deploy
during active test sessions, or a drain/graceful-shutdown path for
`VoiceConversionWorker` so a redeploy doesn't hard-kill a live call.
