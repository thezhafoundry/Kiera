# Historical Decisions & Migrations

## Migration Index
<!-- date | migration file | what changed -->
No formal DB/schema migrations in this project (no persistent datastore) — this section
tracks pipeline/architecture migrations instead:

| Date (approx, from git log) | Commit | What changed |
|---|---|---|
| — | `8aac8be` | Replaced ordered buffer with direct publish to eliminate a 26s latency backlog (later found insufficient on its own — see below). |
| — | `523e6d9` | Introduced a 1-second one-shot pre-buffer for smooth lead playback before streaming begins. |
| — | `0a76fe1` | Discard fail-safe (raw fallback) chunks during the pre-buffer window so the lead only ever hears the converted voice once playback starts. |
| — | `2a20b3a` | Removed an `age_before` check that was silently dropping/silencing lead audio; added a 4s pre-buffer timeout fallback. |
| 2026-07-02 | `6661db7` | Rewrote LATENCY.md with live-measured numbers and added the 4s timeout regression note. |
| 2026-07-02 | `fe678d6` | Replaced the one-shot pre-buffer with the current adaptive, per-session standing playout buffer described in LATENCY.md §5 / [[subsystem-notes]]. |

Note the `eb016f3`/`da46c48` "Reverted: Buffer Changed for voice issue" commits bracketing
this sequence — the buffer/pre-buffer design went through at least one full revert cycle
before landing on the current adaptive approach. If a buffering bug resurfaces, check
whether it's a regression of something already tried and reverted here before re-deriving
a fix from scratch.

## Decisions
- **One-way voice conversion only (agent→lead).** Why: the agent needs to hear the lead's
  real voice for natural conversation; only the outbound "brand voice" needs to be
  consistent. Rejected: bidirectional conversion (adds latency and distorts the agent's
  own listening experience for no product benefit).
- **Adaptive per-session playout buffer over a fixed one-shot pre-buffer.** Why: a fixed
  buffer only smoothed the start of a call; mid-call RVC slowdowns still produced gaps/
  reordering with no buffer left. Rejected: raising the fixed pre-buffer duration globally
  (would add latency to every call to cover the worst case instead of adapting per-session).
  See [[subsystem-notes]] for the mechanism.
- **2000ms RVC conversion budget, deliberately shorter than Modal cold-start time.** Why:
  guarantees the lead always hears *something* (raw voice fail-safe) within a bounded
  delay rather than silence while waiting out a 30-90s cold start. Rejected: a longer
  budget that would ride out cold starts, because it trades a worse failure mode (dead
  air) for a better one only in the cold-start case, at the cost of added latency risk on
  every call.
- **Modal region pinned to `ap-southeast`** on the assumption Render would be colocated.
  Status: **currently wrong** — Render is in Oregon. Not yet resolved; see
  [[active-backlog]]. Do not treat the region comment in `modal_deploy/worker.py` as
  authoritative without checking current Render region first.
