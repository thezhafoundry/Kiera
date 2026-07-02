---
title: Modal/Render region mismatch
type: issue
status: open
sources: [decisions-log, subsystem-notes, stack-and-rules, active-backlog, latency-md]
updated: 2026-07-02
---

The Modal RVC worker ([modal_deploy/worker.py](../../../modal_deploy/worker.py)) is
pinned `region="ap-southeast"` (Singapore) on the assumption that Render/Twilio
infrastructure would be colocated nearby. The deployed Render service (`Kiera`,
`srv-d92lh7navr4c738i03a0`) is actually in **Oregon (us-west)**.

**Impact**: every RVC call currently pays a transpacific round trip on top of inference
time, eating into the 2000ms conversion budget ([[rvc-cold-start]],
[[audio-pipeline-latency-budget]]) and making timeouts (→ raw-voice fail-safe) more
likely under load, especially once the GPU is warm and inference itself is fast.

**Status**: confirmed open as of 2026-07-02 across every source that mentions it
(decisions log, subsystem notes, stack-and-rules, active backlog). Not yet resolved.

**Fix options on the table** (from [[active-backlog]], High priority):
- Re-pin the Modal function to a US region (matches Render's actual location), or
- Move the Render service to `ap-southeast` (matches Modal's current pin).

Do not treat the `region="ap-southeast"` comment in `modal_deploy/worker.py` as
authoritative without first checking Render's current region — the comment reflects the
original (wrong) assumption, not current reality.
