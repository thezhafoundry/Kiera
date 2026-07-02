---
title: Audio pipeline latency budget
type: concept
sources: [latency-md, subsystem-notes]
updated: 2026-07-02
---

Mouth-to-ear latency in [backend/pipeline.py](../../../backend/pipeline.py) is the sum
of five stages. Steady-state total (warm GPU) is **~450–1400ms**; cold-GPU first chunks
fall back to raw voice for 30–90s+ (see [[rvc-cold-start]]).

| Stage | Duration | Driver |
|---|---|---|
| Ingress network | 20–45ms | Browser→LiveKit→worker at 16kHz |
| Noise suppression | 0.2–0.5ms | `WebRTCNoiseSuppressor`, 10ms/320-byte frames |
| VAD chunk collection | 450–700ms | `webrtcvad`-cut on 150ms silence or 700ms hard cap |
| RVC conversion (warm) | ~150–700ms | HTTP round trip to Modal T4, bounded by a 2000ms budget |
| Standing playout buffer | 400–1500ms, adaptive | See [[adaptive-playout-buffer]] |
| Egress network | 20–40ms | 960-byte/10ms frames → LiveKit → browser |

Two numbers were deliberately tuned against each other rather than independently:

- **`MIN_CHUNK_MS` was raised 250ms → 450ms** not to deepen buffering but because RVC's
  ~600–750ms fixed per-request overhead doesn't amortize over shorter chunks — "queue
  backed up" in the logs was a chunk-size problem, not a buffer-depth problem
  ([[subsystem-notes]]).
- **The 2000ms RVC conversion budget is deliberately shorter than Modal cold-start
  time** (30–90s+) so the lead always hears *something* (raw-voice fail-safe) within a
  bounded delay rather than silence. This is a documented, intentional tradeoff, not a
  bug to "fix" by raising the timeout — see [[rvc-cold-start]].

To re-measure after a pipeline change, use the built-in spectral tone test
([LATENCY.md §3](../../../LATENCY.md)), not assumptions from code comments — several
numbers in this project's own code comments (8–30s cold start) turned out to be wrong
when measured live (75s+). There is no automated latency regression test; this is
tracked as tech debt in [[active-backlog]].
