"""Export the three RVC inference models to static-shape ONNX on the L4.

Run individual exports:
    modal run modal_deploy/export_onnx.py::export_hubert
    modal run modal_deploy/export_onnx.py::export_generator
    modal run modal_deploy/export_onnx.py::export_rmvpe

Run all at once (recommended):
    modal run modal_deploy/export_onnx.py

Each exporter also runs a PyTorch-vs-ORT(CPU) parity check and hard-fails
on regression, so a green run IS the export test.

Context:
- BLOCK_MS=1000 + CONTEXT_MS=400 → 22400 input samples (CANONICAL_IN)
- Fixed TRT_T_PAD=16000 reflect pad each side (see implementation_plan.md Warning 3)
- Static shapes throughout — no dynamic_axes — for maximum TRT optimization
- ONNX files live on /root/rvc-models/onnx/ on the Modal volume, NOT in git
"""
import modal

try:
    from modal_deploy.worker import trt_image, volume
except ImportError:  # running with modal_deploy/ on sys.path
    from worker import trt_image, volume

app = modal.App("rvc-onnx-export")

# ---- Canonical static shapes (single source of truth; trt_pipeline.py mirrors these) ----
SR_IN = 16000
CANONICAL_IN = 22400          # 1400 ms: BLOCK_MS(1000ms) + CONTEXT_MS(400ms) in streaming.py
TRT_T_PAD = 16000             # fixed reflect pad each side (x_pad=1 equivalent — Warning 3)
PADDED_IN = CANONICAL_IN + 2 * TRT_T_PAD   # 54400
HUBERT_FRAMES = PADDED_IN // 320           # 170 (HuBERT window=160 samples, stride=320 @ fp)
GEN_FRAMES = HUBERT_FRAMES * 2             # 340 (post 2x interpolation; == p_len == f0 frames)
SR_OUT = 48000
OUT_PADDED_48K = GEN_FRAMES * 480          # 163200 (generator output @ 48kHz, with pads)
T_PAD_TGT = TRT_T_PAD * 3                  # 48000 (pad in output space, 3x ratio)
OUT_48K = OUT_PADDED_48K - 2 * T_PAD_TGT   # 67200 == 3 * CANONICAL_IN (usable output)

# RMVPE mel geometry for PADDED_IN samples: hop=160, center=True -> 341 frames,
# then RMVPE.mel2hidden zero-pads frame count to a multiple of 32 -> 352.
MEL_FRAMES = PADDED_IN // 160 + 1          # 341
MEL_FRAMES_PADDED = ((MEL_FRAMES + 31) // 32) * 32   # 352

ONNX_DIR = "/root/rvc-models/onnx"
OPSET = 17


def _setup_rvc_path():
    import os
    import sys
    os.chdir("/root/rvc")
    if "/root/rvc" not in sys.path:
        sys.path.insert(0, "/root/rvc")
    os.environ.setdefault("weight_root", "/root/rvc-models/weights")
    os.environ.setdefault("index_root", "/root/rvc-models/logs/mi-test")
    os.environ.setdefault("rmvpe_root", "/root/rvc/assets/rmvpe")


# ---------------------------------------------------------------------------
# Task 2: HuBERT export (Engine 1)
# ---------------------------------------------------------------------------

@app.function(image=trt_image, gpu="L4", timeout=1800,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_hubert():
    """Export HuBERT to ONNX.

    Input:  audio  float32[1, 54400]
    Output: feats  float32[1, 170, 768]

    Uses the same load_hubert() loader production uses. Wraps it in a minimal
    traceable nn.Module with padding_mask=None (fixes fairseq tracing issues).
    """
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    from configs.config import Config
    from infer.modules.vc.utils import load_hubert

    config = Config()
    hubert = load_hubert(config)      # same loader production uses
    hubert = hubert.float().eval()   # export in fp32; TRT applies fp16 at build time

    class HubertWrapper(torch.nn.Module):
        """Minimal traceable surface: fixed-length audio, no padding mask,
        RVC v2 = layer-12 output, 768-dim, no final_proj."""
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, audio):                      # [1, PADDED_IN] float32
            feats = self.model.extract_features(
                source=audio, padding_mask=None, output_layer=12,
            )[0]
            return feats                               # [1, HUBERT_FRAMES, 768]

    wrapper = HubertWrapper(hubert)
    dummy = torch.zeros(1, PADDED_IN, dtype=torch.float32)

    with torch.no_grad():
        ref_device = next(hubert.parameters()).device
        ref = wrapper(dummy.to(ref_device))
    assert ref.shape == (1, HUBERT_FRAMES, 768), f"unexpected feats shape {ref.shape}"
    print(f"[Export] HuBERT forward shape OK: {ref.shape}")

    os.makedirs(ONNX_DIR, exist_ok=True)
    out_path = f"{ONNX_DIR}/hubert.onnx"
    wrapper_cpu = wrapper.cpu()
    torch.onnx.export(
        wrapper_cpu, (dummy,), out_path,
        input_names=["audio"], output_names=["feats"],
        opset_version=OPSET, do_constant_folding=True,
        # NO dynamic_axes: static shapes are the point.
    )
    print(f"[Export] hubert.onnx written — running parity check")

    # ---- Parity check: realistic speech noise, not zeros ----
    rng = np.random.default_rng(0)
    speech = (rng.standard_normal(PADDED_IN) * 0.05).astype(np.float32)[None, :]
    with torch.no_grad():
        ref_np = wrapper_cpu(torch.from_numpy(speech)).numpy()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, {"audio": speech})[0]
    cos = float(np.sum(ref_np * got) / (np.linalg.norm(ref_np) * np.linalg.norm(got) + 1e-9))
    maxabs = float(np.abs(ref_np - got).max())
    print(f"[Export] hubert.onnx parity cosine={cos:.6f} maxabs={maxabs:.2e}")
    assert cos >= 0.999, (
        f"HuBERT ONNX parity FAILED (cosine={cos:.6f}) — "
        "see implementation_plan.md Warning 1 for the ContentVec fallback"
    )
    volume.commit()
    print(f"[Export] hubert.onnx committed to volume at {out_path}")


# ---------------------------------------------------------------------------
# Task 3: Generator export (Engine 2)
# ---------------------------------------------------------------------------

@app.function(image=trt_image, gpu="L4", timeout=1800,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_generator():
    """Export the RVC v2 Synthesizer generator to ONNX.

    Uses SynthesizerTrnMsNSFsidM from RVC's own models_onnx.py — the
    training-only rand-slicing is removed and noise is the explicit rnd input.

    Inputs:  phone         float32[1, 340, 768]
             phone_lengths int64[1]
             pitch         int64[1, 340]
             pitchf        float32[1, 340]
             sid           int64[1]
             rnd           float32[1, 192, 340]
    Output:  audio         float32[1, 1, 163200]
    """
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    from infer.lib.infer_pack.models_onnx import SynthesizerTrnMsNSFsidM

    cpt = torch.load("/root/rvc-models/weights/mi-test.pth", map_location="cpu")
    tgt_sr = cpt["config"][-1]
    assert tgt_sr == 48000, (
        f"mi-test.pth tgt_sr={tgt_sr}, but the whole pipeline contract is 48kHz "
        "(worker returns 3x samples with no resample) — stop and escalate."
    )
    # Same n_spk fixup that vc.get_vc() applies
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]
    net_g = SynthesizerTrnMsNSFsidM(*cpt["config"], version="v2", is_half=False)
    net_g.load_state_dict(cpt["weight"], strict=False)
    net_g = net_g.float().eval()

    phone = torch.zeros(1, GEN_FRAMES, 768, dtype=torch.float32)
    phone_lengths = torch.tensor([GEN_FRAMES], dtype=torch.int64)
    pitch = torch.zeros(1, GEN_FRAMES, dtype=torch.int64)
    pitchf = torch.zeros(1, GEN_FRAMES, dtype=torch.float32)
    sid = torch.tensor([0], dtype=torch.int64)
    rnd = torch.zeros(1, 192, GEN_FRAMES, dtype=torch.float32)
    args = (phone, phone_lengths, pitch, pitchf, sid, rnd)

    os.makedirs(ONNX_DIR, exist_ok=True)
    out_path = f"{ONNX_DIR}/generator.onnx"
    torch.onnx.export(
        net_g, args, out_path,
        input_names=["phone", "phone_lengths", "pitch", "pitchf", "sid", "rnd"],
        output_names=["audio"],
        opset_version=OPSET, do_constant_folding=True,
    )
    print(f"[Export] generator.onnx written — running parity check")

    # ---- Parity on realistic inputs ----
    rng = np.random.default_rng(1)
    feed = {
        "phone":         rng.standard_normal((1, GEN_FRAMES, 768)).astype(np.float32) * 0.1,
        "phone_lengths": np.array([GEN_FRAMES], dtype=np.int64),
        "pitch":         rng.integers(1, 255, (1, GEN_FRAMES)).astype(np.int64),
        "pitchf":        rng.uniform(100, 300, (1, GEN_FRAMES)).astype(np.float32),
        "sid":           np.array([0], dtype=np.int64),
        "rnd":           rng.standard_normal((1, 192, GEN_FRAMES)).astype(np.float32),
    }
    with torch.no_grad():
        ref = net_g(*[torch.from_numpy(v) for v in feed.values()])
        ref = (ref[0] if isinstance(ref, tuple) else ref).numpy()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, feed)[0]
    assert got.shape[-1] == OUT_PADDED_48K, (
        f"expected output length {OUT_PADDED_48K}, got shape {got.shape}"
    )
    cos = float(np.sum(ref * got) / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-9))
    print(f"[Export] generator.onnx parity cosine={cos:.6f}")
    assert cos >= 0.999, f"Generator ONNX parity FAILED (cosine={cos:.6f})"
    volume.commit()
    print(f"[Export] generator.onnx committed to volume at {out_path}")


# ---------------------------------------------------------------------------
# Task 4: RMVPE E2E export (Engine 3)
# ---------------------------------------------------------------------------

@app.function(image=trt_image, gpu="L4", timeout=1800,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_rmvpe():
    """Export RMVPE's E2E neural net to ONNX (mel frontend + cents decode stay outside).

    The mel spectrogram frontend (torch.stft) and to_local_average_cents decoding
    are deliberately excluded — torch.stft exports unreliably and the decode is
    pure NumPy anyway. Only the E2E CNN/BiGRU pitch model is compiled.

    Input:  mel    float32[1, 128, 352]   (MEL_FRAMES_PADDED=352)
    Output: hidden float32[1, 352, 360]
    """
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    from infer.lib.rmvpe import RMVPE

    # Try the container path first, then the volume upload location (Task 1 Step 4)
    pt_path = "/root/rvc/assets/rmvpe/rmvpe.pt"
    if not os.path.exists(pt_path):
        pt_path = "/root/rvc-models/assets/rmvpe/rmvpe.pt"
    assert os.path.exists(pt_path), (
        f"rmvpe.pt not found at either expected path. "
        f"Upload it: modal volume put rvc-models <local-path> assets/rmvpe/rmvpe.pt"
    )

    rmvpe = RMVPE(pt_path, is_half=False, device="cpu")
    net = rmvpe.model.float().eval()   # E2E nn.Module only — no mel, no decode

    dummy_mel = torch.zeros(1, 128, MEL_FRAMES_PADDED, dtype=torch.float32)
    os.makedirs(ONNX_DIR, exist_ok=True)
    out_path = f"{ONNX_DIR}/rmvpe.onnx"
    torch.onnx.export(
        net, (dummy_mel,), out_path,
        input_names=["mel"], output_names=["hidden"],
        opset_version=OPSET, do_constant_folding=True,
    )
    print(f"[Export] rmvpe.onnx written — running parity check")

    rng = np.random.default_rng(2)
    mel = rng.standard_normal((1, 128, MEL_FRAMES_PADDED)).astype(np.float32)
    with torch.no_grad():
        ref = net(torch.from_numpy(mel)).numpy()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, {"mel": mel})[0]
    assert got.shape == (1, MEL_FRAMES_PADDED, 360), (
        f"unexpected hidden shape {got.shape}, expected (1, {MEL_FRAMES_PADDED}, 360)"
    )
    cos = float(np.sum(ref * got) / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-9))
    print(f"[Export] rmvpe.onnx parity cosine={cos:.6f}")
    assert cos >= 0.999, (
        f"RMVPE ONNX parity FAILED (cosine={cos:.6f}). "
        "If BiGRU export fails at opset 17, retry with opset_version=18. "
        "If still failing, stop — RMVPE stays PyTorch (see implementation_plan.md Task 4 Step 2)."
    )
    volume.commit()
    print(f"[Export] rmvpe.onnx committed to volume at {out_path}")


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

@app.function(image=trt_image, gpu="L4", timeout=5400,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_all():
    """Run all three exports sequentially in one container (saves cold-start cost)."""
    export_hubert.local()
    export_generator.local()
    export_rmvpe.local()


@app.local_entrypoint()
def main():
    """Default: export all three engines. Run with: modal run modal_deploy/export_onnx.py"""
    print("[Export] Starting full export pipeline on L4 GPU...")
    export_all.remote()
    print("[Export] All three ONNX models exported and committed to volume.")
