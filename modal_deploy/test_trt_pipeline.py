"""Local unit tests for trt_pipeline's pure-NumPy helpers. No GPU, no Modal.
Run: python -m pytest modal_deploy/test_trt_pipeline.py -v
"""
import numpy as np
import pytest

try:
    from modal_deploy import trt_pipeline as tp
except ImportError:
    import trt_pipeline as tp


def test_pad_to_canonical_full_block():
    pcm = np.ones(tp.CANONICAL_IN, dtype=np.int16) * 1000
    audio, zpad = tp.pad_to_canonical(pcm)
    assert audio.shape == (tp.PADDED_IN,)
    assert audio.dtype == np.float32
    assert zpad == 0
    center = audio[tp.TRT_T_PAD: tp.TRT_T_PAD + tp.CANONICAL_IN]
    assert np.allclose(center, 1000 / 32768.0)


def test_pad_to_canonical_short_first_block():
    # Simulate the first-of-session block: only BLOCK_SAMPLES_IN samples arrive
    # (no context yet). Use the constant so the test stays valid at any block size.
    try:
        from modal_deploy import streaming as st
    except ImportError:
        import streaming as st
    pcm = np.ones(st.BLOCK_SAMPLES_IN, dtype=np.int16)
    audio, zpad = tp.pad_to_canonical(pcm)
    assert audio.shape == (tp.PADDED_IN,)
    assert zpad == tp.CANONICAL_IN - st.BLOCK_SAMPLES_IN   # 6400 (context gap)
    head = audio[tp.TRT_T_PAD: tp.TRT_T_PAD + zpad]
    assert np.allclose(head, 0.0)


def test_pad_to_canonical_rejects_oversize():
    with pytest.raises(ValueError):
        tp.pad_to_canonical(np.zeros(tp.CANONICAL_IN + 1, dtype=np.int16))


def test_f0_to_coarse_bounds_and_monotonic():
    f0 = np.array([0.0, 50.0, 220.0, 1100.0, 5000.0])
    c = tp.f0_to_coarse(f0)
    assert c.dtype == np.int64
    assert c.min() >= 1 and c.max() <= 255
    assert c[1] < c[2] < c[3]
    assert c[3] == 255 and c[4] == 255


def test_decode_f0_peak():
    hidden = np.zeros((1, tp.MEL_FRAMES_PADDED, 360), dtype=np.float32)
    hidden[0, :, 180] = 1.0
    f0 = tp.decode_f0(hidden)
    assert f0.shape == (tp.MEL_FRAMES,)
    cents = 20 * 180 + 1997.3794084376191
    expected_hz = 10 * 2 ** (cents / 1200)
    assert np.allclose(f0, expected_hz, rtol=1e-3)


def test_decode_f0_unvoiced_is_zero():
    hidden = np.full((1, tp.MEL_FRAMES_PADDED, 360), 0.001, dtype=np.float32)
    f0 = tp.decode_f0(hidden, thred=0.03)
    assert np.all(f0 == 0.0)


def test_change_rms_identity_at_rate_1():
    rng = np.random.default_rng(3)
    src = rng.standard_normal(tp.CANONICAL_IN).astype(np.float32) * 0.1
    out = rng.standard_normal(tp.OUT_48K).astype(np.float32) * 0.5
    mixed = tp.change_rms(src, out.copy(), rate=1.0)
    assert np.allclose(mixed, out, atol=1e-5)


def test_apply_protect_passthrough_when_disabled():
    rng = np.random.default_rng(4)
    feats = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    raw = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    pitchf = rng.uniform(0, 300, tp.GEN_FRAMES).astype(np.float32)
    out = tp.apply_protect(feats, raw, pitchf, protect=0.5)
    assert np.array_equal(out, feats)


def test_apply_protect_blends_unvoiced():
    feats = np.ones((1, tp.GEN_FRAMES, 768), dtype=np.float32)
    raw = np.zeros((1, tp.GEN_FRAMES, 768), dtype=np.float32)
    pitchf = np.zeros(tp.GEN_FRAMES, dtype=np.float32)
    out = tp.apply_protect(feats, raw, pitchf, protect=0.33)
    assert np.allclose(out, 0.33, atol=1e-6)
