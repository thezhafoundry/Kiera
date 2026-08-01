# Causal Streaming Voice Conversion Engine — Migration Design

**Date**: 2026-08-01
**Status**: Approved for planning (see writing-plans handoff)
**Supersedes for this scope**: the 2026-07-03 "call latency is not a priority" decision
(`.agents/decisions/log.md`) — that decision remains the record of why the current RVC
pipeline is block/buffer-shaped; this doc records that the priority has since changed and a
new engine is being built to actually hit low-latency streaming, rather than re-tuning RVC's
existing block-size lever further.

## Why RVC cannot get here by tuning

RVC v2's non-causality is structural, not a config choice, and traces to three specific
components:
- **HuBERT** (content/feature extraction) — a non-causal transformer encoder; needs the
  full block in hand before feature extraction can start.
- **RMVPE** (pitch/F0 tracking) — also windowed, not per-sample causal.
- **The generator** (VITS/HiFi-GAN-style decoder) — currently block-based; more adaptable
  than the two above, but still needs to become frame-causal to complete the pipeline.

This project already tried the adjacent, cheaper lever — shrinking `BLOCK_MS` — and
reverted it for quality (`.agents/decisions/log.md`, 2026-07-03: 320ms→1000ms after smaller
blocks produced audible "part by part" audio; later retuned back to 320ms as a compromise,
see `.agents/context/subsystem-notes.md`). Sample-level streaming requires replacing these
three components, not shrinking their input window further.

## What stays the same

Per explicit decision: this migration keeps Modal as the GPU host and preserves the
existing hard invariants from `.agents/context/stack-and-rules.md`:
- One-way agent→lead conversion only; lead→agent stays bridged raw.
- Fail-closed: any conversion fault publishes silence, never raw or garbled audio.
- The `VoiceConverter` ABC contract (`backend/converters/base.py`) — the new engine is a
  new implementation of this interface, not a parallel pipeline.
- 16kHz mono PCM in, 48kHz PCM out (frame sizes may change internally; the ABC boundary
  contract does not).

The output of this migration is a new engine, not a modified RVC — internally referred to
below as the **Causal Voice Conversion (CVC) engine** to avoid implying it's still RVC.

## Phases

Each phase has its own gate. A failed gate stops the migration or sends it back to the
previous phase — it does not get silently waived to keep schedule.

### Phase 0 — Research spike (offline, no infra changes)

Survey pretrained causal/streaming bases for each replacement target:
- **Causal content encoder** (replaces HuBERT): streaming wav2vec2 variants, causal
  conformer encoders.
- **Pitch/prosody** (replaces RMVPE): either a causal F0 tracker, or an architecture that
  folds pitch implicitly into the content stream instead of a separate stage.
- **Causal generator/vocoder** (replaces VITS/HiFi-GAN decoder): frame-causal HiFi-GAN
  variants, or streaming neural codec decoders that can consume causal features and emit
  audio incrementally.

**Required per-candidate metric: algorithmic look-ahead delay.** Many published "streaming"
encoders are not fully causal — they use a small fixed look-ahead (commonly 40-60ms) to hold
quality. This number must be logged next to license terms for every candidate; it composes
directly into the mouth-to-ear budget the same way `BLOCK_MS` does in the current pipeline,
and a candidate with 60ms look-ahead is not directly comparable to one with 0ms.

**Output**: a comparison doc (candidates × license × obtainable weights × look-ahead ms ×
rough quality reputation) and a recommendation — no code.

**Gate**: at least one viable, license-clear, weights-obtainable candidate per component
(content encoder, pitch/prosody, generator). If no viable candidate exists for any
component, stop and fall back to the RVC block-size-reduction lever from the earlier
conversation instead of proceeding to Phase 1.

### Phase 1 — Component fine-tuning + offline quality validation

Fine-tune the Phase 0-selected bases on the existing training data for this agent's voice
(the same data used for the current `.pth`/`.index`).

**FAISS retrieval redesign.** RVC's FAISS step matches each frame's feature vector against
the full training-data index to pull exact target-speaker timbre. A literal per-20ms-frame
lookup against the full index causes two problems: GPU overhead scales with call length
instead of staying flat, and independent frame-by-frame lookups lack temporal consistency —
audibly, crackling, because adjacent frames can resolve to inconsistent nearest neighbors.
Design must pick one of:
1. **Pooled-window retrieval**: aggregate the last ~100ms of causal features before doing
   the lookup, trading a small amount of added delay (bounded, and known — log it like the
   look-ahead metric above) for consistency.
2. **Static speaker embedding**: bypass retrieval entirely; train the generator to condition
   on a fixed speaker embedding vector instead of a per-frame index lookup.
Phase 1 must pick and justify one of these two, not leave it open — this is a training-time
decision (it changes what the generator is trained to expect), not a runtime toggle.

**Offline validation harness**: a chunked-file test tool analogous to RVC's
`main_chunked` (`modal_deploy/worker.py`) — feeds a static WAV through the full causal
pipeline and produces output for listening, without any live infra. Used to A/B against
current RVC output on the same input files.

**Gate**: causal pipeline's voice-identity/naturalness passes a listen test against the RVC
baseline at a quality bar defined before testing starts (not judged post-hoc). If it fails,
this phase iterates (different base, different retrieval approach) before Phase 2 starts —
do not carry an unresolved quality question into infra work.

### Phase 2 — Modal serving integration

New converter class implementing `VoiceConverter`
(`backend/converters/base.py`) alongside a new Modal endpoint analogous to
`modal_deploy/worker.py`'s `/ws`, but **per-frame and stateful** instead of per-block: the
causal model carries hidden state across calls instead of `BlockAccumulator` gating on a
fixed block size.

**State management is a required subsection, not an implementation detail.** The current
`RVCStreamingConverter` reconnect logic (500ms drop-oldest input buffer,
`backend/converters/rvc_stream.py`) works safely today specifically because RVC is
stateless per block — dropping input frames on a hiccup just means those frames are never
converted, with no side effect on frames before or after. A causal model's hidden state
breaks that assumption: a desynchronized `h_t` after a dropped/reordered packet does not
fail cleanly, it can warp or corrupt subsequent output while looking superficially like
live audio, which the existing "publish nothing until real audio resumes" fail-closed
contract does not by itself catch (that contract currently only covers the "no audio
arrived" case, not "audio arrived but is desynced").

Required design elements for this subsection:
- A **sequencing/reset protocol** on the `/ws` frame format so the server can detect
  gaps/reordering in the frame stream (e.g. a monotonic sequence number per frame).
- An explicit **hidden-state reset** path: on a detected gap, drop and reinitialize the
  model's recurrent/attention state rather than continuing to decode against a stale state,
  publishing silence for the reset transient exactly as the existing fail-closed contract
  does for a full connection drop.
- A decision on how large a gap is tolerable before reset vs. before treating the whole
  session as failed (mirrors the existing reconnect-buffer-full / session-failed distinction
  in `rvc_stream.py`).

**Gate**: a direct WS benchmark (same shape as `scripts/rvc_stream_benchmark.py`) hitting
real per-frame latency targets with zero drops on a clean connection, **plus** a deliberate
packet-loss/reorder test exercising the reset path with no audible corruption (only clean
silence gaps at reset points).

### Phase 3 — Cutover strategy

Env-gated selection in `_do_start_bot` (`backend/main.py`), same pattern as the existing
`RVC_ENDPOINT_URL` switch — the new engine runs side-by-side with RVC behind a flag, not a
hard replace. Field-test on real calls before flipping the default. Rollback is flipping the
env var back; RVC stays in the codebase as the fallback path until CVC has field-proven
itself, not removed as part of this migration.

**Gate**: N consecutive field calls (number TBD by the user before Phase 3 execution) on the
new engine with no fail-closed-silence incidents attributable to state desync, and a
mouth-to-ear latency measurement using the existing spectral test procedure
(`.agents/context/subsystem-notes.md`) confirming the expected latency win was actually
realized.

## Explicitly out of scope for this doc

- Which specific candidate models win Phase 0 — that survey is Phase 0's job, not this doc's.
- Training data collection/expansion — assumed to reuse existing agent voice data unless
  Phase 1 finds it insufficient for the new architecture, in which case that's a Phase 1
  finding to report back, not a silent assumption here.
- Desktop bridge (`/desktop/`) integration — this doc covers the LiveKit/PSTN path only;
  the desktop path's own known pacing gaps (`.agents/context/subsystem-notes.md`) are a
  separate, already-tracked backlog item.
