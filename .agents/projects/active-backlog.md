# Active Roadmap & Technical Debt

## Backlog
| Task | Priority | Status |
|---|---|---|
| `TWILIO_AUTH_TOKEN` on Render appears stale/invalid — `POST /api/setup`'s Twilio-webhook-config step 401'd (`{"code":20003,"message":"Authenticate"}`) while its LiveKit-trunk step succeeded. Refresh the token in Render's env vars (or confirm it wasn't rotated in the Twilio console), then re-run `/api/setup` to confirm the webhook step also succeeds. | High | Open, identified 2026-07-03 (see [[subsystem-notes]]) |
| Confirm the retried outbound call actually succeeds end-to-end (no `404 object cannot be found` / no 500) now that the LiveKit SIP trunk was recreated and `TWILIO_SIP_TRUNK_ID` updated (locally and on Render) to the new `ST_kFVkcpf5j8vh`. If it connects, this is also a natural opportunity to verify the SIP-isolation fix and the playout-buffer fix on the same call (see the two rows below) rather than three separate live-call sessions. | High | Open, identified 2026-07-03 (see [[subsystem-notes]]) |
| `RVC/` folder: ~195 files are already committed to git despite the folder being intended as gitignored (too large for GitHub). `.gitignore` silently stopped matching it after the folder was renamed from `Retrieval-based-Voice-Conversion-WebUI` to `RVC` — now fixed prospectively, but the already-tracked files need an explicit `git rm -r --cached RVC/` (keeping the working-tree files) to actually stop tracking them. | Medium | Open, identified 2026-07-03 (see [[subsystem-notes]]) |
| **Flip `_DEBUG_SAVE_RAW_AUDIO` back to `False` in `modal_deploy/worker.py` and redeploy** once the voice-identity investigation concludes — currently `True` live, saving 30s of raw call audio per call to the Modal volume. Not meant to run indefinitely. | High | Open, identified 2026-07-03 (see [[subsystem-notes]]) |
| Voice-identity mismatch investigation: confirm whether `raw_call_audio.wav` (real call audio) converts correctly offline via `main_chunked --pitch 12` (explicit, not auto-detect). If yes, the pipeline is cleared end-to-end and the remaining suspects are live-only (WS transport/session mechanics); if no, the pipeline itself still has an unexplained gap. See [wiki/pages/issues/voice-identity-mismatch-investigation.md](../../wiki/pages/issues/voice-identity-mismatch-investigation.md). | High | Open, identified 2026-07-03 |
| Verify the 2026-07-03 standing-playout-buffer + bigger-block fix actually eliminates "part by part" audio on a real live call (deployed, not yet confirmed — the choppiness symptom itself, separate from the voice-identity investigation above) | High | Open, identified 2026-07-03 (see [[log]], [[subsystem-notes]]) |
| GPU tier is now L4 (an incidental side effect of an unrelated deploy) but all voice-identity diagnostics so far were run/compared on T4 — re-verify diagnostics on L4 if the investigation continues, or explicitly re-pin diagnostics to L4 for future comparisons | Medium | Open, identified 2026-07-03 (see [[subsystem-notes]]) |
| If GPU tier alone isn't sufficient for latency: ONNX export (or TensorRT) for the HuBERT/generator inference path — no retraining, but real export/verification effort; FAISS search and `pm` pitch estimation aren't neural nets so this only speeds up part of the pipeline | Low | Open, identified 2026-07-03, deferred pending the above (see [[log]]) |
| Avoid Render `autoDeploy: commit` killing in-flight calls mid-test — consider deploy hooks/manual deploy during active test sessions, or a drain/graceful-shutdown path for `VoiceConversionWorker` | Medium | Open, identified 2026-07-02 |
| Compile RNNoise / get `webrtc-noise-gain` MSVC build working on Windows dev machines so local dev doesn't silently run in passthrough mode | Low | Open — degrades gracefully so not urgent, but masks real noise-suppression behavior during local testing (see [[subsystem-notes]]) |

## Resolved
| Task | Resolved | Notes |
|---|---|---|
| Modal/Render region mismatch (Modal pinned `ap-southeast`, Render was in Oregon/us-west) | 2026-07-03 | Render confirmed live in Singapore via API (`srv-d932m4cvikkc73belt1g`) — colocated with Modal. Docs (this file, [[subsystem-notes]], [[stack-and-rules]]) were stale; no further action needed unless the region changes again. |
| Modal RVC worker had no `max_containers` cap — autoscaler could spin up paid GPU containers per connection attempt | 2026-07-03 | Fixed with `max_containers=1` on `fastapi_app` in `modal_deploy/worker.py`. See [[log]]. |
| GPU-side pitch/gender auto-detection unreliable in production | 2026-07-03 | Reverted to the manual UI `agentGender` toggle driving `pitch_shift`. See [[log]]. |
| FAISS index re-read from disk on every streaming inference call (~1.4-2.0s/block) | 2026-07-03 | Cached via a `faiss.read_index` monkeypatch in `worker.py`, no vendored-code edits. See [[log]]. |
| SIP audio isolation silently failing 100% of calls (`participant_identity` vs `identity` field-name bug) — lead heard raw+converted voice mixed | 2026-07-03 | Fixed, pushed, deployed, confirmed via `[SIP Isolation] ✅` on every call sampled since. See [[log]]. |

## Known Tech Debt
- Buffering/playout logic has now been reverted and re-implemented **five** times:
  `523e6d9` → `0a76fe1` → `2a20b3a` → revert → `fe678d6` (adaptive standing buffer) →
  2026-07-02 rebuild removed it for a one-shot 100ms jitter fill → 2026-07-03 reintroduced a
  (differently-designed, bounded/drop-oldest) standing buffer, this time as a deliberate
  latency-for-quality tradeoff, not a bug fix — see [[log]] 2026-07-03 entry and
  [[subsystem-notes]]. This area has a real history of regressions/reverts; treat playout
  timing changes as high-risk and re-run the spectral latency test (LATENCY.md §3) after any
  edit — the 2026-07-03 change has NOT had that manual test run against it yet, only
  automated unit tests (`backend/test_pipeline.py`).
- No automated latency regression test — LATENCY.md's spectral tone test is manual
  (two browser tabs). A change could regress mouth-to-ear latency without any CI signal.
- `RVC/` vendored third-party WebUI checked into the repo tree (used for offline model
  training, not runtime) — large surface area unrelated to Keira's own code; worth
  confirming it's excluded from anything that scans/lints the whole repo.
