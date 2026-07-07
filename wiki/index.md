# Keira Wiki — Index

See [WIKI.md](WIKI.md) for the schema and maintenance workflows before adding to this
wiki. This index is the first stop for any query — find the page here, then drill in.

## Concepts (evergreen explanations)
- [audio-pipeline-latency-budget](pages/concepts/audio-pipeline-latency-budget.md) — full
  mouth-to-ear latency breakdown, stage by stage. Updated 2026-07-07 for the streaming
  rebuild + TRT migration (was stale on VAD/T4/raw-fallback claims before that).
- [adaptive-playout-buffer](pages/concepts/adaptive-playout-buffer.md) — the current
  (2026-07-07, TRT phase 1) standing playout buffer: 1.25s target/5s cap, drop-oldest
  overflow, decoupled producer/consumer. Fourth distinct design this buffer has had.
- [buffering-history](pages/concepts/buffering-history.md) — the five-attempt,
  one-revert migration path that led to the current buffer design; check here before
  re-fixing an old bug.
- [rvc-cold-start](pages/concepts/rvc-cold-start.md) — Modal cold-start behavior
  (measured ~75s, not the assumed 8-30s) and a confirmed production incident where a lead
  heard raw voice for a whole call (historical — raw fallback no longer exists at all
  post-rebuild). The live worker moved from T4 to **L4** on 2026-07-03.

## Issues (open/resolved problems)
- [tensorrt-migration](pages/issues/tensorrt-migration.md) — **merged to `main` 2026-07-07**
  (commit `9c1093a`). Migrated the Modal worker to 3 static-shape TRT engines on the L4 to
  re-enable RMVPE pitch tracking; C3 GPU benchmark passed live (median 66ms/p95 68ms vs.
  ≤400ms gate). Includes the playout-buffer reduction (~3s → 1.25s, now live). Remaining:
  C4 (offline A/B WAVs), C5 (listen test), and confirming the live Modal deploy is actually
  serving TRT (`/api/health` → `"engine": "trt"`).
- [livekit-sip-trunk-stale](pages/issues/livekit-sip-trunk-stale.md) — **open.** First live
  outbound call after the Modal worker came back up failed to dial (`404 object cannot be
  found`); trunk recreated via `/api/setup`, but the Twilio webhook step 401'd separately and
  the fixed call hasn't been retried yet.
- [modal-deploy-path-bugs](pages/issues/modal-deploy-path-bugs.md) — **resolved.** First real
  `modal deploy` of the streaming rebuild failed twice: a stale folder-name reference, then a
  sibling module Modal never bundled into the container.
- [sip-audio-mixing-isolation-bug](pages/issues/sip-audio-mixing-isolation-bug.md) —
  **resolved, confirmed live.** The "unsubscribe raw agent track from the SIP leg" fix
  (`_restrict_sip_audio`) had a wrong protobuf field name and failed silently on 100% of
  calls; fixed and now confirmed succeeding (`✅`) on every call sampled since.
- [voice-identity-mismatch-investigation](pages/issues/voice-identity-mismatch-investigation.md)
  — **open.** Converted voice doesn't match the trained voice on live calls (offline tests
  sound correct). Five hypotheses ruled out with real evidence (pitch, `index_rate`,
  chunking+SOLA, noise suppression, raw input quality); new reusable diagnostic tooling
  (`convert_file_chunked`/`main_chunked`) built along the way.
- [part-by-part-audio-investigation](pages/issues/part-by-part-audio-investigation.md) —
  **resolved 2026-07-03** (buffer fix not yet live-call-verified). Four distinct root
  causes found via production logs: Modal container fan-out, unreliable pitch
  auto-detection, FAISS index re-read-per-call, and a latency-vs-quality product tradeoff.
- [modal-render-region-mismatch](pages/issues/modal-render-region-mismatch.md) —
  **resolved 2026-07-03.** Render confirmed live in Singapore, colocated with Modal.
- [render-autodeploy-kills-live-calls](pages/issues/render-autodeploy-kills-live-calls.md)
  — **open, medium priority.** Every push to `main` redeploys and kills in-flight calls.
- [readme-latency-budget-contradiction](pages/issues/readme-latency-budget-contradiction.md)
  — **open, doc bug found by lint.** README says 5000ms, code/LATENCY.md say 2000ms.

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
