"""Pure-numpy/stdlib tests for modal_deploy/streaming.py.

streaming.py deliberately imports only numpy + the stdlib (no Modal, no GPU),
so this module is importable and runnable in a plain CPython environment --
no `modal` package, no GPU, no network. Matches backend/test_pipeline.py's
plain asyncio/assert/print style (no pytest), except these tests are
synchronous since sola_crossfade is a pure sync function.

Run: python -m modal_deploy.test_streaming
"""

import numpy as np

from modal_deploy.streaming import sola_crossfade


def test_sola_crossfade_first_block_holds_tail_only():
    print("\n--- Testing SOLA first-call tail-hold (no tail yet -> no crossfade) ---")
    crossfade = 480
    block = np.arange(2000, dtype=np.int16)

    emit, tail = sola_crossfade(np.zeros(0, dtype=np.int16), block, crossfade=crossfade, search=160)

    assert len(emit) == len(block) - crossfade, "first call should emit everything except the held-back tail"
    assert len(tail) == crossfade, "held-back tail must be exactly `crossfade` samples"
    assert np.array_equal(emit, block[:-crossfade])
    assert np.array_equal(tail, block[-crossfade:])
    print("SOLA first-block tail-hold test: SUCCESS")


def test_sola_crossfade_seamless_at_boundary():
    print("\n--- Testing SOLA crossfade seam continuity across two consecutive blocks ---")

    sample_rate = 48000
    freq_hz = 200.0
    crossfade = 480   # 10ms @ 48kHz -- smaller than the production 80ms default,
                       # for a fast/deterministic test; the math is the same.
    search = 160       # ~3.3ms

    # A long continuous sine wave is the "ground truth" signal that two
    # overlapping blocks below are carved out of -- so there's a real
    # correct alignment for SOLA to find (not just noise).
    duration_s = 0.5
    n = int(sample_rate * duration_s)
    t = np.arange(n) / sample_rate
    sine = (np.sin(2 * np.pi * freq_hz * t) * 10000).astype(np.int16)

    block1_len = 6000
    block2_len = 6000
    # Nominal (k=0) overlap point: block2 "should" start `crossfade` samples
    # before block1 ends, mirroring how the /ws streaming handler's
    # `overlap_keep` gives consecutive inference-block outputs an overlap for
    # SOLA to lock onto (see streaming.py's trim_context docstring). Real RVC
    # output length isn't guaranteed exact, so block2 is additionally shifted
    # by `drift` samples (within the search window) -- the correlation search
    # must find this real offset itself; a naive k=0/no-search concatenation
    # would stitch mismatched phases of the sine and produce a real seam jump.
    nominal_overlap_start = block1_len - crossfade
    drift = 50
    assert 0 < drift < search
    overlap_start = nominal_overlap_start - drift

    block1 = sine[:block1_len]
    block2 = sine[overlap_start: overlap_start + block2_len]

    emit1, tail1 = sola_crossfade(np.zeros(0, dtype=np.int16), block1, crossfade=crossfade, search=search)
    assert len(tail1) == crossfade

    emit2, tail2 = sola_crossfade(tail1, block2, crossfade=crossfade, search=search)
    assert len(tail2) == crossfade
    assert emit1.dtype == np.int16 and emit2.dtype == np.int16

    # Baseline: the biggest sample-to-sample jump anywhere in the *source*
    # sine wave itself -- i.e. how "jumpy" perfectly continuous, correctly
    # reconstructed audio is allowed to look.
    baseline_jump = int(np.max(np.abs(np.diff(sine[:block1_len + block2_len].astype(np.int64)))))
    threshold = baseline_jump * 3  # generous multiplier for rounding in the crossfade math

    # Check 1 (the literal seam, as asked): emit1's last sample vs emit2's
    # first sample. NOTE: by construction of the raised-cosine ramp (weight 0
    # on the new block / weight 1 on the held tail at the very first crossfade
    # sample), this exact boundary is *always* bit-continuous with the tail
    # regardless of whether the correlation search below found the right
    # offset -- so on its own this check only catches gross bugs in the
    # tail-hold/dtype/slicing mechanics (which is still real coverage: it
    # would fail on an off-by-one in the tail slice, a mis-shaped ramp, or an
    # int16 clipping/overflow bug). It would NOT catch a broken correlation
    # search, since a pure single-frequency sine is inherently glitch-free
    # under ANY linear blend of itself (A*sin(x)+B*sin(x+phi) is still a
    # sine) -- that's why Check 2 below exists.
    seam_jump = abs(int(emit2[0]) - int(emit1[-1]))
    print(f"seam jump: {seam_jump}, source baseline max jump: {baseline_jump}, threshold: {threshold}")
    assert seam_jump <= threshold, (
        f"SOLA seam discontinuity too large: {seam_jump} > {threshold} (source baseline {baseline_jump})"
    )

    # Check 2 (the one that actually exercises the correlation search): the
    # reconstructed audio has to reproduce the *ground-truth* source sine, not
    # just "some smooth curve". block2 was deliberately given a `drift`-sample
    # offset from the naive (k=0) alignment point, so if `sola_crossfade`
    # picked the wrong offset it would splice in the wrong phase of the sine
    # -- and because two same-frequency sines summed at a large phase offset
    # is itself just another (differently-phased) sine, only a ground-truth
    # comparison -- not a jump/derivative metric -- can catch that. Confirmed
    # this check fails (max error ~= sine amplitude) when the correlation
    # search is disabled, by deliberately breaking `best_k` selection locally
    # during test development.
    reconstructed = np.concatenate([emit1, emit2]).astype(np.int64)
    ground_truth = sine[:len(reconstructed)].astype(np.int64)
    max_error = int(np.max(np.abs(reconstructed - ground_truth)))
    error_threshold = baseline_jump * 20  # generous vs. baseline, but far below sine amplitude (10000)

    print(f"reconstruction max error vs ground truth: {max_error}, threshold: {error_threshold}")
    assert max_error <= error_threshold, (
        f"SOLA misaligned the crossfade: reconstruction diverges from ground truth by {max_error} "
        f"(threshold {error_threshold}) -- correlation search likely picked the wrong offset"
    )
    print("SOLA crossfade seam continuity test: SUCCESS")


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


def main():
    print("Running modal_deploy/streaming.py DSP verification tests...")
    test_sola_crossfade_first_block_holds_tail_only()
    test_sola_crossfade_seamless_at_boundary()
    test_hop_geometry_constants()
    test_accumulator_hop_cadence()
    print("\nAll modal_deploy streaming tests completed successfully!")


if __name__ == "__main__":
    main()
