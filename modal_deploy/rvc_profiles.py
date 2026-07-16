"""Named, immutable geometry profiles for the RVC streaming pipeline.

This module intentionally uses only the standard library so the same profile
can be imported by local tests, the Render backend, and Modal build/runtime
containers.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Mapping, Optional


SAMPLE_RATE_IN = 16_000
SAMPLE_RATE_OUT = 48_000
TRT_T_PAD = 16_000
PROFILE_ENV_VAR = "RVC_STREAM_PROFILE"

_HUBERT_KERNELS = (10, 3, 3, 3, 3, 2, 2)
_HUBERT_STRIDES = (5, 2, 2, 2, 2, 2, 2)


def _hubert_output_frames(input_samples: int) -> int:
    frames = input_samples
    for kernel, stride in zip(_HUBERT_KERNELS, _HUBERT_STRIDES):
        frames = (frames - kernel) // stride + 1
    return frames


@dataclass(frozen=True)
class RVCProfile:
    name: str
    block_ms: int
    context_ms: int
    sola_ms: int
    playout_ms: int

    def __post_init__(self) -> None:
        for field_name in (
            "block_ms",
            "context_ms",
            "sola_ms",
            "playout_ms",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")

    @property
    def canonical_in(self) -> int:
        return (self.block_ms + self.context_ms) * SAMPLE_RATE_IN // 1000

    @property
    def sola_samples(self) -> int:
        return self.sola_ms * SAMPLE_RATE_OUT // 1000

    @property
    def padded_in(self) -> int:
        return self.canonical_in + 2 * TRT_T_PAD

    @property
    def hubert_frames(self) -> int:
        return _hubert_output_frames(self.padded_in)

    @property
    def generator_frames(self) -> int:
        return self.hubert_frames * 2

    @property
    def mel_frames(self) -> int:
        return self.padded_in // 160 + 1

    @property
    def mel_frames_padded(self) -> int:
        return ((self.mel_frames + 31) // 32) * 32


_PROFILES = {
    "baseline": RVCProfile(
        name="baseline",
        block_ms=320,
        context_ms=400,
        sola_ms=80,
        playout_ms=250,
    ),
    "candidate_b": RVCProfile(
        name="candidate_b",
        block_ms=160,
        context_ms=240,
        sola_ms=40,
        playout_ms=160,
    ),
}


def get_profile(name: str) -> RVCProfile:
    normalized = str(name).strip().lower()
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(_PROFILES))
        raise ValueError(
            f"Unknown RVC stream profile {name!r}; expected one of: {choices}"
        ) from exc


def get_active_profile(
    environ: Optional[Mapping[str, str]] = None,
) -> RVCProfile:
    source = os.environ if environ is None else environ
    return get_profile(source.get(PROFILE_ENV_VAR, "baseline"))


def _profile_artifact_root(
    profile: RVCProfile,
    model_root: str = "/root/rvc-models",
) -> str:
    if profile.name == "baseline":
        return model_root
    return f"{model_root}/profiles/{profile.name}"


def profile_onnx_dir(
    profile: RVCProfile,
    model_root: str = "/root/rvc-models",
) -> str:
    return f"{_profile_artifact_root(profile, model_root)}/onnx"


def profile_trt_cache_dir(
    profile: RVCProfile,
    model_root: str = "/root/rvc-models",
) -> str:
    return f"{_profile_artifact_root(profile, model_root)}/trt_cache"


def artifact_sha256(path: os.PathLike | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_model_version(
    checkpoint_path: os.PathLike | str,
    index_path: os.PathLike | str,
) -> str:
    checkpoint_digest = artifact_sha256(checkpoint_path)[:12]
    index_digest = artifact_sha256(index_path)[:12]
    return f"rvc-{checkpoint_digest}-idx-{index_digest}"
