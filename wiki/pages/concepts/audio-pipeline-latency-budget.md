---
title: Audio pipeline latency budget
type: concept
sources: [latency-md, subsystem-notes]
updated: 2026-07-16
---

The current path is one long-lived, ordered WebSocket stream per call. The browser sends
16kHz mono PCM in 20ms frames; RVC accumulates a baseline 320ms block with 400ms context,
converts it on Modal, applies an 80ms SOLA overlap, and returns 48kHz PCM. The backend holds
a 250ms standing playout target and publishes 10ms frames. Raw/unconverted audio is never a
fallback: a converter outage produces silence or blocks/ends the call.

| Stage | Current evidence |
|---|---|
| Browser/LiveKit ingress | Not isolated in the 2026-07-16 run; requires production test |
| Input pairing | Up to 20ms |
| RVC accumulation/context | 320ms new audio / 400ms context baseline |
| Warm L4 TensorRT inference | 50.75ms median / 51.61ms p95 over 30 live blocks |
| SOLA | 80ms baseline; finite-stream tail/duration behavior is under investigation |
| Converter wait | 1207.11ms median / 1358.56ms p95 in the laptop-origin run |
| Estimated network portion | 837.05ms median / 988.91ms p95 in that same route |
| Standing playout | 250ms target / 5s cap, steady drain in bounded 100ms chunks |
| SIP/PSTN egress | Not isolated in the current run |
| End-to-end mouth-to-ear | Not yet measured for this checkout |

The converter benchmark is useful for regressions and route comparisons, but it is not a
mouth-to-ear result. Do not simply add its wait time to the playout target: browser jitter,
LiveKit publication, queue age, SIP transport, and receiver playout must be measured along
the real path. The next authoritative test is stable vs AP-routed Modal from Render
Singapore, followed by a warm staff PSTN spectral/listen test.

The 9.6s baseline returned 211.46ms less output with zero reported drops. That duration
gate must be understood before lowering block sizes because accumulated drift can create
gaps, accelerated speech, or playout starvation during a long call. See
[[rvc-baseline-routing-and-duration]], [[adaptive-playout-buffer]], [[rvc-cold-start]], and
`.agents/context/subsystem-notes.md` (merged in from the former `LATENCY.md`,
2026-07-16) for the measurement procedure.
