# RVC baseline and routing handoff — 2026-07-16

## Outcome

Keira is now following an RVC-first optimization path. LLVC training/deployment is paused
for the multi-client SaaS onboarding model, while its safety/test scaffolding remains.
Modal v11 was deployed and authenticated, and the baseline RVC WebSocket path was measured
before changing block geometry. Production Render routing was not changed.

## Code and deployment evidence

- `cfe7891`, `a379f59`, `d8e83c9`, `e90a1a1`: named RVC profiles, dynamic readiness/stats
  metadata, model/index fingerprinting, benchmark tooling, and adaptive-pitch interpolation.
- `7a24200`: parallel AP-routed Modal benchmark edge.
- Stable `fastapi_app`: baseline profile, `region="ap-southeast"`, default Modal input route.
- Experimental `fastapi_app_ap`: `routing_region="ap-south"`, broad `region="ap"`; first
  observed health container was Tokyo (`ap-northeast-1`).
- Both edges use the same model volume/code and cap at two containers per function, with one
  active streaming session per container.
- Render remains in Singapore on the stable endpoint. No production endpoint switch occurred.
- User confirmed credential rotation and Modal CLI authentication to workspace
  `thezhafoundry`; authenticated WebSocket readiness succeeded. No secrets are stored here.

## Live baseline result

Developer laptop → stable Modal WebSocket, one 9.6s continuous synthetic stream:

| Metric | Result |
|---|---:|
| Output blocks | 30 |
| Cold active readiness | 72,510.73ms |
| TensorRT inference median / p95 / max | 50.75 / 51.61 / 52.54ms |
| Converter wait median / p95 / max | 1207.11 / 1358.56 / 1394.83ms |
| Estimated network median / p95 / max | 837.05 / 988.91 / 1025.31ms |
| Output duration | 9388.54ms |
| Duration delta | -211.46ms |
| Input / output / connection drops | 0 / 0 / 0 |

The run confirmed TensorRT, the baseline 320/400/80 profile, and model fingerprint metadata.
It does not measure Render-origin latency or browser/PSTN mouth-to-ear latency.

## Verification already completed

- 32 Modal/DSP/profile/benchmark tests passed.
- Full `python -m backend.test_pipeline` passed in an environment that allowed localhost
  WebSocket binding.
- Modal startup and authenticated readiness were observed live.

## Remaining gates, in order

1. Diagnose the -211.46ms duration loss, separating expected finite-stream SOLA tail holdback
   from unintended time compression or dropped output.
2. Benchmark stable and AP-routed endpoints with identical input from Render Singapore.
3. Repair the non-fatal startup warm-up import failure for `F0Predictor`.
4. Run one warm staff-only browser → LiveKit → Render → Modal → SIP/Twilio → PSTN spectral and
   listening test, including the one-second adaptive-pitch transition.
5. Only then compile/verify matching Candidate B TensorRT artifacts and compare quality and
   latency. Candidate C is not implemented.

Do not infer production mouth-to-ear latency from the laptop converter benchmark, do not
switch Render to the AP edge without the Render-origin A/B, and keep
`LLVC_PILOT_ENABLED=false`.
