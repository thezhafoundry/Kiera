---
title: Buffering / playout migration history
type: concept
sources: [decisions-log, active-backlog]
updated: 2026-07-07
---

The playout buffering design has been rewritten **five** times now (effectively six with
the TRT-phase-1 reduction), with at least one full revert cycle, before landing on the
current [[adaptive-playout-buffer]]:

1. `8aac8be` — replaced an ordered buffer with direct publish to cut a 26s latency
   backlog. Later found insufficient on its own (caused reordering/gaps).
2. `523e6d9` — introduced a 1-second one-shot pre-buffer for smooth start-of-call
   playback.
3. `0a76fe1` — discarded fail-safe (raw fallback) chunks during the pre-buffer window
   so the lead only ever heard converted voice once playback started.
4. `eb016f3` / `da46c48` — **"Reverted: Buffer Changed for voice issue"** — the above
   approach was rolled back.
5. `2a20b3a` — removed an `age_before` check that was silently dropping/silencing lead
   audio; added a 4s pre-buffer timeout fallback.
6. `fe678d6` — replaced the one-shot pre-buffer with an adaptive, per-session, P95-sized
   standing playout buffer (sequence-numbered, reorder-wait based).
7. **2026-07-02 streaming rebuild** (`f3c16ed`, Tasks 1-5) — removed the adaptive buffer
   entirely, not just tuned. The new persistent-duplex-stream design made reordering a
   non-problem (frames arrive strictly in order), so it was replaced with a much simpler
   one-shot 100ms jitter fill.
8. **2026-07-03** — the one-shot fill proved insufficient: it only smoothed the *start* of
   a call, and any converter block slower than its real-time budget produced a silence gap
   mid-call ("part by part" audio — see [[part-by-part-audio-investigation]]). Reintroduced
   a standing buffer, but a differently-shaped one than step 6's: fixed ~3s target/5s cap
   (not P95-adaptive) and drop-oldest-on-overflow (not sequence-numbered reorder-wait) —
   an explicit latency-for-quality product tradeoff, not a bug fix.
9. **2026-07-07 (TRT migration phase 1)** — target reduced 3.0s → **1.25s** (floor for
   `BLOCK_MS=1000` plus jitter headroom), cap stayed at 5s. This partially reverses the
   2026-07-03 "latency is not a priority" decision — the TRT C3 benchmark (median 66ms/p95
   68ms on a live L4) showed enough GPU headroom to shrink the cushion. Phase 2 (0.25s +
   smaller blocks) is separate and benchmark-gated — see [[tensorrt-migration]].

**Why this page exists**: if a buffering bug resurfaces, the `.agents/decisions/log.md`
explicitly warns to check whether it's a regression of something already tried and
reverted here before re-deriving a fix from scratch. [[active-backlog]] flags this area
as high-risk for the same reason and requires re-running the spectral latency test
(procedure in `.agents/context/subsystem-notes.md`) after any playout timing edit — there is no automated regression test
for latency, and as of 2026-07-07 that manual test has **still not** been re-run against
the current (step 9) design.
