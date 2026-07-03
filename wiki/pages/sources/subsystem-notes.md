---
title: .agents/context/subsystem-notes.md
type: source
sources: [../../../.agents/context/subsystem-notes.md]
updated: 2026-07-03
---

[.agents/context/subsystem-notes.md](../../../.agents/context/subsystem-notes.md) —
load-bearing gotchas per subsystem, the "why" the code alone doesn't convey.

**2026-07-03 note**: this source page previously described a pre-2026-07-02-rebuild
pipeline (P95-adaptive buffer, `_run_playout`, `MIN_CHUNK_MS`, a 2000ms conversion budget)
that no longer exists in any form — it was stale from the wiki's original bootstrap
ingest. Corrected below to match the current `.agents/` file.

## Key claims
- **Pipeline/playout (current)**: the converter is driven as one long-lived duplex WS
  stream (`_run_conversion_stream`) — frames arrive strictly in order, so there is nothing
  to reorder (no sequence numbers, no `_run_playout`). Since 2026-07-03, converted output
  feeds a bounded standing playout buffer (~3s target/~5s cap, drop-oldest overflow)
  drained by a separate `_run_playout_consumer` task, replacing a one-shot 100ms jitter
  fill that only smoothed the start of a call. See [[adaptive-playout-buffer]] and
  [[part-by-part-audio-investigation]].
- `contextlib.aclosing(gen)` around the conversion generator is load-bearing, not
  optional — a bare `async for ... break` won't reliably call the generator's `aclose()`,
  which is what tears down `RVCStreamingConverter`'s WS connection and background tasks.
- **Modal worker**: cold start measured at ~75s with *no* `/health` response at all
  beforehand — much slower than the 8–30s the code comments assume. `RVCEngine.startup()`
  raises `RuntimeError("No FAISS index found.")` if the Modal volume's index is empty —
  looks like a hang, is actually a missing-model error only visible in
  `modal app logs rvc-worker`. See [[rvc-cold-start]].
- **FAISS index re-read from disk every call — fixed 2026-07-03**: the vendored
  `RVC/infer/modules/vc/pipeline.py` re-read and fully reconstructed a 221MB FAISS index
  on every conversion call (~1.4-2.0s of ~3s per streaming block). Fixed via a
  `faiss.read_index` monkeypatch in `worker.py`, no vendored-code edits. See
  [[part-by-part-audio-investigation]].
- **`max_containers` gap — fixed 2026-07-03**: the in-process single-tenancy lock only
  holds within one container; Modal's autoscaler was spinning up extra (paid) GPU
  containers per connection attempt. Fixed with `max_containers=1`.
- **Gender/pitch auto-detection unreliable — reverted 2026-07-03**: confirmed
  misdetecting a known-male agent as female twice in production logs. Reverted to the
  manual UI gender toggle driving `pitch_shift`.
- Region mismatch (`ap-southeast` pinned) — **resolved 2026-07-03**, Render confirmed
  live in Singapore, colocated with Modal. See [[modal-render-region-mismatch]].
- **Render deployment**: `autoDeploy: commit` redeploys on every push to `main`,
  confirmed live to have killed an in-flight call twice in ~4 minutes during a test
  session — symptoms indistinguishable from "Modal not connecting." See
  [[render-autodeploy-kills-live-calls]].
- **Windows dev**: `webrtc-noise-gain` silently degrades to passthrough if its native lib
  is missing — tests still pass, so a broken Windows dev setup can look identical to a
  working one. Check startup logs, not test results. (`webrtcvad` is gone entirely post-
  rebuild, not just degraded — it's an unused dependency now.)
