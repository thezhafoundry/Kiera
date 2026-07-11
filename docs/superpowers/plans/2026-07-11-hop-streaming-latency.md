# Hop-Streaming Latency Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut Keira's added mouth-to-ear latency roughly in half (~720ms → ~370ms of pipeline-added delay; ~450-550ms total including network/PSTN) by converting the streaming DSP from 320ms-block inference to 160ms-hop sliding-window inference, without changing the RVC model, the TRT engines, or any quality-bearing component.

**Architecture:** The TRT engines take a fixed 720ms input window (`trt_pipeline.CANONICAL_IN = 11520` samples = `BLOCK_MS + CONTEXT_MS`). Today that window advances 320ms at a time (BLOCK_MS=320, CONTEXT_MS=400). The hop design advances the **same 720ms window** by 160ms per inference (BLOCK_MS=160, CONTEXT_MS=560) and emits only the newest 160ms of converted audio — halving the accumulation wait while *increasing* per-sample left context (560ms vs 400ms). Because `BlockAccumulator`, `trim_context`, `sola_crossfade`, the `/ws` handler, and `main_chunked` are all parameterized by `streaming.py`'s module constants, the core change is a constants+comments edit in `modal_deploy/streaming.py`; the TRT static shapes are untouched (no ONNX re-export, no engine rebuild). Two companion changes complete the latency win: SOLA crossfade 80ms→40ms (keeps the same 25% overlap-per-hop ratio; seam rate doubles to 6.25/s, gated by an offline listen test before deploy) and the backend playout cushion 0.25s→0.10s (converted audio now arrives in ~160ms hops with ~51ms compute, so the cushion only absorbs hop jitter, not a whole block interval).

**Latency budget after this change (design targets):** 160ms accumulation (worst case) + ~51ms TRT compute + 40ms SOLA holdback + 100ms cushion + ~20ms frame batching ≈ **371ms pipeline-added**, vs ~721ms today. GPU duty cycle rises 16%→32% (51ms compute per 160ms hop) — comfortable on the single-tenant L4.

**Tech Stack:** Python 3 stdlib + numpy only (no new dependencies). No TRT/ONNX changes.

## Global Constraints

- **`BLOCK_MS + CONTEXT_MS` must equal 720ms** (= `trt_pipeline.CANONICAL_IN` / 16000 · 1000 = 11520 samples) — the TRT static shapes depend on it. This plan keeps the sum at 720 (160+560); any other split requires re-exporting all three ONNX models, which is explicitly out of scope.
- Never touch the fail-closed conversion invariant (`.agents/context/stack-and-rules.md`) — no raw-audio path, silence on failure, unchanged.
- Preserve all message shapes: the `/ws` `"stats"` message keys are unchanged (`block_ms` will now report 160 — a value change, not a shape change); the frontend `{"pipeline_latency_ms","is_fallback"}` payload is unchanged.
- Sample-rate contracts unchanged: 16kHz in / 48kHz out / 960-byte published frames / 320-byte NS frames.
- No changes to `modal_deploy/trt_pipeline.py` logic or `modal_deploy/export_onnx.py` / `compile_trt.py` — comments only.
- The offline listen gate (Task 4) MUST pass before any production deploy (Task 5). Both Task 4 and Task 5 are **USER-RUN** (Modal/Render production actions).
- Style: match existing comment density/tone in each file; tests follow `backend/test_pipeline.py` / `modal_deploy/test_streaming.py`'s plain assert/print style (no pytest fixtures) where those files are touched, pytest style in `test_trt_pipeline.py`.

---

### Task 1: Hop geometry in `modal_deploy/streaming.py` (TDD)

**Files:**
- Modify: `modal_deploy/streaming.py:23-37` (constants + comment block)
- Test: `modal_deploy/test_streaming.py` (new tests), `modal_deploy/test_trt_pipeline.py` (new cross-module invariant test)

**Interfaces:**
- Produces: `streaming.BLOCK_MS == 160`, `streaming.CONTEXT_MS == 560`, `streaming.BLOCK_SAMPLES_IN == 2560`, `streaming.CONTEXT_SAMPLES_IN == 8960`, `streaming.SOLA_CROSSFADE_SAMPLES == 1920`. Everything downstream (`BlockAccumulator` defaults, the `/ws` handler, `main_chunked`, silence bypass, `trim_context` calls) picks these up automatically — no other code change.
- Consumes: `trt_pipeline.CANONICAL_IN` (11520, unchanged) for the invariant test. Safe to import in a GPU-free environment: `trt_pipeline`'s module level imports only numpy+time; GPU libs are deferred into `TRTVoicePipeline.__init__`/`_f0`.

- [ ] **Step 1: Write the failing tests**

Add to `modal_deploy/test_streaming.py`, after `test_sola_crossfade_seamless_at_boundary` and before `main()`:

```python
def test_hop_geometry_constants():
    print("\n--- Testing hop-streaming geometry constants ---")
    from modal_deploy import streaming as st
    from modal_deploy import trt_pipeline as tp

    # Hop-streaming (2026-07-11): 160ms hop + 560ms context. The inference
    # window total MUST stay equal to the TRT static input shape -- this is
    # the invariant that makes the hop change deploy-safe without an ONNX
    # re-export.
    assert st.BLOCK_MS == 160, f"hop must be 160ms, got {st.BLOCK_MS}"
    assert st.CONTEXT_MS == 560, f"context must be 560ms, got {st.CONTEXT_MS}"
    assert st.BLOCK_SAMPLES_IN == 2560
    assert st.CONTEXT_SAMPLES_IN == 8960
    assert st.BLOCK_SAMPLES_IN + st.CONTEXT_SAMPLES_IN == tp.CANONICAL_IN, (
        f"window {st.BLOCK_SAMPLES_IN + st.CONTEXT_SAMPLES_IN} != TRT static shape "
        f"{tp.CANONICAL_IN} -- changing this requires re-exporting all three ONNX models"
    )
    # SOLA crossfade halved with the hop so the overlap ratio per emitted hop
    # stays 25% (was 80ms/320ms, now 40ms/160ms).
    assert st.SOLA_CROSSFADE_SAMPLES == st.SAMPLE_RATE_OUT * 40 // 1000 == 1920
    print("Hop geometry constants test: SUCCESS")


def test_accumulator_hop_cadence():
    print("\n--- Testing BlockAccumulator hop cadence (160ms pops, 560ms context cap) ---")
    from modal_deploy import streaming as st

    acc = st.BlockAccumulator()  # defaults = production geometry

    # Feed exactly one hop of new audio -> exactly one poppable block, no context yet.
    acc.push(np.arange(st.BLOCK_SAMPLES_IN, dtype=np.int16))
    infer_input, context_len, block = acc.pop_block()
    assert len(block) == st.BLOCK_SAMPLES_IN == 2560
    assert context_len == 0
    assert len(infer_input) == st.BLOCK_SAMPLES_IN
    assert acc.pop_block() is None, "no second block until another hop of NEW audio arrives"

    # Second hop: previous hop becomes context.
    acc.push(np.arange(st.BLOCK_SAMPLES_IN, dtype=np.int16))
    infer_input, context_len, block = acc.pop_block()
    assert context_len == st.BLOCK_SAMPLES_IN
    assert len(infer_input) == 2 * st.BLOCK_SAMPLES_IN

    # After enough hops, context saturates at CONTEXT_SAMPLES_IN and the
    # inference window reaches the full 720ms TRT shape (11520 samples).
    for _ in range(6):
        acc.push(np.arange(st.BLOCK_SAMPLES_IN, dtype=np.int16))
        infer_input, context_len, block = acc.pop_block()
    assert context_len == st.CONTEXT_SAMPLES_IN == 8960
    assert len(infer_input) == st.BLOCK_SAMPLES_IN + st.CONTEXT_SAMPLES_IN == 11520
    print("BlockAccumulator hop cadence test: SUCCESS")
```

And register both in `main()` (currently `modal_deploy/test_streaming.py:121-125`):

```python
def main():
    print("Running modal_deploy/streaming.py DSP verification tests...")
    test_sola_crossfade_first_block_holds_tail_only()
    test_sola_crossfade_seamless_at_boundary()
    test_hop_geometry_constants()
    test_accumulator_hop_cadence()
    print("\nAll modal_deploy streaming tests completed successfully!")
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from repo root — `modal_deploy` package import requires it):
```bash
python -m modal_deploy.test_streaming
```
Expected: `AssertionError: hop must be 160ms, got 320` in `test_hop_geometry_constants`.

- [ ] **Step 3: Change the constants**

`modal_deploy/streaming.py:23-37` currently reads:
```python
# TRT Phase 2 (2026-07-07): block shrunk 1000ms→320ms to reduce mouth-to-ear latency
# now that TRT median is 66ms (21× real-time) and the playout buffer gate has passed.
# SOLA crossfade stays at 80ms (25% overhead per block at 320ms — up from 8% at 1000ms;
# watch for time-compression artefacts in C5 listen test).
# NOTE: BLOCK_MS + CONTEXT_MS must equal trt_pipeline.CANONICAL_IN / SAMPLE_RATE_IN * 1000
# (now 320+400=720ms = 11520 samples) -- changing either without updating the
# TRT static shapes requires re-exporting all three ONNX models.
BLOCK_MS = 320               # new audio processed per inference block (was 1000)
CONTEXT_MS = 400              # prior input prepended as left context (unchanged)

BLOCK_SAMPLES_IN = SAMPLE_RATE_IN * BLOCK_MS // 1000        # 5120  (was 16000)
CONTEXT_SAMPLES_IN = SAMPLE_RATE_IN * CONTEXT_MS // 1000    # 6400  (unchanged)

SOLA_CROSSFADE_SAMPLES = SAMPLE_RATE_OUT * 80 // 1000       # 3840 (80 ms @ 48 kHz)
SOLA_SEARCH_SAMPLES = SAMPLE_RATE_OUT * 10 // 1000          # 480  (10 ms @ 48 kHz)
```
Replace with:
```python
# Hop-streaming (2026-07-11): BLOCK shrunk 320ms→160ms (a "hop") with CONTEXT grown
# 400ms→560ms so the total inference window stays 720ms — the TRT static shapes
# (trt_pipeline.CANONICAL_IN = 11520) are UNCHANGED and need no ONNX re-export.
# Each inference slides the same 720ms window forward by one 160ms hop and emits
# only the newest hop's worth of audio: half the accumulation wait of the old
# 320ms block, with MORE left context per emitted sample (560ms vs 400ms).
# SOLA crossfade halved 80ms→40ms to keep the same 25% overlap ratio per hop;
# seam rate doubles to 6.25/s — listen-gated offline via main_chunked before any
# deploy (see docs/superpowers/plans/2026-07-11-hop-streaming-latency.md).
# GPU duty: ~51ms TRT compute per 160ms hop = 32% (was 16%) on the single-tenant L4.
# NOTE: BLOCK_MS + CONTEXT_MS must equal trt_pipeline.CANONICAL_IN / SAMPLE_RATE_IN * 1000
# (160+560=720ms = 11520 samples) -- changing either without updating the
# TRT static shapes requires re-exporting all three ONNX models.
BLOCK_MS = 160               # new audio per inference hop (was 320; 1000 pre-TRT-phase-2)
CONTEXT_MS = 560              # prior input prepended as left context (was 400)

BLOCK_SAMPLES_IN = SAMPLE_RATE_IN * BLOCK_MS // 1000        # 2560  (was 5120)
CONTEXT_SAMPLES_IN = SAMPLE_RATE_IN * CONTEXT_MS // 1000    # 8960  (was 6400)

SOLA_CROSSFADE_SAMPLES = SAMPLE_RATE_OUT * 40 // 1000       # 1920 (40 ms @ 48 kHz, was 80 ms)
SOLA_SEARCH_SAMPLES = SAMPLE_RATE_OUT * 10 // 1000          # 480  (10 ms @ 48 kHz)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m modal_deploy.test_streaming
python -m pytest modal_deploy/test_trt_pipeline.py -q
```
Expected: streaming suite ends `All modal_deploy streaming tests completed successfully!`; all 9 existing pytest tests still pass (`test_pad_to_canonical_short_first_block` reads `st.BLOCK_SAMPLES_IN` and auto-adjusts — its zpad expectation becomes `11520 − 2560 = 8960`, computed from the constants, not hardcoded).

- [ ] **Step 5: Add the cross-module invariant test to the pytest suite too**

Append to `modal_deploy/test_trt_pipeline.py`:

```python
def test_hop_window_matches_trt_static_shape():
    """The streaming hop geometry MUST fill the TRT static input shape exactly --
    a drifted constant here means a silent head-zero-pad on every block (quality
    loss) or a ValueError (oversize), only visible on a live GPU otherwise."""
    try:
        from modal_deploy import streaming as st
    except ImportError:
        import streaming as st
    assert st.BLOCK_SAMPLES_IN + st.CONTEXT_SAMPLES_IN == tp.CANONICAL_IN
```

Run: `python -m pytest modal_deploy/test_trt_pipeline.py -q`
Expected: 10 passed.

- [ ] **Step 6: Commit**

```bash
git add modal_deploy/streaming.py modal_deploy/test_streaming.py modal_deploy/test_trt_pipeline.py
git commit -m "feat(latency): 160ms hop-streaming geometry — same 720ms TRT window, half the accumulation wait"
```

---

### Task 2: Backend playout cushion 0.25s → 0.10s + stale comments

**Files:**
- Modify: `backend/pipeline.py:40-45` (cushion constant + comment)
- Modify: `modal_deploy/worker.py:601` (stale "1000 ms block" comment)
- Modify: `modal_deploy/trt_pipeline.py:16` (stale geometry comment)
- Test: existing suites only (a change-detector test on a constant adds no value; the playout-buffer behavior tests in `backend/test_pipeline.py` override the constants and are unaffected)

**Interfaces:**
- Produces: `VoiceConversionWorker._PLAYOUT_BUFFER_TARGET_BYTES == int(48000 * 2 * 0.10)` (9600 bytes). Cap (`_PLAYOUT_BUFFER_MAX_BYTES`, 5s) unchanged — it's an overflow safety bound, not a latency term.
- Consumes: nothing new.

- [ ] **Step 1: Change the cushion**

`backend/pipeline.py:40-45` currently reads:
```python
    # Phase 2 of the TRT latency plan (2026-07-07): 0.25s target now that
    # BLOCK_MS=320 and TRT median is 66ms (21x real-time, p95=68ms). Converted
    # audio arrives in ~320ms bursts; the 0.25s cushion absorbs jitter without
    # adding a full extra block interval of mouth-to-ear delay.
    # (Phase 1 was 1.25s, required at BLOCK_MS=1000 to absorb one full block interval.)
    _PLAYOUT_BUFFER_TARGET_BYTES = int(48000 * 2 * 0.25)
```
Replace with:
```python
    # Hop-streaming (2026-07-11): 0.10s target now that BLOCK_MS=160 and live
    # TRT compute is ~51ms/hop (32% duty). Converted audio arrives in ~160ms
    # hops; the cushion only needs to absorb hop-to-hop jitter, not a whole
    # block interval. (History: 3s → 1.25s → 0.25s → 0.10s; see
    # .agents/decisions/log.md and the hop-streaming plan.)
    _PLAYOUT_BUFFER_TARGET_BYTES = int(48000 * 2 * 0.10)
```

- [ ] **Step 2: Fix the stale worker comment**

`modal_deploy/worker.py:601` currently reads:
```python
                # Drain every complete 1000 ms block currently buffered.
```
Replace with:
```python
                # Drain every complete hop (st.BLOCK_MS of new audio) currently buffered.
```

- [ ] **Step 3: Fix the stale geometry comment in trt_pipeline.py**

`modal_deploy/trt_pipeline.py:16` currently reads:
```python
CANONICAL_IN = 11520          # 720 ms: BLOCK_MS=320 + CONTEXT_MS=400  (was 22400)
```
Replace with:
```python
CANONICAL_IN = 11520          # 720 ms window: BLOCK_MS=160 + CONTEXT_MS=560 (hop-streaming 2026-07-11; sum unchanged since TRT phase 2)
```

- [ ] **Step 4: Run both suites**

```bash
python -m backend.test_pipeline
python -m modal_deploy.test_streaming
python -m pytest modal_deploy/test_trt_pipeline.py -q
```
Expected: all pass (`backend/test_pipeline.py`'s playout tests set their own `_PLAYOUT_BUFFER_TARGET_BYTES` overrides, so the constant change cannot affect them).

- [ ] **Step 5: Commit**

```bash
git add backend/pipeline.py modal_deploy/worker.py modal_deploy/trt_pipeline.py
git commit -m "feat(latency): 0.10s playout cushion for 160ms hops + stale geometry comments"
```

---

### Task 3: Documentation updates

**Files:**
- Modify: `LATENCY.md` (banner + §1 table rows)
- Modify: `CLAUDE.md` (playout-buffer numbers in "Audio Pipeline & Streaming")
- Modify: `.agents/context/subsystem-notes.md` (hop-streaming entry)

**Interfaces:** none — documentation only. Exact replacement text below; where surrounding prose differs slightly from what's quoted, preserve the rest of the sentence and change only the numbers/phrases shown.

- [ ] **Step 1: LATENCY.md banner**

Add to the end of the banner block (after the `2026-07-03 / 2026-07-07 update` blockquote, before the `---`):

```markdown
> **2026-07-11 hop-streaming update:** BLOCK_MS 320→**160** ("hop") with CONTEXT_MS
> 400→**560** (same 720ms TRT window — no ONNX re-export), SOLA crossfade 80→**40ms**,
> playout cushion 0.25s→**0.10s**. Pipeline-added latency design target is now
> **~370ms** (~450-550ms mouth-to-ear incl. network/PSTN), halved from the ~720ms
> post-TRT-phase-2 figure. Live TRT compute measured 2026-07-11 at ~51ms/block
> (hubert 2.6 / index 13.7 / rmvpe 9.0 / generator 24.0 / postproc 1.5) via the
> per-call `[Worker][LatencySummary]` Render log added the same day.
```

- [ ] **Step 2: LATENCY.md §1 table rows**

In the §1 table, update these rows (change only the figures/phrases shown):
- "Inference Block Accumulation" row: `up to 320 ms (design target)` → `up to 160 ms (hop, design target)`; and in its description, `320ms of *new* audio` → `160ms of *new* audio` and the `160ms left-context window` mention → `560ms left-context window`.
- "SOLA Crossfade" row: `80 ms held back per block` → `40 ms held back per hop`; `(SOLA_CROSSFADE_SAMPLES, 3840 samples @ 48kHz)` → `(SOLA_CROSSFADE_SAMPLES, 1920 samples @ 48kHz)`; `last 80ms` → `last 40ms`.
- "Standing Playout Buffer" row: `1.25 s target / 5 s cap (as of TRT migration phase 1, 2026-07-07; was ~3s/~5s from 2026-07-03 to phase 1)` → `0.10 s target / 5 s cap (hop-streaming 2026-07-11; history 3s → 1.25s → 0.25s → 0.10s)`.
- "Steady-state Total" row: `~1.3 - 1.6 s (design estimate, not live-measured)` → `~450-550 ms (design estimate incl. network/PSTN; pipeline-added ~370ms — not yet live-measured)`; and `Dominated by the 1.25s standing playout buffer` → `Dominated by hop accumulation (160ms) and the 0.10s cushion`.

- [ ] **Step 3: CLAUDE.md playout-buffer numbers**

In "Audio Pipeline & Streaming", the sentence fragment:
```
bounded `self._playout_buffer` (1.25s target/5s cap as of the TRT migration phase 1, down
   from an earlier 3s target — see `.agents/context/subsystem-notes.md`; drop-oldest on
   overflow)
```
becomes:
```
bounded `self._playout_buffer` (0.10s target/5s cap as of hop-streaming 2026-07-11 —
   history 3s → 1.25s → 0.25s → 0.10s, see `.agents/context/subsystem-notes.md`; drop-oldest on
   overflow)
```

- [ ] **Step 4: subsystem-notes.md hop-streaming entry**

Add a new subsection under "## Streaming pipeline", after the "### Playout buffer" subsection:

```markdown
### Hop streaming (2026-07-11)
The 2026-07-03 "latency isn't a priority" decision was reversed on 2026-07-11: latency is
now the active focus. BLOCK_MS 320→160 ("hop") with CONTEXT_MS 400→560 — the TRT input
window stays 720ms/11520 samples, so **the TRT engines did not change and no ONNX
re-export was needed** (this is the whole trick; `test_trt_pipeline.py::
test_hop_window_matches_trt_static_shape` guards the invariant). SOLA crossfade halved to
40ms (same 25% overlap ratio per hop), playout cushion 0.25s→0.10s. Seam rate doubled to
6.25/s — gated by an offline `main_chunked` listen A/B before deploy. Per-emitted-sample
left context actually GREW (560ms vs 400ms), so per-hop conversion quality should be equal
or better; the risk is purely seam density. GPU duty 16%→32% (51ms per 160ms hop),
single-tenant L4 unaffected. Live per-stage numbers (2026-07-11, from `[Worker]
[LatencySummary]`): hubert 2.6ms / index 13.7ms / rmvpe 9.0ms / generator 24.0ms /
postproc 1.5ms ≈ 51ms total, lock_wait ~0.
```

- [ ] **Step 5: Commit**

```bash
git add LATENCY.md CLAUDE.md .agents/context/subsystem-notes.md
git commit -m "docs(latency): hop-streaming geometry, cushion, and live per-stage numbers"
```

---

### Task 4: Offline listen gate (USER-RUN — before any deploy)

**Files:** none — verification only. Runs the **local working-tree code** in an ephemeral Modal container (`modal run` does not touch the deployed `rvc-worker` app), so this is safe to run before production changes, but it uses the user's Modal account/GPU minutes — user runs it.

- [ ] **Step 1: Produce a baseline conversion at current-main geometry**

From a checkout of the pre-change commit (or before merging this branch), run:
```bash
modal run modal_deploy/worker.py::main_chunked --input-file <test-speech.wav> --pitch 7 --output-file baseline_320.wav
```

- [ ] **Step 2: Produce the hop-geometry conversion**

From this branch's working tree:
```bash
modal run modal_deploy/worker.py::main_chunked --input-file <same-test-speech.wav> --pitch 7 --output-file hop_160.wav
```

- [ ] **Step 3: Listen A/B**

Listen to both, focusing on seam artifacts (clicks, warble, time-compression) — the seam rate doubles, so any per-seam artifact becomes twice as audible. **Gate: hop_160.wav must sound equal-or-better than baseline_320.wav.** If seams are audible: first suspect is crossfade length — try 60ms (`SOLA_CROSSFADE_SAMPLES = SAMPLE_RATE_OUT * 60 // 1000`) as a middle ground before anything else, re-run Step 2, re-listen. Do not proceed to Task 5 until this gate passes.

---

### Task 5: Deploy + live verification (USER-RUN)

**Files:** none — deploy + measurement only.

- [ ] **Step 1: Deploy the Modal worker** — `modal deploy modal_deploy/worker.py`; wait for `/health` `{"status":"ready"}`.
- [ ] **Step 2: Push to `main`** (Render auto-deploys; not during a live call).
- [ ] **Step 3: Place a test call and check the end-of-call `[Worker][LatencySummary]`** in Render logs:
  - `block_ms=160` on every row (confirms the deployed worker runs hop geometry).
  - Roughly 2× the block count per minute vs the previous calls.
  - Voiced-block `infer_ms` still ~51ms with the same stage breakdown; `lock_wait_ms` still ~0 (32% duty must not create queueing).
  - `playout_buffer_bytes` mostly 0 with occasional ≤ ~15360 (one 160ms hop = 15360 bytes) — a value repeatedly ≥ 2 hops means the cushion is filling faster than it drains; investigate before trusting the latency number.
- [ ] **Step 4: Measure mouth-to-ear latency** with the built-in spectral tone test (LATENCY.md §3). Target: **≤ ~550ms** (was ~1.3-1.6s design / ~720ms+network post-TRT). Record the measured figure in LATENCY.md §1 (replacing the design-target caveat for this row) and in `.agents/context/subsystem-notes.md`.
- [ ] **Step 5: Live listen check** on a real PSTN call — same seam-artifact focus as Task 4, now with real network jitter. If the lead-side audio shows gaps/choppiness that Task 4's offline replay didn't, the cushion (0.10s) is the first knob to revisit — raise to 0.15s before touching the hop size.

---

## Self-Review

**Spec coverage:** "focus fully on latency" → Tasks 1-2 halve the three structural terms that remain (accumulation 320→160, SOLA 80→40, cushion 250→100). "Don't retry older methods" → no block-size-only resize (the window/hop decoupling is new to this codebase), no cushion-only retune (the cushion change is a consequence of the hop change), no TRT rework (explicitly untouched). User chose "Hop 160ms first" → hop 80ms and causal-model options are explicitly out of scope; Task 5 Step 4's measurement is the decision point for whether to go further.

**Placeholder scan:** all steps carry literal code/text; `<test-speech.wav>` in Task 4 is a genuine user-supplied input (any clean 16kHz+ speech WAV, e.g. the existing test1.wav referenced in subsystem-notes), not a placeholder for missing plan content.

**Type consistency:** the only cross-module contract is `BLOCK_SAMPLES_IN + CONTEXT_SAMPLES_IN == CANONICAL_IN`, asserted twice (Task 1 Steps 1 and 5). All downstream consumers read the `st.*` constants at import time; none hardcode 320/400/3840 (verified: `worker.py` uses `st.BLOCK_MS`/`st.SOLA_CROSSFADE_SAMPLES`/`acc.pop_block()` defaults; `test_trt_pipeline.py::test_pad_to_canonical_short_first_block` computes from `st.BLOCK_SAMPLES_IN`; `test_streaming.py`'s SOLA tests pass explicit crossfade/search args).

**Known risks, stated:** seam rate doubles (gated by Task 4 listen A/B, with a 60ms-crossfade fallback); GPU duty doubles to 32% (Task 5 Step 3 watches `lock_wait_ms` for queueing); first-of-session hops have less context (existing `pad_to_canonical` zero-fill path, already exercised at 320ms geometry — behavior unchanged in kind, only in degree).
