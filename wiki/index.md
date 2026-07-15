# Keira Wiki — Index

See [WIKI.md](WIKI.md) for the schema and maintenance workflows before adding to this
wiki. This index is the first stop for any query — find the page here, then drill in.

## Concepts (evergreen explanations)
- [audio-pipeline-latency-budget](pages/concepts/audio-pipeline-latency-budget.md) — full
  mouth-to-ear latency breakdown, stage by stage. Updated 2026-07-07 for the streaming
  rebuild + TRT migration (was stale on VAD/T4/raw-fallback claims before that).
- [adaptive-playout-buffer](pages/concepts/adaptive-playout-buffer.md) — the standing
  playout buffer: currently **0.25s** target/5s cap (as of `b38070c`), drop-oldest
  overflow, decoupled producer/consumer. Fifth distinct design this buffer has had. Has an
  **open bug** against the current design — see
  [[playout-buffer-gulp-drain-oscillation]].
- [buffering-history](pages/concepts/buffering-history.md) — the five-attempt,
  one-revert migration path that led to the current buffer design; check here before
  re-fixing an old bug.
- [rvc-cold-start](pages/concepts/rvc-cold-start.md) — Modal cold-start behavior
  (measured ~75s, not the assumed 8-30s) and a confirmed production incident where a lead
  heard raw voice for a whole call (historical — raw fallback no longer exists at all
  post-rebuild). The live worker moved from T4 to **L4** on 2026-07-03.

## Issues (open/resolved problems)
- [control-plane-security-and-call-ordering-audit](pages/issues/control-plane-security-and-call-ordering-audit.md)
  — **open for deployment verification.** Operator bearer auth, Twilio signature validation,
  Modal auth, managed worker lifecycle, two-phase outbound dialing, fail-closed SIP gating,
  and non-destructive setup are implemented locally; provider configuration and live calls remain.
- [adaptive-pitch-lock-rollout](pages/issues/adaptive-pitch-lock-rollout.md) — **open,
  deployed + field-confirmed 2026-07-14.** Fixed-shift pitch constant replaced with a
  per-call adaptive lock after the constant itself went stale; two live calls confirmed
  the lock math exactly. Open: an audible mid-call pitch jump never listen-tested before
  shipping, and the 208Hz target itself is a single old reference measurement.
- [playout-buffer-gulp-drain-oscillation](pages/issues/playout-buffer-gulp-drain-oscillation.md)
  — **open, found 2026-07-14.** The playout buffer's drain pattern lets it overshoot its
  0.25s target by 6-7x during sustained speech, three times in one 7-second utterance —
  plausible cause of a "voice gets blurred on long sentences" field report. Unrelated to
  the pitch-lock work shipped the same week.
- [tensorrt-migration](pages/issues/tensorrt-migration.md) — **merged to `main` 2026-07-07,
  already past Phase 1 into Phase 2 same day.** Migrated the Modal worker to 3 static-shape
  TRT engines on the L4 to re-enable RMVPE pitch tracking; Phase 1 C3 benchmark 66ms/68ms,
  Phase 2 (block 320ms, buffer 0.25s) benchmark 54ms/55ms median/p95 — but Phase 2 shipped
  without the week-long live soak its own gate called for. Same day, found+fixed a real
  audio bug (zeroed unvoiced-frame noise causing hissing/garbled consonants). Remaining:
  C4 (offline A/B WAVs), a fresh C5 listen test against the noise fix, and confirming the
  live Modal deploy is actually serving TRT (`/api/health` → `"engine": "trt"`).
- [livekit-sip-trunk-stale](pages/issues/livekit-sip-trunk-stale.md) — **open.** First live
  outbound call after the Modal worker came back up failed to dial (`404 object cannot be
  found`); trunk recreated via `/api/setup`, but the Twilio webhook step 401'd separately and
  the fixed call hasn't been retried yet.
- [modal-deploy-path-bugs](pages/issues/modal-deploy-path-bugs.md) — **resolved.** First real
  `modal deploy` of the streaming rebuild failed twice: a stale folder-name reference, then a
  sibling module Modal never bundled into the container.
- [sip-audio-mixing-isolation-bug](pages/issues/sip-audio-mixing-isolation-bug.md) —
  **open for fail-closed verification.** The protobuf field bug is fixed; the current code
  gates outbound dialing and inbound bridging on confirmed server-side isolation.
- [voice-identity-mismatch-investigation](pages/issues/voice-identity-mismatch-investigation.md)
  — **pitch axis superseded, clarity axis REOPENED 2026-07-14.** Originally two independent
  causes fixed `f748a89` on 2026-07-08: pitch overshoot (→ [[adaptive-pitch-lock-rollout]]
  now) and double noise-suppression (browser + server) stripping HF detail. Three field
  calls since (07-13 x2, 07-14) measured input spectral centroid back down to 250-360Hz —
  worse than the original 413Hz problem — likely the same browser-cache gotcha this page
  already documented, recurring; not yet confirmed by a hard-refresh test.
- [sip-leg-packet-loss-edge-pinning](pages/issues/sip-leg-packet-loss-edge-pinning.md) —
  **RESOLVED 2026-07-08** (field-verify pending). Lead heard clipped words: RTP loss on the
  LiveKit→Twilio leg because `{trunk}.pstn.twilio.com` resolves to Twilio US, a transpacific
  hop from Singapore. Re-pointed the trunk to the Singapore edge; dropouts 5→1 on the next
  call. Open drift trap: `/api/setup` drops the inbound `;edge=singapore` pin.
- [part-by-part-audio-investigation](pages/issues/part-by-part-audio-investigation.md) —
  **open for live acceptance.** The current bounded 100ms drain fix still needs a real call
  verification.
- [modal-render-region-mismatch](pages/issues/modal-render-region-mismatch.md) —
  **resolved 2026-07-03.** Render confirmed live in Singapore, colocated with Modal.
- [render-autodeploy-kills-live-calls](pages/issues/render-autodeploy-kills-live-calls.md)
  — **open, medium priority.** Every push to `main` redeploys and kills in-flight calls.
- [readme-latency-budget-contradiction](pages/issues/readme-latency-budget-contradiction.md)
  — **resolved historical doc bug.** The old per-request budget was removed by the streaming rebuild.

## Sources (ingested raw material)
- [latency-md](pages/sources/latency-md.md) — `LATENCY.md`
- [decisions-log](pages/sources/decisions-log.md) — `.agents/decisions/log.md`
- [subsystem-notes](pages/sources/subsystem-notes.md) — `.agents/context/subsystem-notes.md`
- [stack-and-rules](pages/sources/stack-and-rules.md) — `.agents/context/stack-and-rules.md`
- [active-backlog](pages/sources/active-backlog.md) — `.agents/projects/active-backlog.md`
- [readme-md](pages/sources/readme-md.md) — `README.md`

## Not yet ingested (candidates for next pass)
- `backend/pipeline.py`, `backend/converters/rvc.py`, `modal_deploy/worker.py` — the
  actual source code backing the above concept pages (currently cited by path, not
  independently summarized as sources).
- `.agents/context/identity.md` — dev persona / code style, likely low-value for this
  wiki's purpose (it's about agent behavior conventions, not project knowledge) but
  worth a look.
- Anything not yet written down: incident postmortems, call-quality feedback, RVC model
  training notes for the current brand voice.
