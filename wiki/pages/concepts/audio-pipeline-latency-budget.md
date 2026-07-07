---
title: Audio pipeline latency budget
type: concept
sources: [latency-md, subsystem-notes]
updated: 2026-07-07
---

**This page describes the pre-2026-07-02 VAD-chunked design and is now superseded** — see
[[buffering-history]]. The 2026-07-02 streaming rebuild deleted VAD chunking entirely
(`webrtcvad` is no longer imported anywhere in `backend/pipeline.py`), replaced the
per-chunk HTTP-with-timeout RVC call with a persistent `/ws` duplex stream, and — load-
bearing for anyone reading only this page — **deleted the raw-voice fallback path
structurally, not just avoided it**. There is no longer any condition under which the lead
hears the agent's original, unconverted voice: on any conversion failure the pipeline
publishes silence and recovers when real converted audio resumes. See CLAUDE.md "Audio
Pipeline & Streaming" (Fail-CLOSED) and [[rvc-cold-start]] for what a cold GPU does instead
(the call is blocked from starting, not degraded to raw voice).

Mouth-to-ear latency in [backend/pipeline.py](../../../backend/pipeline.py) is currently:

| Stage | Duration | Driver |
|---|---|---|
| Ingress network | 20–45ms | Browser→LiveKit→worker at 16kHz |
| Noise suppression | 0.2–0.5ms | `WebRTCNoiseSuppressor`, 10ms/320-byte frames |
| Input frame batching | up to 20ms | `_frame_pairs()` pairs two 10ms denoised frames into one 20ms input frame; no VAD, no chunk cutting |
| Inference block accumulation | up to 1000ms + 400ms context | `modal_deploy/streaming.py::BlockAccumulator` (`BLOCK_MS`/`CONTEXT_MS`, widened from 320/160 on 2026-07-03) over the persistent `/ws` stream — not a per-request HTTP call |
| GPU inference (warm) | TRT: median 66ms/p95 68ms per block (live L4, 2026-07-06 C3 benchmark); legacy path: ~250ms target, not live-measured | `RVCEngine`/`TRTVoicePipeline` on the Modal **L4** worker (was T4 until 2026-07-03); no per-chunk timeout/budget exists anymore |
| Standing playout buffer | 1.25s target/5s cap (was ~3s/5s 2026-07-03 → 2026-07-07) | See [[adaptive-playout-buffer]] — now the dominant term |
| Egress network | 20–40ms | 960-byte/10ms frames → LiveKit → browser |

This replaces the old VAD-chunking/2000ms-budget/raw-fallback table this page used to show —
see [[readme-latency-budget-contradiction]] for that specific stale-number's history. Steady
state total is currently a design estimate of **~1.3–1.6s**, dominated by the playout buffer,
not GPU inference; no live spectral-tone measurement has been run against this exact
(buffer + TRT) configuration — see LATENCY.md's top-of-document banner.

To re-measure after a pipeline change, use the built-in spectral tone test
([LATENCY.md §3](../../../LATENCY.md)), not assumptions from code comments — several
numbers in this project's own code comments (8–30s cold start) turned out to be wrong
when measured live (75s+). There is no automated latency regression test; this is
tracked as tech debt in [[active-backlog]].
