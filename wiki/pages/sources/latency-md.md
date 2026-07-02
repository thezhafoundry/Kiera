---
title: LATENCY.md
type: source
sources: [../../../LATENCY.md]
updated: 2026-07-02
---

[LATENCY.md](../../../LATENCY.md) — the canonical latency budget and troubleshooting doc
for the pipeline, last rewritten 2026-07-02 with live-measured numbers.

## Key claims
- Mouth-to-ear latency = ingress + noise suppression + VAD chunking + RVC conversion +
  egress. Steady-state (warm GPU): **~450–1400ms**. Cold GPU: 30–90s+ of raw voice before
  conversion kicks in.
- RVC conversion budget is **2000ms** (`asyncio.timeout`), deliberately shorter than a
  cold Modal start — see [[rvc-cold-start]].
- Cold start measured live at **~75s** with no `/health` response at all, not the
  8–30s the code comments assumed.
- Confirmed root causes of a "Modal not connecting" report (2026-07-02): Render
  redeploys killing in-flight calls, and cold start being much slower than assumed
  — see [[render-autodeploy-kills-live-calls]] and [[rvc-cold-start]].
- Documents a confirmed production incident: a lead heard raw (unconverted) agent
  voice for an entire call because every chunk timed out; fixed by adding
  `_wait_for_rvc_ready()` polling before dialing the lead. See [[rvc-cold-start]].
- §5 documents the ordered playout queue / adaptive standing buffer design — see
  [[adaptive-playout-buffer]].

## Contradiction flagged
States the RVC budget as 2000ms, which matches `backend/main.py` and `.agents/`, but
contradicts [[readme-md]]'s "5000ms" claim. See [[readme-latency-budget-contradiction]].
