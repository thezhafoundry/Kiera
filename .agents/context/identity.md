# Identity, Dev Persona & Code Style

## Who is working on this
Solo developer (Yogeshwanth2005) building Keira end-to-end: backend (FastAPI/Python
async pipeline), the Modal GPU worker, telephony wiring (Twilio + LiveKit SIP), and the
vanilla-JS frontend dashboard. Comfortable across the whole stack — no single area is
"unfamiliar territory" the way it would be on a team with specialists. Iterates directly
against a live Render deployment and real test calls rather than a heavy staging process,
so latency/config regressions tend to surface as "call sounded wrong" reports investigated
after the fact (see [[log]] and [[subsystem-notes]] for examples).

## Response Conventions
- Terse, technical, numbers-first — LATENCY.md is the house style: concrete measured
  values ("~75s", "580-750ms") over vague qualifiers, explicit root causes over guesses.
- When diagnosing a live issue, pull real evidence (Render logs, Modal `/health`, git
  history) before proposing a fix — don't speculate when the data is one command away.
- Prefer fixing root causes over adding defensive/fallback code, *except* where the
  pipeline's existing fail-safe philosophy already calls for it (never drop call audio —
  see [[stack-and-rules]]).

## Code Style Rules
- PEP 8 + type hints per [CLAUDE.md](../../CLAUDE.md); never block the event loop —
  offload to `asyncio.to_thread`/tasks.
- Commit message style in this repo: short imperative prefix (`Fix:`, `Feature:`,
  `Optimize:`, `Corrected:`) + specific description of the behavioral change, not the
  mechanism (e.g. "Replace ordered buffer with direct publish to eliminate 26s latency
  backlog" rather than "refactor buffer logic").
- No speculative abstractions — the pipeline's converter/noise-suppressor interfaces are
  the only two pluggable seams by design (see [[stack-and-rules]]); don't add more without
  a concrete second implementation driving it.
