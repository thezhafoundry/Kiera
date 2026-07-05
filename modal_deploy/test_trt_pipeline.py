"""Local unit tests for trt_pipeline's pure-NumPy helpers. No GPU, no Modal required.

Run: python -m pytest modal_deploy/test_trt_pipeline.py -v
  or from repo root: python -m pytest modal_deploy/test_trt_pipeline.py -v

These tests cover the pure-NumPy helper functions only. TRTVoicePipeline (which
requires ORT + GPU) is not tested here — its integration is verified via
compile_trt.py's build_engines() warmup pass on the actual L4.
"""
import numpy as np
import pytest

try:
    from modal_deploy import trt_pipeline as tp
except ImportError:
    import trt_pipeline as tp


# ---------------------------------------------------------------------------
# pad_to_canonical
# ---------------------------------------------------------------------------

def test_pad_to_canonical_full_block():
    """A full-size CANONICAL_IN block should have no zero padding."""
    pcm = np.ones(tp.CANONICAL_IN, dtype=np.int16) * 1000
    audio, zpad = tp.pad_to_canonical(pcm)
    assert audio.shape == (tp.PADDED_IN,), f"shape {audio.shape} != ({tp.PADDED_IN},)"
    assert audio.dtype == np.float32
    assert zpad == 0
    # The center region is the normalized input
    center = audio[tp.TRT_T_PAD: tp.TRT_T_PAD + tp.CANONICAL_IN]
    assert np.allclose(center, 1000 / 32768.0)


def test_pad_to_canonical_short_first_block():
    """First streaming block has no context yet — zero-fill at head."""
    pcm = np.ones(16000, dtype=np.int16)
    audio, zpad = tp.pad_to_canonical(pcm)
    assert audio.shape == (tp.PADDED_IN,)
    assert zpad == tp.CANONICAL_IN - 16000   # 6400
    # Zero-fill sits at the head of the canonical region
    head = audio[tp.TRT_T_PAD: tp.TRT_T_PAD + zpad]
    assert np.allclose(head, 0.0), "zero-fill region should be all zeros"
    # Actual samples follow immediately after
    body = audio[tp.TRT_T_PAD + zpad: tp.TRT_T_PAD + tp.CANONICAL_IN]
    assert np.allclose(body, 1.0 / 32768.0)


def test_pad_to_canonical_rejects_oversize():
    """Blocks exceeding CANONICAL_IN must raise ValueError."""
    with pytest.raises(ValueError, match="exceeds canonical"):
        tp.pad_to_canonical(np.zeros(tp.CANONICAL_IN + 1, dtype=np.int16))


def test_pad_to_canonical_minimal_block():
    """Even a 1-sample block should work without error."""
    audio, zpad = tp.pad_to_canonical(np.array([32767], dtype=np.int16))
    assert audio.shape == (tp.PADDED_IN,)
    assert zpad == tp.CANONICAL_IN - 1


# ---------------------------------------------------------------------------
# f0_to_coarse
# ---------------------------------------------------------------------------

def test_f0_to_coarse_bounds_and_monotonic():
    """Output must be int64 in [1, 255] and monotonic in the voiced range."""
    f0 = np.array([0.0, 50.0, 220.0, 1100.0, 5000.0])
    c = tp.f0_to_coarse(f0)
    assert c.dtype == np.int64
    assert c.min() >= 1 and c.max() <= 255
    assert c[1] < c[2] < c[3]           # monotonic in voiced range
    assert c[3] == 255 and c[4] == 255  # clamped at F0_MAX


def test_f0_to_coarse_unvoiced_is_one():
    """f0=0 (unvoiced) maps to coarse code 1 (minimum code, not 0)."""
    c = tp.f0_to_coarse(np.array([0.0]))
    assert c[0] == 1


# ---------------------------------------------------------------------------
# decode_f0
# ---------------------------------------------------------------------------

def test_decode_f0_peak():
    """A clean salience peak at bin 180 must decode near its expected Hz value."""
    hidden = np.zeros((1, tp.MEL_FRAMES_PADDED, 360), dtype=np.float32)
    hidden[0, :, 180] = 1.0
    f0 = tp.decode_f0(hidden)
    assert f0.shape == (tp.MEL_FRAMES,), f"shape {f0.shape} != ({tp.MEL_FRAMES},)"
    cents = 20.0 * 180 + 1997.3794084376191
    expected_hz = 10.0 * 2.0 ** (cents / 1200.0)
    assert np.allclose(f0, expected_hz, rtol=1e-3), (
        f"Expected ~{expected_hz:.1f} Hz, got {f0[0]:.1f} Hz"
    )


def test_decode_f0_unvoiced_is_zero():
    """Below-threshold salience must produce f0=0 everywhere."""
    hidden = np.full((1, tp.MEL_FRAMES_PADDED, 360), 0.001, dtype=np.float32)
    f0 = tp.decode_f0(hidden, thred=0.03)
    assert np.all(f0 == 0.0), "all frames should be unvoiced (f0=0)"


def test_decode_f0_output_length():
    """decode_f0 must always return exactly MEL_FRAMES values."""
    hidden = np.random.default_rng(99).standard_normal(
        (1, tp.MEL_FRAMES_PADDED, 360)
    ).astype(np.float32)
    f0 = tp.decode_f0(hidden)
    assert len(f0) == tp.MEL_FRAMES


# ---------------------------------------------------------------------------
# change_rms
# ---------------------------------------------------------------------------

def test_change_rms_identity_at_rate_1():
    """rate=1.0 must return the output array unchanged."""
    rng = np.random.default_rng(3)
    src = rng.standard_normal(tp.CANONICAL_IN).astype(np.float32) * 0.1
    out = rng.standard_normal(tp.OUT_48K).astype(np.float32) * 0.5
    out_orig = out.copy()
    mixed = tp.change_rms(src, out, rate=1.0)
    assert np.array_equal(mixed, out_orig), "rate=1 should return output unchanged"


# ---------------------------------------------------------------------------
# apply_protect
# ---------------------------------------------------------------------------

def test_apply_protect_passthrough_when_disabled():
    """protect >= 0.5 means protection is OFF — feats should pass through unchanged."""
    rng = np.random.default_rng(4)
    feats = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    raw = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    pitchf = rng.uniform(0, 300, tp.GEN_FRAMES).astype(np.float32)
    out = tp.apply_protect(feats, raw, pitchf, protect=0.5)
    assert np.array_equal(out, feats), "protect=0.5 should disable protection"


def test_apply_protect_blends_unvoiced():
    """On fully unvoiced audio (pitchf=0), output should be feats*protect + raw*(1-protect)."""
    feats = np.ones((1, tp.GEN_FRAMES, 768), dtype=np.float32)
    raw = np.zeros((1, tp.GEN_FRAMES, 768), dtype=np.float32)
    pitchf = np.zeros(tp.GEN_FRAMES, dtype=np.float32)   # all unvoiced
    protect = 0.33
    out = tp.apply_protect(feats, raw, pitchf, protect=protect)
    # unvoiced: feats*protect + raw*(1-protect) = 1*0.33 + 0*0.67 = 0.33
    assert np.allclose(out, 0.33, atol=1e-6)


def test_apply_protect_voiced_frames_unchanged():
    """Voiced frames (pitchf != 0) should keep the original FAISS-mixed feats."""
    rng = np.random.default_rng(5)
    feats = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    raw = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    # All voiced: pitchf everywhere > 0
    pitchf = np.ones(tp.GEN_FRAMES, dtype=np.float32) * 220.0
    out = tp.apply_protect(feats, raw, pitchf, protect=0.33)
    assert np.array_equal(out, feats), "voiced frames should be left unchanged"
