---
title: Desktop playout pacer had no way to recover from backlog once it existed
type: issue
status: resolved
sources: [decisions-log, active-backlog]
updated: 2026-08-02
---

Found 2026-08-02 while investigating a user-reported "voice is breaking up" complaint on
a WhatsApp desktop test call, in the same session that first fixed the cushion-underrun
breakup (see [[desktop-playout-pacer]]'s sizing history). After that cushion fix, the user
ran another test and reported a **steady ~4-second delay for the entire call**, with the
GPU confirmed warm beforehand (ruling out a one-time cold-start explanation).

## Root cause

`run_playout_consumer`'s pacer (`backend/desktop_audio.py`) had a self-correction for the
*dry* side only: if the buffer ran empty, the schedule re-anchored from `now` rather than
trying to catch up (see [[desktop-playout-pacer]] mechanism step 3). There was **no
equivalent for the backlog side** — once held audio exceeded the cushion for any reason
(one early network hiccup, first-inference slowness, anything), strict 1x-only pacing
meant the pacer could never drain faster than real time, so that delay became
**permanent for the rest of the call**, not a symptom that fades. A single early event
producing a multi-second backlog would sit there, unchanged, until the call ended.

This is architecturally distinct from — and more consequential than — the cushion-sizing
work happening in the same session: cushion size only matters if the pacer can recover
from occasionally overshooting it, and it couldn't.

## Fix

Added a bounded catch-up (commit `a9b2e64`): once backlog exceeds
`PLAYOUT_CATCHUP_THRESHOLD_BYTES` (0.75s), drain at `PLAYOUT_CATCHUP_RATE` (1.15x real
time, not 1x) until back at/below `PLAYOUT_CATCHUP_TARGET_BYTES` (0.4s), with hysteresis
between the two thresholds. The rate is deliberately mild — well under the audible
time-compression threshold that motivated strict real-time pacing in the first place
(the LiveKit path's analogous [[playout-buffer-gulp-drain-oscillation]] and this
project's own 2026-07-27/2026-08-02 "blurred voice"/breakup findings).

A new test, `test_backlog_above_catchup_threshold_shrinks_over_time`
(`backend/test_desktop_audio.py`), floods a 3-second backlog and asserts delivery is
measurably faster than strict 1x while staying well short of an unpaced dump. Worth
noting for anyone touching this code again: the test's own first-draft upper bound was
miscalculated (didn't account for `asyncio`/event-loop scheduling slack — the observed
rate ran closer to 1.33x than the configured 1.15x) and had to be corrected after
comparing against a standalone simulation of the same schedule-advance logic. "Existing
tests still pass" was not sufficient evidence the fix worked, since none of the prior 26
tests drove backlog large enough to exercise the new catch-up path at all — see
`.agents/decisions/log.md`'s 2026-08-02 entry and the linked personal-memory lesson for
the fuller writeup.

## Status

Fixed and deployed (commit `a9b2e64`). **Not yet field-confirmed** — needs a live call
where an early hiccup is deliberately induced or waited for, then watching "Playout
buffer" visibly recover instead of staying elevated for the whole call. A follow-up
session found the fix's effect was hard to distinguish on a later test because the
subsequent ~2s latency measured turned out to be within the expected floor for
`baseline`'s 720ms accumulation, not further evidence of a stuck-backlog recurrence — see
[[desktop-input-frame-drops]] for what that follow-up investigation actually found.
