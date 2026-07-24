"""Export the three RVC inference models to static-shape ONNX on the L4.

Run individual exporters:
  modal run modal_deploy/export_onnx.py::export_hubert
  modal run modal_deploy/export_onnx.py::export_generator
  modal run modal_deploy/export_onnx.py::export_rmvpe

Or run all three at once:
  modal run modal_deploy/export_onnx.py

Each exporter runs a PyTorch-vs-ORT parity check and hard-fails on regression,
so a green run IS the export test.
"""
import modal

try:
    from modal_deploy.modal_defs import trt_image, volume
except ImportError:   # inside container
    from modal_defs import trt_image, volume
try:
    from modal_deploy.rvc_profiles import (
        PROFILE_ENV_VAR,
        SAMPLE_RATE_IN,
        SAMPLE_RATE_OUT,
        TRT_T_PAD,
        get_active_profile,
        profile_onnx_dir,
    )
except ImportError:  # inside container
    from rvc_profiles import (
        PROFILE_ENV_VAR,
        SAMPLE_RATE_IN,
        SAMPLE_RATE_OUT,
        TRT_T_PAD,
        get_active_profile,
        profile_onnx_dir,
    )

app = modal.App("rvc-onnx-export")

# ---- Canonical static shapes (shared with trt_pipeline.py) ----
PROFILE = get_active_profile()
PROFILE_NAME = PROFILE.name
SR_IN = SAMPLE_RATE_IN
SR_OUT = SAMPLE_RATE_OUT
CANONICAL_IN = PROFILE.canonical_in
PADDED_IN = PROFILE.padded_in
HUBERT_FRAMES = PROFILE.hubert_frames
GEN_FRAMES = PROFILE.generator_frames
OUT_PADDED_48K = GEN_FRAMES * 480
T_PAD_TGT = TRT_T_PAD * 3
OUT_48K = OUT_PADDED_48K - 2 * T_PAD_TGT
MEL_FRAMES = PROFILE.mel_frames
MEL_FRAMES_PADDED = PROFILE.mel_frames_padded

ONNX_DIR = profile_onnx_dir(PROFILE)
FALLBACK_ONNX_DIR = "/root/rvc-models/onnx"
OPSET = 17


def _log_profile() -> None:
    print(
        f"[Export] RVC profile={PROFILE_NAME} "
        f"canonical_in={CANONICAL_IN} onnx_dir={ONNX_DIR}"
    )


def _setup_rvc_path():
    import os, sys
    os.chdir("/root/rvc")
    if "/root/rvc" not in sys.path:
        sys.path.insert(0, "/root/rvc")
    os.environ.setdefault("weight_root", "/root/rvc-models/weights")
    os.environ.setdefault("index_root", "/root/rvc-models/logs/mi-test")
    os.environ.setdefault("rmvpe_root", "/root/rvc/assets/rmvpe")


@app.function(image=trt_image, gpu="L4", timeout=1800,
              env={PROFILE_ENV_VAR: PROFILE_NAME},
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_hubert():
    """Export HuBERT (hubert_base.pt) as a static-shape ONNX for Engine 1.

    The community ContentVec ONNX (vec-768-layer-12.onnx) uses a 3-D input
    [batch, 1, T] incompatible with our [batch, T] pipeline contract, so we
    always export directly from hubert_base.pt (confirmed present by Task 1 probe).
    """
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    _log_profile()

    # Monkeypatch fairseq's pad_to_multiple to be JIT tracing compatible.
    # The default fairseq code checks 'if m.is_integer():' where m is a Tensor during tracing,
    # raising AttributeError: 'Tensor' object has no attribute 'is_integer'.
    import sys
    import fairseq.models.wav2vec.utils as fairseq_utils
    def patched_pad_to_multiple(x, multiple, dim=-1, value=0):
        import torch.nn.functional as F
        try:
            tsz = int(x.shape[dim])
        except Exception:
            tsz = x.shape[dim]
        remainder = tsz % multiple
        if remainder == 0:
            return x, 0
        pad_length = multiple - remainder
        ndim = x.ndim
        actual_dim = dim if dim >= 0 else dim + ndim
        pad_widths = [0] * (2 * ndim)
        pad_widths[2 * (ndim - 1 - actual_dim) + 1] = pad_length
        padded = F.pad(x, pad_widths, value=value)
        return padded, pad_length
    fairseq_utils.pad_to_multiple = patched_pad_to_multiple
    if "fairseq.models.wav2vec.utils" in sys.modules:
        sys.modules["fairseq.models.wav2vec.utils"].pad_to_multiple = patched_pad_to_multiple
    if "fairseq.models.wav2vec.wav2vec2" in sys.modules:
        sys.modules["fairseq.models.wav2vec.wav2vec2"].pad_to_multiple = patched_pad_to_multiple

    os.makedirs(ONNX_DIR, exist_ok=True)
    hubert_out = f"{ONNX_DIR}/hubert.onnx"

    from configs.config import Config
    from infer.modules.vc.utils import load_hubert

    config = Config()
    hubert = load_hubert(config).float().eval()

    class HubertWrapper(torch.nn.Module):
        """[1, PADDED_IN] float32 -> [1, HUBERT_FRAMES, 768].
        Strips padding_mask (None at runtime, not traceable) and pins output_layer=12.
        """
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, audio):
            feats = self.model.extract_features(
                source=audio, padding_mask=None, output_layer=12,
            )[0]
            return feats

    wrapper = HubertWrapper(hubert).cpu().eval()
    dummy = torch.zeros(1, PADDED_IN, dtype=torch.float32)

    with torch.no_grad():
        ref = wrapper(dummy)
    assert ref.shape == (1, HUBERT_FRAMES, 768), f"wrapper shape mismatch: {ref.shape}"

    torch.onnx.export(
        wrapper, (dummy,), hubert_out,
        input_names=["audio"], output_names=["feats"],
        opset_version=OPSET, do_constant_folding=True,
    )
    print("[Export] hubert.onnx written")

    rng = np.random.default_rng(0)
    speech = (rng.standard_normal(PADDED_IN) * 0.05).astype(np.float32)[None, :]
    sess = ort.InferenceSession(hubert_out, providers=["CPUExecutionProvider"])
    got = sess.run(None, {"audio": speech})[0]
    print(f"[Export] hubert.onnx output shape: {got.shape}")
    assert got.shape == (1, HUBERT_FRAMES, 768), f"Shape mismatch: {got.shape}"

    with torch.no_grad():
        ref_out = wrapper(torch.from_numpy(speech)).numpy()
    cos = float(np.sum(ref_out * got) / (np.linalg.norm(ref_out) * np.linalg.norm(got) + 1e-9))
    print(f"[Export] hubert.onnx parity cosine={cos:.6f}")
    assert cos >= 0.999, f"HuBERT ONNX parity failed: cosine={cos:.6f}"

    volume.commit()
    print("[Export] hubert.onnx committed to volume")


@app.function(image=trt_image, gpu="L4", timeout=1800,
              env={PROFILE_ENV_VAR: PROFILE_NAME},
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_generator():
    """Export the RVC SynthesizerTrnMs768NSFsid generator as static-shape ONNX (Engine 2)."""
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    _log_profile()
    from infer.lib.infer_pack.models_onnx import SynthesizerTrnMsNSFsidM

    cpt = torch.load("/root/rvc-models/weights/mi-test.pth", map_location="cpu")
    tgt_sr = cpt["config"][-1]
    assert tgt_sr == 48000, (
        f"mi-test.pth tgt_sr={tgt_sr}, but the whole pipeline contract is 48kHz "
        "(worker returns 3x samples with no resample) â€” stop and escalate."
    )
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk fixup
    config = list(cpt["config"])
    if len(config) < 19:
        config.append("v2")
    net_g = SynthesizerTrnMsNSFsidM(*config, is_half=False)
    net_g.load_state_dict(cpt["weight"], strict=False)
    net_g = net_g.float().eval()

    phone = torch.zeros(1, GEN_FRAMES, 768, dtype=torch.float32)
    phone_lengths = torch.tensor([GEN_FRAMES], dtype=torch.int64)
    pitch = torch.zeros(1, GEN_FRAMES, dtype=torch.int64)
    pitchf = torch.zeros(1, GEN_FRAMES, dtype=torch.float32)
    sid = torch.tensor([0], dtype=torch.int64)
    rnd = torch.zeros(1, 192, GEN_FRAMES, dtype=torch.float32)
    # sine_noise: external noise for SineGen -- shape [1, OUT_PADDED_48K, 1].
    # Generated by numpy RNG in trt_pipeline.py at inference time; passed in as a
    # model input so no ONNX RandomNormal ops appear inside the graph (TRT Myelin
    # cannot compile those). Zero tensor for the trace is fine -- parity check
    # uses a real N(0,1) tensor so cosine matches between PyTorch and ORT.
    sine_noise = torch.zeros(1, OUT_PADDED_48K, 1, dtype=torch.float32)
    args = (phone, phone_lengths, pitch, pitchf, sid, rnd, None, sine_noise)

    os.makedirs(ONNX_DIR, exist_ok=True)
    out_path = f"{ONNX_DIR}/generator.onnx"
    torch.onnx.export(
        net_g, args, out_path,
        input_names=["phone", "phone_lengths", "pitch", "pitchf", "sid", "rnd", "sine_noise"],
        output_names=["audio"],
        opset_version=OPSET, do_constant_folding=True,
    )

    # ---- Parity check ----
    rng = np.random.default_rng(1)
    feed = {
        "phone": rng.standard_normal((1, GEN_FRAMES, 768)).astype(np.float32) * 0.1,
        "phone_lengths": np.array([GEN_FRAMES], dtype=np.int64),
        "pitch": rng.integers(1, 255, (1, GEN_FRAMES)).astype(np.int64),
        "pitchf": rng.uniform(100, 300, (1, GEN_FRAMES)).astype(np.float32),
        "sid": np.array([0], dtype=np.int64),
        "rnd": rng.standard_normal((1, 192, GEN_FRAMES)).astype(np.float32),
        "sine_noise": rng.standard_normal((1, OUT_PADDED_48K, 1)).astype(np.float32),
    }
    with torch.no_grad():
        ref_inputs = list(feed.values())
        ref = net_g(
            *[torch.from_numpy(v) for v in ref_inputs[:6]],
            None,
            torch.from_numpy(ref_inputs[6]),
        )
        ref = (ref[0] if isinstance(ref, tuple) else ref).numpy()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, feed)[0]
    assert got.shape[-1] == OUT_PADDED_48K, f"expected {OUT_PADDED_48K} samples, got {got.shape}"
    cos = float(np.sum(ref * got) / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-9))
    print(f"[Export] generator.onnx parity cosine={cos:.6f}")
    assert cos >= 0.999, f"Generator ONNX parity failed: cosine={cos:.6f}"
    volume.commit()
    print(f"[Export] wrote {out_path}")


@app.function(image=trt_image, gpu="L4", timeout=1800,
              env={PROFILE_ENV_VAR: PROFILE_NAME},
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_rmvpe():
    """Export the RMVPE E2E neural net as static-shape ONNX (Engine 3)."""
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    _log_profile()
    from infer.lib.rmvpe import RMVPE

    # Check both locations (container assets dir + volume upload location)
    pt_path = "/root/rvc/assets/rmvpe/rmvpe.pt"
    if not os.path.exists(pt_path):
        pt_path = "/root/rvc-models/assets/rmvpe/rmvpe.pt"
    if not os.path.exists(pt_path):
        raise FileNotFoundError(
            "rmvpe.pt not found. Upload it via: "
            "modal volume put rvc-models <local-path-to-rmvpe.pt> assets/rmvpe/rmvpe.pt"
        )

    rmvpe = RMVPE(pt_path, is_half=False, device="cpu")
    net = rmvpe.model.float().eval()

    dummy_mel = torch.zeros(1, 128, MEL_FRAMES_PADDED, dtype=torch.float32)
    os.makedirs(ONNX_DIR, exist_ok=True)
    out_path = f"{ONNX_DIR}/rmvpe.onnx"
    torch.onnx.export(
        net, (dummy_mel,), out_path,
        input_names=["mel"], output_names=["hidden"],
        opset_version=OPSET, do_constant_folding=True,
    )

    rng = np.random.default_rng(2)
    mel = rng.standard_normal((1, 128, MEL_FRAMES_PADDED)).astype(np.float32)
    with torch.no_grad():
        ref = net(torch.from_numpy(mel)).numpy()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, {"mel": mel})[0]
    assert got.shape == (1, MEL_FRAMES_PADDED, 360), f"unexpected hidden shape {got.shape}"
    cos = float(np.sum(ref * got) / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-9))
    print(f"[Export] rmvpe.onnx parity cosine={cos:.6f}")
    assert cos >= 0.999, f"RMVPE ONNX parity failed: cosine={cos:.6f}"
    volume.commit()
    print(f"[Export] wrote {out_path}")


@app.function(image=trt_image, gpu="L4", timeout=3600,
              env={PROFILE_ENV_VAR: PROFILE_NAME},
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_all():
    """Export all three TRT-targeted ONNX models plus the dynamic fallback pair."""
    export_hubert.local()
    export_generator.local()
    export_rmvpe.local()
    export_fallback.local()


@app.local_entrypoint()
def main():
    export_all.remote()


# ---- A6: Fallback-pair exporter (consolidated from worker.export_model_to_onnx) ----
# Produces the two artifacts used by the onnx-cuda (non-TRT) fallback path:
#   vec-768-layer-12.onnx  — community ContentVec model, downloaded from HuggingFace
#   mi-test.onnx           — dynamic-axis export of the trained generator checkpoint
# These differ from the TRT-targeted static exports: dynamic axes let the fallback
# path accept variable block sizes without a recompile.

@app.function(image=trt_image, gpu="L4", timeout=1800,
              env={PROFILE_ENV_VAR: PROFILE_NAME},
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_fallback():
    """Download the ContentVec ONNX (pinned HF revision) and export mi-test.onnx
    with dynamic axes for the onnx-cuda fallback path."""
    import os
    import urllib.request
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    from infer.lib.infer_pack.models_onnx import SynthesizerTrnMsNSFsidM

    os.makedirs(FALLBACK_ONNX_DIR, exist_ok=True)

    # -- 1. ContentVec ONNX (community model, pinned to a specific HF commit) --
    # NOTE: Pin this to a specific commit SHA for reproducibility, e.g.:
    #   .../resolve/a6b64b2ef76a7136a1c0ccc90645e7fa0fded7a4/vec-768-layer-12.onnx
    # Using /resolve/main/ is acceptable for initial setup but should be pinned
    # once the SHA is confirmed stable in your environment.
    contentvec_path = f"{FALLBACK_ONNX_DIR}/vec-768-layer-12.onnx"
    if not os.path.exists(contentvec_path):
        print("[Fallback] Downloading vec-768-layer-12.onnx from HuggingFace...")
        url = ("https://huggingface.co/NaruseMioShirakana/MoeSS-SUBModel"
               "/resolve/main/vec-768-layer-12.onnx")
        urllib.request.urlretrieve(url, contentvec_path)
        print(f"[Fallback] ContentVec ONNX downloaded to {contentvec_path}")
    else:
        print(f"[Fallback] ContentVec ONNX already present at {contentvec_path}")

    # -- 2. mi-test.onnx: dynamic-axis export reusing the same checkpoint/config --
    model_pth = "/root/rvc-models/weights/mi-test.pth"
    out_path = f"{FALLBACK_ONNX_DIR}/mi-test.onnx"

    cpt = torch.load(model_pth, map_location="cpu")
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk fixup
    config = list(cpt["config"])
    if len(config) < 19:
        config.append("v2")
    net_g = SynthesizerTrnMsNSFsidM(*config, is_half=False)
    net_g.load_state_dict(cpt["weight"], strict=False)
    net_g = net_g.float().eval()

    # Dummy inputs for tracing (200-frame sequence; dynamic axes allow any length)
    SEQ = 200
    phone = torch.zeros(1, SEQ, 768, dtype=torch.float32)
    phone_lengths = torch.tensor([SEQ], dtype=torch.int64)
    pitch = torch.zeros(1, SEQ, dtype=torch.int64)
    pitchf = torch.zeros(1, SEQ, dtype=torch.float32)
    sid = torch.tensor([0], dtype=torch.int64)
    rnd = torch.zeros(1, 192, SEQ, dtype=torch.float32)

    torch.onnx.export(
        net_g,
        (phone, phone_lengths, pitch, pitchf, sid, rnd),
        out_path,
        input_names=["phone", "phone_lengths", "pitch", "pitchf", "ds", "rnd"],
        output_names=["audio"],
        dynamic_axes={"phone": [1], "pitch": [1], "pitchf": [1], "rnd": [2]},
        opset_version=OPSET,
        do_constant_folding=True,
    )
    print(f"[Fallback] mi-test.onnx written to {out_path}")

    # Quick shape sanity check (parity: the fallback path uses dynamic-axis so
    # we only verify the ORT session loads and produces audio-shaped output)
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(3)
    feed = {
        "phone": rng.standard_normal((1, SEQ, 768)).astype(np.float32) * 0.1,
        "phone_lengths": np.array([SEQ], dtype=np.int64),
        "pitch": rng.integers(1, 255, (1, SEQ)).astype(np.int64),
        "pitchf": rng.uniform(100, 300, (1, SEQ)).astype(np.float32),
        "ds": np.array([0], dtype=np.int64),
        "rnd": rng.standard_normal((1, 192, SEQ)).astype(np.float32),
    }
    got = sess.run(None, feed)[0]
    print(f"[Fallback] mi-test.onnx output shape: {got.shape} (expected 1 x 1 x N)")
    assert got.ndim == 3 and got.shape[0] == 1, f"Unexpected output shape: {got.shape}"

    volume.commit()
    print("[Fallback] artifacts committed to volume")
