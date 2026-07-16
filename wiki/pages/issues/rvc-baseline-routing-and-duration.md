---
title: RVC baseline routing and duration gates
type: issue
status: open
sources: [latency-md, subsystem-notes, active-backlog, decisions-log]
updated: 2026-07-16
---

The RVC-first optimization work now has a reproducible baseline, but it is not ready for a
lower-latency profile yet. Modal v11 reports the baseline geometry (320ms block / 400ms
context / 80ms SOLA), TensorRT on an L4 with a hot cache, and a model/index fingerprint.
An authenticated 9.6s continuous WebSocket run returned 30 blocks with 50.75/51.61ms
median/p95 inference and no input, output, or connection drops.

Two results remain blocking:

1. The stream returned 9388.54ms from 9600ms input, a **-211.46ms duration delta**. The
   finite run can legitimately retain a SOLA tail, but the loss exceeds the nominal 80ms
   tail. Separate tail-flush behavior from unintended compression or missing output.
2. Converter wait was 1207.11/1358.56ms median/p95, including an estimated
   837.05/988.91ms network component. That run originated from a developer laptop, while
   the application origin is Render Singapore, so it cannot select the production route.

Modal exposes two edges from the same worker/model:

- Stable `fastapi_app`: Singapore compute (`ap-southeast`) with default US input routing.
- Experimental `fastapi_app_ap`: Mumbai input routing (`ap-south`) and broad AP GPU
  placement; its first observed container landed in Tokyo.

Render has **not** been switched from the stable endpoint. Benchmark both with identical
input from Render Singapore, then run one warm staff browser→LiveKit→converter→SIP/PSTN
spectral/listening test. Also repair the non-fatal `F0Predictor` startup-warm-up import
failure. Only after those gates pass should Candidate B (160/240/40/160) receive matching
TensorRT artifacts and a quality comparison. Candidate C is not implemented.

Related: [[rvc-cold-start]], [[audio-pipeline-latency-budget]],
[[modal-render-region-mismatch]], [[tensorrt-migration]], [[active-backlog]].
