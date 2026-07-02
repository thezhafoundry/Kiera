---
title: Adaptive standing playout buffer
type: concept
sources: [subsystem-notes, decisions-log]
updated: 2026-07-02
---

`_run_playout` in [backend/pipeline.py](../../../backend/pipeline.py) separates
*producing* RVC conversions from *playing them out*, replacing an earlier design that
published every RVC result the instant it finished (causing reordering and gaps when a
chunk was slow).

**Two phases per speech session:**
1. **Filling** — accumulate contiguous ready chunks until an adaptive target is met.
2. **Draining** — publish chunks strictly in sequence; if the next expected chunk isn't
   ready, wait up to 600ms (`_REORDER_WAIT_S`) before skipping past it, bounding how
   long one slow RVC call can stall the whole call.

**Buffer target is adaptive, not fixed**: P95 of the last 20 RVC round trips × 1.2,
clamped to 400–1500ms, recomputed at the start of every speech session (not just once
per call). A session with a consistently fast GPU gets a smaller buffer; one with more
variance gets a deeper one automatically.

**Sequence numbers are never reset mid-call** — `stop_pipeline` (fired on
`track_unsubscribed`) only resets the buffering *phase*, not `_next_publish_seq` or
`_pending_chunks`. The dispatch counter runs for the worker's whole lifetime; resetting
the playout side would make it wait forever for sequence numbers already consumed.

**Known asyncio gotcha this design has to account for**: `asyncio.Condition.notify_all()`
wakes *every* waiter, not just the one whose data arrived. The wait for a specific
sequence number therefore loops against a wall-clock deadline
(`time.monotonic()`) instead of a single `await cv.wait()` — otherwise an unrelated
chunk resolving can look like your own wait timing out early, silently shrinking the
600ms reorder window.

This is the *current* (as of `fe678d6`) iteration of a design with real history — see
[[buffering-history]] for the migration path and the full revert cycle it went through.
