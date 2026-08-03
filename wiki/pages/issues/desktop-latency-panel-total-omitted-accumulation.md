---
title: Desktop latency panel's "estimated mouth-to-ear" total omitted block-accumulation wait
type: issue
status: open
sources: [decisions-log, active-backlog]
updated: 2026-08-02
---

The desktop page's "Live latency breakdown" panel and post-call "Last call summary" (both
added 2026-08-02, same session) were built to help debug the desktop path's real
mouth-to-ear latency. The panel's own math had a real bug the same day it shipped.

## Bug 1: total omitted the dominant term (fixed)

`updateLatencyPanel` (`frontend/desktop/desktop.js`) summed network RTT + GPU inference +
current playout buffer size into an "Estimated mouth-to-ear" total — but never included
the block-accumulation wait (`block_ms` + `context_ms`, the 400-720ms the converter holds
incoming audio before it has enough to run inference at all, depending on profile — see
[[audio-pipeline-latency-budget]]). This is the single largest term in the real budget.
Result: the panel showed ~100ms while the user's own stopwatch measured ~2s on the same
call — caught by the user cross-checking against a stopwatch rather than trusting the UI.

Fixed same day (commit `5ddbd03`): `block_ms`/`context_ms` were already arriving in every
Modal `stats` message, just never read by the frontend. Now shown as its own "Block
accumulation wait" row and included in the total, in both the live panel and the summary.

## Bug 2: "GPU inference (total)" field shows 0ms despite populated sub-fields (open)

Found later the same session via a live screenshot during the
[[desktop-input-frame-drops]] investigation: HuBERT/FAISS/RMVPE/Generator sub-fields all
populate correctly and sum to a real ~50ms, but the "GPU inference (total)" field itself
displays 0ms — a display bug isolated to how that one field is read/summed from the stats
payload, not a backend/Modal issue (the components clearly arrive fine). **Not yet
root-caused or fixed** — see `.agents/projects/active-backlog.md`.
