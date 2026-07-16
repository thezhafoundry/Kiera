"""Contracts for the named RVC streaming geometry profiles."""

import pytest

from modal_deploy.rvc_profiles import (
    build_model_version,
    get_profile,
    profile_onnx_dir,
    profile_trt_cache_dir,
)


@pytest.mark.parametrize("name", ["baseline", "candidate_b"])
def test_profile_geometry_contracts(name):
    profile = get_profile(name)

    assert profile.canonical_in == (
        profile.block_ms + profile.context_ms
    ) * 16
    assert profile.sola_samples == profile.sola_ms * 48
    assert profile.playout_ms > 0
    assert profile.name in {"baseline", "candidate_b"}


def test_baseline_matches_the_current_deployed_geometry():
    profile = get_profile("baseline")

    assert (
        profile.block_ms,
        profile.context_ms,
        profile.sola_ms,
        profile.playout_ms,
    ) == (320, 400, 80, 250)


def test_candidate_b_matches_the_approved_geometry():
    profile = get_profile("candidate_b")

    assert (
        profile.block_ms,
        profile.context_ms,
        profile.sola_ms,
        profile.playout_ms,
    ) == (160, 240, 40, 160)


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="Unknown RVC stream profile"):
        get_profile("fast-ish")


def test_candidate_artifacts_cannot_overwrite_the_baseline():
    baseline = get_profile("baseline")
    candidate = get_profile("candidate_b")

    assert profile_onnx_dir(baseline) == "/root/rvc-models/onnx"
    assert profile_trt_cache_dir(baseline) == "/root/rvc-models/trt_cache"
    assert profile_onnx_dir(candidate).endswith("/profiles/candidate_b/onnx")
    assert profile_trt_cache_dir(candidate).endswith(
        "/profiles/candidate_b/trt_cache"
    )


def test_model_version_identifies_checkpoint_and_index(tmp_path):
    checkpoint = tmp_path / "voice.pth"
    index = tmp_path / "voice.index"
    checkpoint.write_bytes(b"checkpoint-v1")
    index.write_bytes(b"index-v1")

    first = build_model_version(checkpoint, index)
    index.write_bytes(b"index-v2")
    second = build_model_version(checkpoint, index)

    assert first.startswith("rvc-") and "-idx-" in first
    assert first != second
