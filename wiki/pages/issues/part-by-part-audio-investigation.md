---
title: "Part by part" audio investigation (post-rebuild)
type: issue
status: open
sources: [decisions-log, subsystem-notes, active-backlog]
updated: 2026-07-15
---

After the 2026-07-02 streaming rebuild shipped and went live, the user reported call audio
"breaking in between... hearing part by part voice" — and separately, that the converted
voice "was not the trained voice." Both turned out to have distinct, evidence-backed root
causes, found by pulling real Modal/Render production logs rather than guessing.

## Finding 1: unbounded Modal GPU container fan-out (cost issue, not audio)

The Modal dashboard showed **4 simultaneous `rvc-worker` containers** running. Root cause:
`fastapi_app` in [modal_deploy/worker.py](../../../modal_deploy/worker.py) had no
`max_containers` set. The in-process single-tenancy gate (`_session_active`) only enforces
"1 session" *inside one already-running container* — Modal's autoscaler doesn't know only
one container should ever exist, and was spinning up a new (paid) GPU container per
connection attempt that arrived while another was cold-starting (~75s, see
[[rvc-cold-start]]) or mid-call. **Fix**: `max_containers=1` added to the function
decorator.

## Finding 2: voice didn't match the trained model

Confirmed via Modal logs that a known-male agent was misdetected as female **twice** the
same day (F0=222Hz and F0=166Hz, both classified Female → `pitch_shift=0` applied instead
of the correct +12) by `_auto_detect_pitch`'s autocorrelation-based gender detector
(introduced in `a0f3c42` to replace a manual UI toggle, on the assumption it would be more
accurate). Feeding audio outside the trained model's pitch range produces a voice that
sounds like a different identity, not just mispitched. It also re-runs from scratch on
every WebSocket reconnect (the detected value is never reported back to/persisted by the
client), so a single call could even change identity mid-call. **Fix**: reverted
`backend/main.py`'s `_do_start_bot` to drive `pitch_shift` from the UI's `agentGender`
toggle again, instead of `-1` (GPU auto-detect).

## Finding 3: FAISS index re-read from disk on every inference call

The real cause of persistent ~3s-per-block latency. `RVC/infer/modules/vc/pipeline.py`
(vendored, third-party — not Keira's own code) calls `faiss.read_index(file_index)` then
`index.reconstruct_n(0, index.ntotal)` **unconditionally on every `vc_single()` call**.
Fine for the original WebUI's one-shot "convert a whole file" use case; catastrophic for
streaming, where it was running once per ~480ms audio block. Confirmed in production
`[Timing]` logs: `npy: 1.99s, f0: 0.00s, infer: 0.46s` — ~1.4-2.0s of the ~3.0-3.8s total
per block was this alone, reading a 221MB index file. **Fix**: rather than edit the
vendored file, `worker.py` monkeypatches `faiss.read_index` at container startup with an
`lru_cache`-backed wrapper that also caches the `reconstruct_n` result. The existing GPU
warm-up pass in `RVCEngine.startup()` naturally primes the cache before any real caller
connects. **Confirmed fixed**: post-deploy logs show `npy: 0.05s`, total
`run_conversion: ~0.46-0.59s` per block — a ~6x improvement.

## Finding 4: even at ~0.5s/block, still slower than real-time — the real fix was a buffer, not more speed

After Finding 3's fix, the pipeline still took ~460-590ms to convert each 320ms of new
audio (~1.5-1.8x real-time) — enough to keep gradually falling behind over a call, which is
why choppiness improved but didn't fully disappear. At this point the user reframed the
goal directly: **call latency is not a priority for this app, voice continuity is.** That
flipped the fix from "make it faster" to "absorb the slowness as delay instead of silence"
— see [[adaptive-playout-buffer]] (the standing playout buffer redesign) and
`modal_deploy/streaming.py`'s `BLOCK_MS`/`CONTEXT_MS` increase (320/160 → 1000/400, more
context per inference call, ~3x fewer SOLA crossfade seams per second).

## Considered and rejected: true streaming (no chunking at all)

Asked directly whether chunking could be replaced with genuine streaming. Answer: no, not
without swapping the underlying model. HuBERT (RVC v2's feature extractor) is
bidirectional/non-causal by architecture — it has no incremental/streaming mode, it
fundamentally needs a windowed chunk of audio. The current block+context+SOLA-crossfade
approach is also not a naive shortcut — it's the same technique RVC's own real-time
community tooling (w-okada's voice-changer) uses to make this model family behave in
near-real-time. A genuinely causal/streaming-native model (e.g. in the shape of Google's
StreamVC) would mean **retraining the Keira voice from scratch** on a different
architecture, not a pipeline code change — out of scope unless the buffer approach proves
insufficient.

## Status

Findings 1-3 were confirmed fixed via production evidence. The current consumer drains the
standing buffer in bounded 100ms chunks rather than gulping the entire buffer, but that change
still needs live-call verification — see [[active-backlog]].
