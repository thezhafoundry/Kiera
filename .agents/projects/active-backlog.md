# Active Roadmap & Technical Debt

## Backlog
| Task | Priority | Status |
|---|---|---|
| **RVC-first Gate R1**: make the Modal `ready` payload report model/profile/TRT geometry, remove the client's hardcoded 320 ms latency accounting, then capture one warm staff-PSTN baseline before changing block sizes. | P0 | Plan written 2026-07-16; local implementation pending credential rotation for live verification |
| **Pause LLVC training/deployment**: retain the verified streaming/safety tooling, keep `LLVC_PILOT_ENABLED=false`, and do not generate a 360-hour teacher corpus. Reassess zero-shot streaming VC only after RVC optimization. | P0 | Decision recorded 2026-07-16 |
| **Rotate the exposed Render MCP bearer token.** A live Authorization header was committed in tracked `.mcp.json` and is present in git history. Stripped from the working tree, but stripping the file does not revoke the credential. Revoke/rotate it in Render and decide whether shared history needs scrubbing. | P0 | Open, identified 2026-07-15 |
| **Secure the control plane**: set `KEIRA_CONTROL_TOKEN` on Render, require bearer auth for operator routes, validate Twilio signatures, and keep `CORS_ORIGINS` restricted. | P0 | Implemented locally; deployment/configuration pending |
| **Authenticate the Modal worker**: create the `rvc-api-key` Modal secret and redeploy the worker so `/convert` and `/ws` enforce `RVC_API_KEY`. | P0 | Implemented locally; secret/deploy pending |
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
| **Adaptive pitch lock**: deployed and field-confirmed 2026-07-14; listen-test the prior→locked pitch jump and revalidate `RVC_TARGET_F0=208`. | High | Open follow-up |
| **Input audio muffled again**: perform a genuine hard refresh before the next field call; static JS/CSS now receive no-cache headers locally. | High | Local cache-control fix implemented 2026-07-15; live hard-refresh verification pending |
| **Playout buffer overshoots and gulp-drains** during long continuous speech; consumer now drains bounded 100ms chunks. | High | Local fix implemented 2026-07-15; targeted live listen test pending |

## Resolved
| Task | Resolved | Notes |
|---|---|---|
| Modal/Render region mismatch | 2026-07-03 | Render confirmed live in Singapore, colocated with Modal. |
| Modal worker had no `max_containers` cap | 2026-07-03 | Fixed with `max_containers=1`. |
| GPU-side pitch/gender auto-detection unreliable | 2026-07-03 | Removed from the dashboard path; current UI now exposes the selected profile. |
| FAISS index re-read per inference call | 2026-07-03 | Cached via a `faiss.read_index` monkeypatch. |
| SIP audio isolation protobuf field-name bug | 2026-07-03 | Field fixed and confirmed in sampled logs; stronger fail-closed gating remains under P0 verification. |
| RTP packet loss on the LiveKit→Twilio SIP leg | 2026-07-08 | Re-pointed trunk/origination to Singapore edge; field verification continues. |
| Converted voice mismatch and muffled output | 2026-07-08 | Pitch and double-noise-suppression fixes shipped; later input-clarity regression remains open above. |

## Known Tech Debt
- Buffering/playout logic has been reverted and reimplemented repeatedly; treat timing edits as high-risk and rerun the spectral latency test in `LATENCY.md`.
- No automated latency regression test currently exists; the spectral tone test remains manual.
- `RVC/` is vendored third-party WebUI code and should remain outside normal runtime scans.
