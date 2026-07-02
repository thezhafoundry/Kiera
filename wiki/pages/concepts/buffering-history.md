---
title: Buffering / playout migration history
type: concept
sources: [decisions-log, active-backlog]
updated: 2026-07-02
---

The playout buffering design has been rewritten several times, with **at least one full
revert cycle**, before landing on the current [[adaptive-playout-buffer]]:

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
6. `fe678d6` — replaced the one-shot pre-buffer with the current adaptive, per-session
   standing playout buffer ([[adaptive-playout-buffer]]).

**Why this page exists**: if a buffering bug resurfaces, the `.agents/decisions/log.md`
explicitly warns to check whether it's a regression of something already tried and
reverted here before re-deriving a fix from scratch. [[active-backlog]] flags this area
as high-risk for the same reason and requires re-running the spectral latency test
(LATENCY.md §3) after any playout timing edit — there is no automated regression test
for latency.
