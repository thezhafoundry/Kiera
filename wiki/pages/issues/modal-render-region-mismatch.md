---
title: Modal/Render region mismatch
type: issue
status: resolved
sources: [decisions-log, subsystem-notes, stack-and-rules, active-backlog, latency-md]
updated: 2026-07-16
---

The Modal RVC worker ([modal_deploy/worker.py](../../../modal_deploy/worker.py)) is
pinned `region="ap-southeast"` (Singapore) on the assumption that Render/Twilio
infrastructure would be colocated nearby.

**Resolved 2026-07-03**: verified live via the Render API that the deployed Render service
(`Kiera`, `srv-d932m4cvikkc73belt1g`) is now in **Singapore** — colocated with Modal. The
service ID differs from the old `srv-d92lh7navr4c738i03a0`/Oregon deployment this page
originally described, consistent with the migration having actually happened at some point
without every doc (this page included) being updated to reflect it. All other pages/docs
that cited "Render is in Oregon" (decisions log, subsystem notes, stack-and-rules, active
backlog) have been corrected as of the same date.

This resolution concerns the Render **compute region**. A separate 2026-07-16 experiment
compares Modal's default US input route against an `ap-south` input edge with broad AP GPU
placement. It does not reopen the historical Render migration, and production has not been
switched. See [[rvc-baseline-routing-and-duration]].

**Original impact (historical, no longer applies)**: every RVC call was paying a
transpacific round trip on top of inference time, eating into the conversion latency
budget ([[rvc-cold-start]], [[audio-pipeline-latency-budget]]).

**Lesson for future doc maintenance**: this mismatch sat "confirmed open" across five
different docs for at least a day after it was actually fixed, because nothing prompted a
re-check of live infra state — it was only caught because an unrelated debugging session
(see [[part-by-part-audio-investigation]]) happened to query the Render API directly for
other reasons. Consider periodically re-verifying infra-state claims (region, service IDs)
against the live provider API rather than trusting docs indefinitely, especially for
claims with no automated check behind them.
