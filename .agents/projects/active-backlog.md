# Active Roadmap & Technical Debt

## Backlog
| Task | Priority | Status |
|---|---|---|
| **RVC-first Gate R1 — finish production baseline**: metadata/profile/model fingerprint and dynamic client timing are deployed and the direct WebSocket baseline is captured; run the same benchmark from Render Singapore, then one warm staff-PSTN spectral/listen test before changing block sizes. | P0 | Partial: Modal v11 verified 2026-07-16; Render/PSTN measurements pending |
| **Resolve RVC duration loss**: the 9.6s baseline returned 9.38854s (-211.46ms) despite zero queue/connection drops. Separate expected SOLA tail holdback from additional time compression and fix before promoting a lower-latency profile. | P0 | Open, measured 2026-07-16 |
| **A/B Modal routing from the real origin**: compare legacy `fastapi_app` (`ap-southeast` compute/default US routing) with `fastapi_app_ap` (Mumbai routing/broad AP; first container landed Tokyo) from Render Singapore. Keep Render on legacy until the result is recorded. | P0 | Experimental endpoint deployed; production switch not made |
| **Repair the nominal GPU warm-up**: Modal startup logs `No module named 'infer.lib.infer_pack.modules.F0Predictor'`; startup continues and TRT becomes ready, but the intended inference warm-up is skipped. | High | Open, observed on v10/v11 startup |
| **Benchmark Candidate B only after Gate R1**: `candidate_b` geometry exists (160/240/40/160) but matching ONNX/TRT artifacts and quality evidence do not. Candidate C (120/200/20/120) is not implemented. | High | Blocked on baseline routing/duration/PSTN gates |
| **Pause LLVC training/deployment**: the prototype was fully removed from `main`/this tree (2026-07-16) and set aside on branch `codex/llvc-pilot`; do not generate a 360-hour teacher corpus. Reassess zero-shot streaming VC only after RVC optimization, re-deriving scope from that branch. | P0 | Decision recorded 2026-07-16; code removed from main 2026-07-16 |
| Decide whether git history containing the old Render MCP bearer token requires scrubbing. The credential itself was user-confirmed rotated on 2026-07-16. | Medium | Rotation complete; history decision open |
| **Secure the control plane**: set `KEIRA_CONTROL_TOKEN` on Render, require bearer auth for operator routes, validate Twilio signatures, and keep `CORS_ORIGINS` restricted. | P0 | Implemented locally; deployment/configuration pending |
| **Fix bot lifecycle and SIP gating**: outbound now prepares, joins, then dials; inbound waits for worker readiness and confirmed isolation; verify both with a live call. | P0 | Implemented locally; live verification pending |
| **Make `/api/setup` non-destructive**: it now reuses/creates only Keira-named resources and reports conflicts without deleting unrelated trunks/rules. | P0 | Implemented locally; live verification pending |
| `TWILIO_AUTH_TOKEN` on Render appears stale/invalid — `POST /api/setup`'s Twilio-webhook-config step 401'd while its LiveKit-trunk step succeeded. Refresh the token in Render's env vars, then re-run `/api/setup`. | High | Open, identified 2026-07-03 (see [[subsystem-notes]]) |
| Confirm the retried outbound call succeeds end-to-end after the SIP trunk recreation and new trunk ID. | High | Open, identified 2026-07-03 (see [[subsystem-notes]]) |
| `RVC/` folder: ~195 files are committed despite the folder being intended as gitignored. Remove unneeded tracked files with an explicit `git rm -r --cached RVC/` only after confirming the TRT shims and local changes. | Medium | Open, identified 2026-07-03; unblocked 2026-07-07 |
| **Disable per-call debug WAV capture** (`DEBUG_SAVE_AUDIO=0` + `modal deploy`) once voice identity/clarity is field-verified. | High | Safe default is now OFF locally (2026-07-15); confirm deployed worker env |
| Verify the 2026-07-08 voice identity/clarity fix; pitch is field-confirmed but input clarity regressed on later calls. | High | Partially resolved 2026-07-14 |
| **`/api/setup` must preserve the inbound `;edge=singapore` pin** on future reconciliation. | Medium | Open, identified 2026-07-08 |
| **`RVC_MALE_PITCH_SHIFT=7` is calibrated to one agent**; continue validating adaptive pitch behavior and the audible transition. | Medium | Open, identified 2026-07-08 |
| Verify the standing playout-buffer/bigger-block fix eliminates part-by-part audio on a live call. | High | Open, identified 2026-07-03 |
| Re-verify TRT live deployment, C4 offline A/B WAVs, and C5 listen test. | High | Open, identified 2026-07-07 |
| Avoid Render `autoDeploy: commit` killing in-flight calls mid-test; add drain/graceful shutdown later. | Medium | Open, identified 2026-07-02 |
| Compile RNNoise / get `webrtc-noise-gain` MSVC build working on Windows. | Low | Open |
| **Adaptive pitch lock**: one-second prior→locked interpolation is implemented, deployed in Modal v11, and covered by tests; listen-test the transition and revalidate `RVC_TARGET_F0=208`. | High | Live listening follow-up open |
| **Input audio muffled again**: perform a genuine hard refresh before the next field call; static JS/CSS now receive no-cache headers locally. | High | Local cache-control fix implemented 2026-07-15; live hard-refresh verification pending |
| **Playout buffer overshoots and gulp-drains** during long continuous speech. 2026-07-15 fix (bounded 100ms drain chunks) alone did not resolve it -- 2026-07-16 live call telemetry still showed the same oscillation (playout_buffer_bytes swinging 0 to 500-740ms repeatedly across one call). Root cause: the consumer had no wall-clock pacing of its own, only LiveKit backpressure, which has enough queue headroom to let backlogged chunks through faster than real time. Fixed with an explicit real-time pacer in `_run_playout_consumer` (see `docs/superpowers/plans/2026-07-16-playout-consumer-real-time-pacing.md`). | High | Local fix implemented 2026-07-16; live listen test pending |

## Resolved
| Task | Resolved | Notes |
|---|---|---|
| Modal/Render region mismatch | 2026-07-03 | Render confirmed live in Singapore, colocated with Modal. |
| Modal worker had no `max_containers` cap | 2026-07-03 | Cap added; current approved two-call configuration is `max_containers=2` per edge, one active stream per container. |
| Modal worker authentication deployment | 2026-07-16 | `rvc-api-key` secret exists; authenticated production WebSocket readiness succeeded on v10/v11. |
| GPU-side pitch/gender auto-detection unreliable | 2026-07-03 | Removed from the dashboard path; current UI now exposes the selected profile. |
| FAISS index re-read per inference call | 2026-07-03 | Cached via a `faiss.read_index` monkeypatch. |
| SIP audio isolation protobuf field-name bug | 2026-07-03 | Field fixed and confirmed in sampled logs; stronger fail-closed gating remains under P0 verification. |
| RTP packet loss on the LiveKit→Twilio SIP leg | 2026-07-08 | Re-pointed trunk/origination to Singapore edge; field verification continues. |
| Converted voice mismatch and muffled output | 2026-07-08 | Pitch and double-noise-suppression fixes shipped; later input-clarity regression remains open above. |

## Known Tech Debt
- Buffering/playout logic has been reverted and reimplemented repeatedly; treat timing edits as high-risk and rerun the spectral latency test (procedure in [[subsystem-notes]]).
- `scripts/rvc_stream_benchmark.py` now automates converter-path regression measurement, but it is not in CI and cannot replace the manual browser/PSTN mouth-to-ear spectral test.
- `RVC/` is vendored third-party WebUI code and should remain outside normal runtime scans.
