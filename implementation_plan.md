# TensorRT Migration Implementation Plan — Keira RVC Worker (NVIDIA L4 / Modal)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the PyTorch inference path in the Modal RVC worker with three static-shape TensorRT engines (HuBERT, RVC generator, RMVPE) so RMVPE pitch tracking can be re-enabled at streaming-compatible latency, then reduce the playout buffer in a benchmark-gated phase.

**Architecture:** The vendored RVC `Pipeline.vc` is re-implemented as `modal_deploy/trt_pipeline.py`: audio is padded to one canonical static shape, HuBERT features are computed by Engine 1, FAISS index mixing stays in NumPy (cannot be a TRT engine), F0 comes from Engine 3 (RMVPE net only — mel frontend and cents decoding stay outside), and Engine 2 (SynthesizerTrnMs768NSFsid) synthesizes 48 kHz audio. Engines are ONNX models executed through `onnxruntime-gpu`'s `TensorrtExecutionProvider` with the engine cache persisted on the `rvc-models` Modal Volume (engines are SM89/TRT-version specific and must be built on the L4, never at image-build time).

**Tech Stack:** Modal (L4, `ap-southeast`, `max_containers=1`), PyTorch + fairseq (export only), ONNX opset 17, onnxruntime-gpu w/ TensorRT EP, FAISS (CPU path unchanged), NumPy/librosa DSP.

## Global Constraints

- **Fail-CLOSED, never raw**: no code path may ever emit the agent's unconverted voice. On any TRT/engine failure the worker emits nothing (existing `{"type":"error"}` + silence behavior). The PyTorch path remains as a *converted-voice* fallback only.
- **Audio contract unchanged**: input 16 kHz mono int16 PCM; output 48 kHz mono int16 PCM, exactly 3× the input sample count per block.
- **Streaming DSP unchanged in Phase 1**: `BLOCK_MS=1000`, `CONTEXT_MS=400`, SOLA crossfade, silence bypass, single-tenant `/ws`, `max_containers=1`, `region="ap-southeast"` all stay as-is.
- **Canonical static shape**: 22,400 input samples (1400 ms @ 16 kHz) + fixed 16,000-sample reflect pad each side = **54,400 samples** into HuBERT; 170 HuBERT frames → 340 after 2× interpolation; generator output 163,200 samples @ 48 kHz, trimmed by 48,000 each side → **67,200 samples (3 × 22,400)**.
- **`modal deploy` is a live-infra mutation — USER-RUN ONLY.** Every step marked `[USER-RUN]` must be executed (or explicitly approved) by the project owner. `modal run` jobs are ephemeral but incur GPU cost — announce before running.
- **Committed ≠ deployed** on Modal (confirmed incident 2026-07-03): after any worker change, verify live state via `GET /health`, never via git.
- **Render auto-deploys on every push to `main`** — never push during a live test call.
- Never commit model artifacts (`.pth`, `.index`, `.onnx`, `.engine`, `.wav`) — ONNX/engine files live on the `rvc-models` volume only.
- Every new sibling module under `modal_deploy/` MUST be added to the image via `.add_local_python_source(...)` — Modal does not auto-trace imports (confirmed incident 2026-07-03, `streaming.py`).
- Playout buffer decision (approved 2026-07-05): Phase 1 target = **1.25 s** (not 0.25 s); Phase 2 (0.25 s + smaller `BLOCK_MS`) is gated on the Task 9 benchmark.

---

## 1. User Review Required / Warnings

1. **fairseq HuBERT is the most likely export blocker.** `HubertModel.extract_features` contains padding-mask logic, layer-drop, and config plumbing that `torch.onnx.export` chokes on. The plan wraps it in a minimal `nn.Module` with `padding_mask=None` and eval-mode (layerdrop inert). **Fallback (pre-approved here):** use the community-standard ContentVec `vec-768-layer-12.onnx` export (same checkpoint lineage as `hubert_base.pt` for RVC v2) and gate it on the Task 2 parity check (cosine similarity ≥ 0.999 vs `load_hubert` output). If parity fails for both routes, stop and escalate.
2. **`rmvpe.pt` may not exist in the container.** Production currently uses `"pm"`, so `/root/rvc/assets/rmvpe/rmvpe.pt` has never been exercised live. Task 1 probes for it; if missing it must be uploaded to the `rvc-models` volume (`modal volume put`) before Task 4 — flag to the user, do not silently download from the internet inside the worker.
3. **Fixed 16,000-sample pad deviates from `Config.x_pad`.** The vendored pipeline pads by `t_pad = 16000 × config.x_pad`, and `x_pad` varies with GPU/half-precision config (can be 3 → 48,000 samples/side, a 5.3× compute blowup for streaming). This plan pins the pad to 16,000 samples/side (the low-mem RVC setting). This changes edge behavior vs current production — the Task 9 A/B listen test is the gate; if edge artifacts appear, raise `TRT_T_PAD` and re-export (all shape constants derive from it).
4. **Toolchain risk (highest schedule risk).** The image pins Python 3.10 and `pip<24.1` (fairseq/omegaconf). `onnxruntime-gpu` + `tensorrt-cu12` wheels must coexist with the existing torch/fairseq pins, and ORT's TRT EP needs `LD_LIBRARY_PATH` pointed at `tensorrt_libs`. Task 1 is a hard gate: nothing else starts until the version probe passes on the L4. Candidate pins: `onnx==1.16.*`, `onnxruntime-gpu==1.18.*`, `tensorrt-cu12==10.0.*` (ORT 1.18's TRT EP targets TRT 10.0 / CUDA 12). If the existing torch is CUDA 11.x, exports still work (export is version-agnostic ONNX) but the runtime image may need a CUDA-12 torch bump — surface to user before changing torch.
5. **TRT engines are L4- and TRT-version-specific.** The engine cache on the volume is invalid after any GPU tier change or ORT/TRT upgrade. A cold cache means the first container boot pays a multi-minute engine build on top of the existing ~75 s cold start. Mitigation: `compile_trt.py` primes the cache as an explicit job; `/health` exposes `trt_cache: "hot"|"cold"` so a stale cache is visible before a call is attempted.
6. **FP16 drift is expected and acceptable.** `trt_fp16_enable=True` will not be bit-exact vs the PyTorch fp16 reference. The quality gate is the A/B listen + parity thresholds (feature cosine ≥ 0.999, F0 mean abs deviation ≤ 5 cents on voiced frames), not bitwise equality.
7. **Playout buffer change reverses part of the 2026-07-03 "continuity over latency" decision** — explicitly re-approved by the user on 2026-07-05 as a *phased* reversal: 1.25 s now (safe floor for 1000 ms block arrival granularity), 0.25 s + smaller `BLOCK_MS` only if Task 9 shows p95 per-block wall time ≤ 40% of `BLOCK_MS`. Update `.agents/decisions/log.md` when Phase 1 ships.
8. **`int64` inputs (`pitch`, `sid`, `phone_lengths`) force ORT to partition around TRT-unsupported casts in some TRT versions.** If the Task 7 provider-assignment dump shows the generator's embedding lookups falling back to `CUDAExecutionProvider`, that is acceptable (they are trivial ops); only flag if >10% of nodes fall off TRT.

---

## 2. File Structure

| File | Status | Responsibility |
|---|---|---|
| `modal_deploy/export_onnx.py` | **Create** | Modal-run job on L4: loads PyTorch weights, exports 3 static-shape ONNX models to `/root/rvc-models/onnx/`, runs PyTorch-vs-ORT parity checks. |
| `modal_deploy/compile_trt.py` | **Create** | Modal-run job on L4: builds ORT TRT sessions with `trt_engine_cache_path` on the volume, runs a dummy pass to materialize `.engine` caches, commits volume. |
| `modal_deploy/trt_pipeline.py` | **Create** | Runtime module: `TRTVoicePipeline` (pad → HuBERT engine → FAISS mix → RMVPE engine F0 → generator engine → trim → rms/protect) + pure-NumPy helpers. No Modal imports — locally testable. |
| `modal_deploy/test_trt_pipeline.py` | **Create** | Local pytest for the pure-NumPy helpers (padding, f0→coarse, cents decode, change_rms, protect mix). |
| `modal_deploy/worker.py` | **Modify** | Image deps + `add_local_python_source("trt_pipeline")`, `USE_TRT` startup branch, `convert_block()` routing for `/ws` and `convert_file_chunked`, `/health` TRT fields. |
| `backend/pipeline.py` | **Modify** | `_PLAYOUT_BUFFER_TARGET_BYTES` 3.0 s → 1.25 s (Task 11, after live verification). |

Volume layout added: `/root/rvc-models/onnx/{hubert,generator,rmvpe}.onnx`, `/root/rvc-models/trt_cache/` (ORT-managed `.engine` files).

---

### Task 1: Toolchain & Asset Probe (hard gate)

**Files:**
- Modify: `modal_deploy/worker.py` (image definition only — new `trt_image` variable, no runtime changes yet)
- Create: `modal_deploy/compile_trt.py` (probe function only in this task; grows in Task 7)

**Interfaces:**
- Produces: `trt_image` (a `modal.Image` in `worker.py`, importable as `from modal_deploy.worker import trt_image` — wait, `compile_trt.py` defines its own app but reuses the image via import; exact form below) and a passing `probe` run proving: ORT sees `TensorrtExecutionProvider` on the L4; `hubert_base.pt` and `rmvpe.pt` exist.

- [ ] **Step 1: Add the TRT image layer in `worker.py`**

Directly below the existing `image = (...)` block in `modal_deploy/worker.py`, add:

```python
# TRT runtime image: existing image + ONNX/ORT/TensorRT stack. Kept as a separate
# variable so the still-deployed PyTorch fastapi_app is untouched until Task 8
# flips it. tensorrt-cu12 ships its shared libs inside site-packages/tensorrt_libs;
# ORT's TensorRT EP dlopen()s them via LD_LIBRARY_PATH, it does NOT find them itself.
trt_image = (
    image
    .pip_install(
        "onnx==1.16.1",
        "onnxruntime-gpu==1.18.1",
        "tensorrt-cu12==10.0.1",
    )
    .env({
        "LD_LIBRARY_PATH": "/usr/local/lib/python3.10/site-packages/tensorrt_libs:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/cuda_runtime/lib"
    })
)
```

- [ ] **Step 2: Write the probe in `modal_deploy/compile_trt.py`**

```python
"""TRT engine-cache management for the Keira RVC worker.

Task 1: environment/asset probe. Task 7 adds the cache-priming job.
Run: modal run modal_deploy/compile_trt.py::probe
"""
import modal

try:
    from modal_deploy.worker import trt_image, volume
except ImportError:  # running with modal_deploy/ on sys.path
    from worker import trt_image, volume

app = modal.App("rvc-trt-tools")


@app.function(image=trt_image, gpu="L4", timeout=600,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def probe() -> dict:
    import os
    import onnxruntime as ort
    import torch

    report = {
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "ort": ort.__version__,
        "ort_providers": ort.get_available_providers(),
        "hubert_pt_exists": os.path.exists("/root/rvc/assets/hubert/hubert_base.pt"),
        "rmvpe_pt_exists": os.path.exists("/root/rvc/assets/rmvpe/rmvpe.pt"),
        "rmvpe_pt_on_volume": os.path.exists("/root/rvc-models/assets/rmvpe/rmvpe.pt"),
        "index_files": __import__("glob").glob("/root/rvc-models/logs/mi-test/*.index"),
    }
    for k, v in report.items():
        print(f"[Probe] {k}: {v}")
    assert "TensorrtExecutionProvider" in report["ort_providers"], \
        "TRT EP missing — check LD_LIBRARY_PATH / tensorrt-cu12 install"
    return report


@app.local_entrypoint()
def main():
    probe.remote()
```

- [ ] **Step 3: Run the probe** *(announce GPU cost to user first)*

Run: `modal run modal_deploy/compile_trt.py::probe`
Expected: prints all report lines; `TensorrtExecutionProvider` in providers; no assertion error. **If the assertion fails**: iterate on the pins in Step 1 (this is the anticipated failure mode — see Warning 4). Do not proceed to Task 2 until green.

- [ ] **Step 4: Handle a missing `rmvpe.pt`**

If `rmvpe_pt_exists` and `rmvpe_pt_on_volume` are both `False`: STOP and tell the user to upload it:
`modal volume put rvc-models <local-path-to-rmvpe.pt> assets/rmvpe/rmvpe.pt`
Then re-run the probe. (Runtime code in later tasks checks both locations, container first.)

- [ ] **Step 5: Commit**

```bash
git add modal_deploy/worker.py modal_deploy/compile_trt.py
git commit -m "feat(trt): add TRT runtime image layer and L4 environment probe"
```

---

### Task 2: `export_onnx.py` — HuBERT (Engine 1 source)

**Files:**
- Create: `modal_deploy/export_onnx.py`

**Interfaces:**
- Consumes: `trt_image`, `volume` from `worker.py` (Task 1).
- Produces: `/root/rvc-models/onnx/hubert.onnx` — input `audio: float32[1, 54400]`, output `feats: float32[1, 170, 768]`. Also the module-level shape constants that Tasks 3–6 reuse: `CANONICAL_IN=22400`, `TRT_T_PAD=16000`, `PADDED_IN=54400`, `HUBERT_FRAMES=170`, `GEN_FRAMES=340`, `OUT_48K=67200`.

- [ ] **Step 1: Create the exporter skeleton with shared constants**

```python
"""Export the three RVC inference models to static-shape ONNX on the L4.

Run: modal run modal_deploy/export_onnx.py::export_all
Each exporter also runs a PyTorch-vs-ORT(CPU/CUDA) parity check and hard-fails
on regression, so a green run IS the export test.
"""
import modal

try:
    from modal_deploy.worker import trt_image, volume
except ImportError:
    from worker import trt_image, volume

app = modal.App("rvc-onnx-export")

# ---- Canonical static shapes (single source of truth; trt_pipeline.py mirrors these) ----
SR_IN = 16000
CANONICAL_IN = 22400          # 1400 ms: BLOCK_SAMPLES_IN + CONTEXT_SAMPLES_IN (streaming.py)
TRT_T_PAD = 16000             # fixed reflect pad each side (x_pad=1 equivalent — Warning 3)
PADDED_IN = CANONICAL_IN + 2 * TRT_T_PAD   # 54400
HUBERT_FRAMES = PADDED_IN // 320           # 170
GEN_FRAMES = HUBERT_FRAMES * 2             # 340 (post 2x interpolation; == p_len == f0 frames)
SR_OUT = 48000
OUT_PADDED_48K = GEN_FRAMES * 480          # 163200
T_PAD_TGT = TRT_T_PAD * 3                  # 48000
OUT_48K = OUT_PADDED_48K - 2 * T_PAD_TGT   # 67200 == 3 * CANONICAL_IN

ONNX_DIR = "/root/rvc-models/onnx"
OPSET = 17


def _setup_rvc_path():
    import os, sys
    os.chdir("/root/rvc")
    if "/root/rvc" not in sys.path:
        sys.path.insert(0, "/root/rvc")
    os.environ.setdefault("weight_root", "/root/rvc-models/weights")
    os.environ.setdefault("index_root", "/root/rvc-models/logs/mi-test")
    os.environ.setdefault("rmvpe_root", "/root/rvc/assets/rmvpe")
```

- [ ] **Step 2: Add the HuBERT export function**

```python
@app.function(image=trt_image, gpu="L4", timeout=1800,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_hubert():
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    from configs.config import Config
    from infer.modules.vc.utils import load_hubert

    config = Config()
    hubert = load_hubert(config)          # same loader production uses
    hubert = hubert.float().eval()        # export in fp32; TRT applies fp16 at build

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
        ref = wrapper(dummy.cuda() if next(hubert.parameters()).is_cuda else dummy)
    assert ref.shape == (1, HUBERT_FRAMES, 768), f"unexpected feats shape {ref.shape}"

    os.makedirs(ONNX_DIR, exist_ok=True)
    out_path = f"{ONNX_DIR}/hubert.onnx"
    wrapper_cpu = wrapper.cpu()
    torch.onnx.export(
        wrapper_cpu, (dummy,), out_path,
        input_names=["audio"], output_names=["feats"],
        opset_version=OPSET, do_constant_folding=True,
        # NO dynamic_axes: static shapes are the point.
    )

    # ---- Parity check: real speech, not zeros ----
    rng = np.random.default_rng(0)
    speech = (rng.standard_normal(PADDED_IN) * 0.05).astype(np.float32)[None, :]
    with torch.no_grad():
        ref = wrapper_cpu(torch.from_numpy(speech)).numpy()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, {"audio": speech})[0]
    cos = float(np.sum(ref * got) / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-9))
    print(f"[Export] hubert.onnx parity cosine={cos:.6f} maxabs={np.abs(ref-got).max():.2e}")
    assert cos >= 0.999, "HuBERT ONNX parity failed — see Warning 1 fallback"
    volume.commit()
    print(f"[Export] wrote {out_path}")
```

- [ ] **Step 3: Run the export** *(announce GPU cost)*

Run: `modal run modal_deploy/export_onnx.py::export_hubert`
Expected: `parity cosine=1.000000` (or ≥ 0.999) and `wrote /root/rvc-models/onnx/hubert.onnx`.
**If `torch.onnx.export` raises** (fairseq tracing — Warning 1): fall back to the prebuilt ContentVec `vec-768-layer-12.onnx`: ask the user to obtain it, `modal volume put rvc-models <path> onnx/hubert.onnx`, then re-run **only the parity block** (add a `verify_hubert` function that loads the volume copy and runs the same cosine check against `load_hubert`). Parity gate is identical.

- [ ] **Step 4: Commit**

```bash
git add modal_deploy/export_onnx.py
git commit -m "feat(trt): ONNX export + parity check for HuBERT (engine 1)"
```

---

### Task 3: `export_onnx.py` — Generator (Engine 2 source)

**Files:**
- Modify: `modal_deploy/export_onnx.py`

**Interfaces:**
- Consumes: shape constants from Task 2.
- Produces: `/root/rvc-models/onnx/generator.onnx` — inputs `phone: float32[1, 340, 768]`, `phone_lengths: int64[1]`, `pitch: int64[1, 340]`, `pitchf: float32[1, 340]`, `sid: int64[1]`, `rnd: float32[1, 192, 340]`; output `audio: float32[1, 1, 163200]`.

- [ ] **Step 1: Add the generator export function**

Append to `modal_deploy/export_onnx.py`:

```python
@app.function(image=trt_image, gpu="L4", timeout=1800,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_generator():
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    # models_onnx.SynthesizerTrnMsNSFsidM is RVC's own export-ready variant:
    # training-only rand-slicing removed, noise is the explicit `rnd` input.
    from infer.lib.infer_pack.models_onnx import SynthesizerTrnMsNSFsidM

    cpt = torch.load("/root/rvc-models/weights/mi-test.pth", map_location="cpu")
    tgt_sr = cpt["config"][-1]
    assert tgt_sr == 48000, (
        f"mi-test.pth tgt_sr={tgt_sr}, but the whole pipeline contract is 48kHz "
        "(worker returns 3x samples with no resample) — stop and escalate."
    )
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk, same fixup vc.get_vc does
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

    # ---- Parity on realistic inputs ----
    rng = np.random.default_rng(1)
    feed = {
        "phone": rng.standard_normal((1, GEN_FRAMES, 768)).astype(np.float32) * 0.1,
        "phone_lengths": np.array([GEN_FRAMES], dtype=np.int64),
        "pitch": rng.integers(1, 255, (1, GEN_FRAMES)).astype(np.int64),
        "pitchf": rng.uniform(100, 300, (1, GEN_FRAMES)).astype(np.float32),
        "sid": np.array([0], dtype=np.int64),
        "rnd": rng.standard_normal((1, 192, GEN_FRAMES)).astype(np.float32),
    }
    with torch.no_grad():
        ref = net_g(*[torch.from_numpy(v) for v in feed.values()])
        ref = (ref[0] if isinstance(ref, tuple) else ref).numpy()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, feed)[0]
    assert got.shape[-1] == OUT_PADDED_48K, f"expected {OUT_PADDED_48K} samples, got {got.shape}"
    cos = float(np.sum(ref * got) / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-9))
    print(f"[Export] generator.onnx parity cosine={cos:.6f}")
    assert cos >= 0.999
    volume.commit()
    print(f"[Export] wrote {out_path}")
```

- [ ] **Step 2: Run the export** *(announce GPU cost)*

Run: `modal run modal_deploy/export_onnx.py::export_generator`
Expected: shape assertion passes (output length 163,200), `parity cosine≥0.999`, `wrote .../generator.onnx`. If `SynthesizerTrnMsNSFsidM`'s forward signature differs from `(phone, phone_lengths, pitch, nsff0, sid, rnd)` (RVC forks vary), read `RVC/infer/lib/infer_pack/models_onnx.py` and match it exactly — that file is the source of truth, and `RVC/infer/modules/onnx/export.py` shows the reference call.

- [ ] **Step 3: Commit**

```bash
git add modal_deploy/export_onnx.py
git commit -m "feat(trt): ONNX export + parity check for RVC generator (engine 2)"
```

---

### Task 4: `export_onnx.py` — RMVPE E2E (Engine 3 source)

**Files:**
- Modify: `modal_deploy/export_onnx.py`

**Interfaces:**
- Consumes: shape constants from Task 2.
- Produces: `/root/rvc-models/onnx/rmvpe.onnx` — input `mel: float32[1, 128, 352]`, output `hidden: float32[1, 352, 360]`. Mel frontend (torch.stft) and `to_local_average_cents` decoding deliberately stay OUTSIDE the engine (Warning: torch.stft exports unreliably). `MEL_FRAMES=341`, `MEL_FRAMES_PADDED=352` become shared constants.

- [ ] **Step 1: Add mel-shape constants and the RMVPE export function**

Append to `modal_deploy/export_onnx.py` (constants near the top with the others):

```python
# RMVPE mel geometry for PADDED_IN samples: hop 160, center=True -> 341 frames,
# then RMVPE.mel2hidden zero-pads frame count to a multiple of 32 -> 352.
MEL_FRAMES = PADDED_IN // 160 + 1          # 341
MEL_FRAMES_PADDED = ((MEL_FRAMES + 31) // 32) * 32   # 352
```

```python
@app.function(image=trt_image, gpu="L4", timeout=1800,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_rmvpe():
    import os
    import numpy as np
    import torch
    import onnxruntime as ort
    _setup_rvc_path()
    from infer.lib.rmvpe import RMVPE

    pt_path = "/root/rvc/assets/rmvpe/rmvpe.pt"
    if not os.path.exists(pt_path):
        pt_path = "/root/rvc-models/assets/rmvpe/rmvpe.pt"   # Task 1 Step 4 upload location
    rmvpe = RMVPE(pt_path, is_half=False, device="cpu")
    net = rmvpe.model.float().eval()       # the E2E nn.Module ONLY — no mel, no decode

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
    assert cos >= 0.999
    volume.commit()
    print(f"[Export] wrote {out_path}")


@app.function(image=trt_image, gpu="L4", timeout=1800,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def export_all():
    export_hubert.local()
    export_generator.local()
    export_rmvpe.local()


@app.local_entrypoint()
def main():
    export_all.remote()
```

- [ ] **Step 2: Run the export** *(announce GPU cost)*

Run: `modal run modal_deploy/export_onnx.py::export_rmvpe`
Expected: `hidden` shape `(1, 352, 360)`, parity cosine ≥ 0.999. If the E2E net's deformable/BiGRU layers fail export at opset 17, retry with `opset_version=18`; if still failing, STOP — RMVPE stays PyTorch, and ship Engines 1+2 only with `f0_method="rmvpe-torch"` (still a win vs `pm`; escalate to user for the call).

- [ ] **Step 3: Commit**

```bash
git add modal_deploy/export_onnx.py
git commit -m "feat(trt): ONNX export + parity check for RMVPE E2E net (engine 3)"
```

---

### Task 5: `trt_pipeline.py` — pure-NumPy helpers (TDD, runs locally)

**Files:**
- Create: `modal_deploy/trt_pipeline.py`
- Create: `modal_deploy/test_trt_pipeline.py`

**Interfaces:**
- Produces (consumed by Task 6's `TRTVoicePipeline` and its tests):
  - `pad_to_canonical(pcm_int16: np.ndarray) -> tuple[np.ndarray, int]` — returns `(float32 audio of len PADDED_IN, left_zero_pad_samples)`
  - `f0_to_coarse(f0: np.ndarray) -> np.ndarray` — int64 in [1, 255], RVC's mel-scale mapping
  - `decode_f0(hidden: np.ndarray, thred: float = 0.03) -> np.ndarray` — RMVPE `to_local_average_cents` port, `[MEL_FRAMES]` Hz (0 = unvoiced)
  - `change_rms(source_16k, out_48k, rate) -> np.ndarray` — RMS envelope mix port
  - `apply_protect(feats, feats_raw, pitchf, protect) -> np.ndarray` — consonant protection port
  - Constants mirrored from `export_onnx.py`: `CANONICAL_IN, TRT_T_PAD, PADDED_IN, HUBERT_FRAMES, GEN_FRAMES, SR_IN, SR_OUT, OUT_48K, MEL_FRAMES, MEL_FRAMES_PADDED`

- [ ] **Step 1: Write the failing tests**

Create `modal_deploy/test_trt_pipeline.py`:

```python
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
    # center region is the normalized input
    center = audio[tp.TRT_T_PAD: tp.TRT_T_PAD + tp.CANONICAL_IN]
    assert np.allclose(center, 1000 / 32768.0)


def test_pad_to_canonical_short_first_block():
    # first streaming block: 16000 samples, no context yet
    pcm = np.ones(16000, dtype=np.int16)
    audio, zpad = tp.pad_to_canonical(pcm)
    assert audio.shape == (tp.PADDED_IN,)
    assert zpad == tp.CANONICAL_IN - 16000        # 6400
    # zero-fill sits at the head of the canonical region
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
    assert c[1] < c[2] < c[3]          # monotonic in voiced range
    assert c[3] == 255 and c[4] == 255  # clamped at f0_max


def test_decode_f0_peak():
    # a clean salience peak at bin 180 must decode near its cent value
    hidden = np.zeros((1, tp.MEL_FRAMES_PADDED, 360), dtype=np.float32)
    hidden[0, :, 180] = 1.0
    f0 = tp.decode_f0(hidden)
    assert f0.shape == (tp.MEL_FRAMES,)
    cents = 20 * 180 + 1997.3794084376191
    expected_hz = 10 * 2 ** (cents / 1200)
    assert np.allclose(f0, expected_hz, rtol=1e-3)


def test_decode_f0_unvoiced_is_zero():
    hidden = np.full((1, tp.MEL_FRAMES_PADDED, 360), 0.001, dtype=np.float32)  # below thred
    f0 = tp.decode_f0(hidden, thred=0.03)
    assert np.all(f0 == 0.0)


def test_change_rms_identity_at_rate_1():
    rng = np.random.default_rng(3)
    src = rng.standard_normal(tp.CANONICAL_IN).astype(np.float32) * 0.1
    out = rng.standard_normal(tp.OUT_48K).astype(np.float32) * 0.5
    mixed = tp.change_rms(src, out.copy(), rate=1.0)
    assert np.allclose(mixed, out, atol=1e-5)     # rate=1 -> output envelope untouched


def test_apply_protect_passthrough_when_disabled():
    rng = np.random.default_rng(4)
    feats = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    raw = rng.standard_normal((1, tp.GEN_FRAMES, 768)).astype(np.float32)
    pitchf = rng.uniform(0, 300, tp.GEN_FRAMES).astype(np.float32)
    out = tp.apply_protect(feats, raw, pitchf, protect=0.5)
    assert np.array_equal(out, feats)             # protect >= 0.5 is "off" in RVC


def test_apply_protect_blends_unvoiced():
    feats = np.ones((1, tp.GEN_FRAMES, 768), dtype=np.float32)
    raw = np.zeros((1, tp.GEN_FRAMES, 768), dtype=np.float32)
    pitchf = np.zeros(tp.GEN_FRAMES, dtype=np.float32)   # all unvoiced
    out = tp.apply_protect(feats, raw, pitchf, protect=0.33)
    # unvoiced frames: feats*protect + raw*(1-protect) = 0.33
    assert np.allclose(out, 0.33, atol=1e-6)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest modal_deploy/test_trt_pipeline.py -v`
Expected: FAIL / collection error — `ModuleNotFoundError: No module named 'trt_pipeline'`.

- [ ] **Step 3: Implement the helpers**

Create `modal_deploy/trt_pipeline.py`:

```python
"""TRT-backed re-implementation of RVC's Pipeline.vc for Keira's streaming worker.

Pure-NumPy helpers + TRTVoicePipeline (ORT/TensorRT sessions). This module must
stay importable WITHOUT Modal or GPU libs at module level, so the helper half is
unit-testable locally. It is mounted into the container via
.add_local_python_source("trt_pipeline") — see worker.py's image (Modal does NOT
auto-bundle sibling modules; confirmed incident 2026-07-03 with streaming.py).
"""
import numpy as np

# ---- Canonical static shapes (MUST mirror export_onnx.py) ----
SR_IN = 16000
SR_OUT = 48000
CANONICAL_IN = 22400
TRT_T_PAD = 16000
PADDED_IN = CANONICAL_IN + 2 * TRT_T_PAD      # 54400
HUBERT_FRAMES = PADDED_IN // 320              # 170
GEN_FRAMES = HUBERT_FRAMES * 2                # 340
OUT_PADDED_48K = GEN_FRAMES * 480             # 163200
T_PAD_TGT = TRT_T_PAD * 3                     # 48000
OUT_48K = OUT_PADDED_48K - 2 * T_PAD_TGT      # 67200
MEL_FRAMES = PADDED_IN // 160 + 1             # 341
MEL_FRAMES_PADDED = ((MEL_FRAMES + 31) // 32) * 32   # 352

# RVC f0 mapping constants (verbatim from RVC/infer/modules/vc/pipeline.py)
F0_MIN, F0_MAX = 50.0, 1100.0
F0_MEL_MIN = 1127.0 * np.log(1.0 + F0_MIN / 700.0)
F0_MEL_MAX = 1127.0 * np.log(1.0 + F0_MAX / 700.0)

# RMVPE cents mapping (verbatim from RVC/infer/lib/rmvpe.py)
_CENTS_MAPPING = 20.0 * np.arange(360) + 1997.3794084376191


def pad_to_canonical(pcm_int16: np.ndarray) -> tuple[np.ndarray, int]:
    """int16 block (<= CANONICAL_IN samples) -> (float32[PADDED_IN], left_zero_pad).

    Short first-of-session blocks are zero-filled at the HEAD of the canonical
    region (they lack left context); the fixed TRT_T_PAD reflect pad is then
    applied outside that. left_zero_pad lets the caller trim 3x that many
    samples off the 48 kHz output head.
    """
    pcm_int16 = np.asarray(pcm_int16, dtype=np.int16)
    if len(pcm_int16) > CANONICAL_IN:
        raise ValueError(f"block of {len(pcm_int16)} exceeds canonical {CANONICAL_IN}")
    zpad = CANONICAL_IN - len(pcm_int16)
    audio = pcm_int16.astype(np.float32) / 32768.0
    canonical = np.concatenate([np.zeros(zpad, dtype=np.float32), audio])
    padded = np.pad(canonical, (TRT_T_PAD, TRT_T_PAD), mode="reflect")
    return padded, zpad


def f0_to_coarse(f0: np.ndarray) -> np.ndarray:
    """Hz -> RVC's 1..255 mel-quantized pitch codes (port of get_f0's tail)."""
    f0_mel = 1127.0 * np.log(1.0 + np.asarray(f0, dtype=np.float64) / 700.0)
    voiced = f0_mel > 0
    f0_mel[voiced] = (f0_mel[voiced] - F0_MEL_MIN) * 254.0 / (F0_MEL_MAX - F0_MEL_MIN) + 1.0
    f0_mel[f0_mel <= 1.0] = 1.0
    f0_mel[f0_mel > 255.0] = 255.0
    return np.rint(f0_mel).astype(np.int64)


def decode_f0(hidden: np.ndarray, thred: float = 0.03) -> np.ndarray:
    """RMVPE hidden [1, MEL_FRAMES_PADDED, 360] -> f0 Hz [MEL_FRAMES].

    Port of RMVPE.decode/to_local_average_cents: local weighted average of
    cents in a +/-4 bin window around the argmax, zeroed where peak < thred.
    """
    salience = np.asarray(hidden, dtype=np.float32)[0, :MEL_FRAMES]      # [T, 360]
    padded = np.pad(salience, ((0, 0), (4, 4)))
    centers = np.argmax(salience, axis=1)
    f0 = np.zeros(MEL_FRAMES, dtype=np.float64)
    for i, c in enumerate(centers):
        window = padded[i, c: c + 9]                       # 9 bins centered on c
        cents_win = _CENTS_MAPPING[max(0, c - 4): c + 5]
        if len(cents_win) < len(window):                   # edge bins
            window = window[: len(cents_win)]
        denom = window.sum()
        if denom > 0:
            cents = float((window * cents_win).sum() / denom)
            f0[i] = 10.0 * 2.0 ** (cents / 1200.0)
    f0[salience.max(axis=1) <= thred] = 0.0
    return f0


def change_rms(source_16k: np.ndarray, out_48k: np.ndarray, rate: float) -> np.ndarray:
    """Port of pipeline.py change_rms: blend output loudness envelope toward the
    source's. rate=1 leaves the converted output's own envelope untouched."""
    if rate >= 1.0:
        return out_48k
    import librosa
    rms1 = librosa.feature.rms(y=source_16k, frame_length=SR_IN // 2 * 2,
                               hop_length=SR_IN // 2)[0]
    rms2 = librosa.feature.rms(y=out_48k, frame_length=SR_OUT // 2 * 2,
                               hop_length=SR_OUT // 2)[0]
    n = len(out_48k)
    rms1 = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(rms1)), rms1)
    rms2 = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(rms2)), rms2)
    rms2 = np.maximum(rms2, 1e-6)
    return out_48k * (rms1 / rms2) ** (1.0 - rate)


def apply_protect(feats: np.ndarray, feats_raw: np.ndarray,
                  pitchf: np.ndarray, protect: float) -> np.ndarray:
    """Port of Pipeline.vc's consonant protection: on unvoiced frames
    (pitchf==0), blend index-mixed feats back toward the raw HuBERT feats.
    protect >= 0.5 disables (RVC convention)."""
    if protect >= 0.5:
        return feats
    mask = (np.asarray(pitchf) == 0.0)[None, :, None]      # [1, T, 1]
    blended = feats * protect + feats_raw * (1.0 - protect)
    return np.where(mask, blended, feats).astype(np.float32)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest modal_deploy/test_trt_pipeline.py -v`
Expected: 9 passed. (`librosa` is only imported inside `change_rms` when `rate<1`; the rate=1 test avoids needing it locally — if you have it installed, also spot-check `rate=0.75` manually.)

- [ ] **Step 5: Commit**

```bash
git add modal_deploy/trt_pipeline.py modal_deploy/test_trt_pipeline.py
git commit -m "feat(trt): pure-numpy pipeline helpers with local unit tests"
```

---

### Task 6: `trt_pipeline.py` — `TRTVoicePipeline` (ORT sessions + full block conversion)

**Files:**
- Modify: `modal_deploy/trt_pipeline.py`

**Interfaces:**
- Consumes: Task 5 helpers; ONNX files from Tasks 2–4; the FAISS index object worker.py already loads/caches.
- Produces (consumed by worker.py in Task 8):
  - `TRTVoicePipeline(onnx_dir: str, cache_dir: str, index, big_npy, mel_extractor, device: str)`
  - `.convert_block(pcm_int16: np.ndarray, pitch_shift: int, index_rate: float, rms_mix_rate: float, protect: float, filter_radius: int = 3) -> np.ndarray` — int16 @48 kHz, length exactly `3 * len(pcm_int16)`
  - `.warmup()` — one full dummy pass (builds/loads TRT engines; call from `startup()`)

- [ ] **Step 1: Append the pipeline class**

```python
class TRTVoicePipeline:
    """RVC voice conversion over 3 static-shape ORT/TensorRT sessions.

    FAISS mixing, F0 decode, protect and RMS logic run in NumPy between engine
    calls — a faithful port of RVC's Pipeline.vc for one fixed block geometry.
    """

    def __init__(self, onnx_dir: str, cache_dir: str, index, big_npy,
                 mel_extractor, device: str = "cuda"):
        import onnxruntime as ort
        self.index = index                # faiss index (worker's cached loader)
        self.big_npy = big_npy            # full reconstruct_n array (cached)
        self.mel = mel_extractor          # RMVPE's torch MelSpectrogram module (GPU)
        self.device = device
        providers = [
            ("TensorrtExecutionProvider", {
                "trt_engine_cache_enable": True,
                "trt_engine_cache_path": cache_dir,
                "trt_timing_cache_enable": True,
                "trt_fp16_enable": True,
            }),
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        opts = ort.SessionOptions()
        self.s_hubert = ort.InferenceSession(f"{onnx_dir}/hubert.onnx", opts, providers=providers)
        self.s_gen = ort.InferenceSession(f"{onnx_dir}/generator.onnx", opts, providers=providers)
        self.s_rmvpe = ort.InferenceSession(f"{onnx_dir}/rmvpe.onnx", opts, providers=providers)
        self._rng = np.random.default_rng(0)   # rnd noise; seeded = reproducible tests

    def warmup(self):
        self.convert_block(np.zeros(CANONICAL_IN, dtype=np.int16),
                           pitch_shift=0, index_rate=0.75,
                           rms_mix_rate=0.75, protect=0.33)

    def _f0(self, audio_f32: np.ndarray, pitch_shift: int, filter_radius: int):
        """audio [PADDED_IN] float32 -> (pitch int64 [GEN_FRAMES], pitchf f32 [GEN_FRAMES])."""
        import torch
        with torch.no_grad():
            x = torch.from_numpy(audio_f32).float().to(self.device)[None, :]
            mel = self.mel(x, center=True)                 # [1, 128, MEL_FRAMES]
        mel_np = mel.cpu().numpy().astype(np.float32)
        pad = MEL_FRAMES_PADDED - mel_np.shape[-1]
        mel_np = np.pad(mel_np, ((0, 0), (0, 0), (0, pad)), mode="constant")
        hidden = self.s_rmvpe.run(None, {"mel": mel_np})[0]
        f0 = decode_f0(hidden)                             # [MEL_FRAMES] Hz
        if filter_radius >= 2:
            from scipy.signal import medfilt
            voiced = f0 > 0
            f0_f = medfilt(f0, kernel_size=filter_radius if filter_radius % 2 else 3)
            f0 = np.where(voiced & (f0_f > 0), f0_f, f0)
        f0 = f0 * (2.0 ** (pitch_shift / 12.0))
        # resample MEL_FRAMES(341) f0 points onto the GEN_FRAMES(340) grid
        f0 = np.interp(np.linspace(0, 1, GEN_FRAMES), np.linspace(0, 1, len(f0)), f0)
        pitchf = f0.astype(np.float32)
        pitch = f0_to_coarse(f0)
        return pitch, pitchf

    def convert_block(self, pcm_int16, pitch_shift, index_rate,
                      rms_mix_rate, protect, filter_radius: int = 3) -> np.ndarray:
        n_in = len(pcm_int16)
        audio, zpad = pad_to_canonical(pcm_int16)

        # ---- Engine 1: HuBERT ----
        feats = self.s_hubert.run(None, {"audio": audio[None, :]})[0]   # [1, 170, 768]
        feats_raw_pre = feats.copy()

        # ---- FAISS index mixing (CPU, cannot be TRT) ----
        if self.index is not None and index_rate > 0:
            npy = feats[0].astype(np.float32)
            score, ix = self.index.search(npy, k=8)
            weight = np.square(1.0 / np.maximum(score, 1e-9))
            weight /= weight.sum(axis=1, keepdims=True)
            mixed = np.sum(self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1)
            feats = (index_rate * mixed + (1 - index_rate) * npy)[None].astype(np.float32)

        # ---- 2x temporal upsample to the generator frame grid ----
        def interp2x(x):    # [1, T, 768] -> [1, 2T, 768], matches F.interpolate scale_factor=2
            return np.repeat(x, 2, axis=1)
        feats = interp2x(feats)
        feats_raw = interp2x(feats_raw_pre)

        # ---- Engine 3: RMVPE F0 ----
        pitch, pitchf = self._f0(audio, pitch_shift, filter_radius)

        # ---- protect (consonant guard) ----
        feats = apply_protect(feats, feats_raw, pitchf, protect)

        # ---- Engine 2: generator ----
        out = self.s_gen.run(None, {
            "phone": feats.astype(np.float32),
            "phone_lengths": np.array([GEN_FRAMES], dtype=np.int64),
            "pitch": pitch[None, :],
            "pitchf": pitchf[None, :],
            "sid": np.array([0], dtype=np.int64),
            "rnd": self._rng.standard_normal((1, 192, GEN_FRAMES)).astype(np.float32),
        })[0].reshape(-1)                                   # [163200] float32

        # ---- trim fixed pad, then the zero-fill's share, then rms mix ----
        out = out[T_PAD_TGT: T_PAD_TGT + OUT_48K]           # 67200 = 3 * CANONICAL_IN
        out = out[3 * zpad:]                                # exactly 3 * n_in remain
        if rms_mix_rate < 1.0:
            src = np.asarray(pcm_int16, dtype=np.float32) / 32768.0
            out = change_rms(src, out, rms_mix_rate)
        out = np.clip(out * 32767.0, -32768, 32767).astype(np.int16)
        assert len(out) == 3 * n_in, f"contract violation: {len(out)} != 3*{n_in}"
        return out
```

- [ ] **Step 2: Run the local tests again (helpers must be unaffected)**

Run: `python -m pytest modal_deploy/test_trt_pipeline.py -v`
Expected: 9 passed (the class only imports GPU libs inside methods, so local import still works).

- [ ] **Step 3: Note the upsample-fidelity check for Task 9**

`np.repeat` (nearest) vs `F.interpolate(mode="nearest", scale_factor=2)` are identical; the vendored pipeline uses nearest — no action, but the Task 9 A/B is the backstop if a fork used linear.

- [ ] **Step 4: Commit**

```bash
git add modal_deploy/trt_pipeline.py
git commit -m "feat(trt): TRTVoicePipeline — 3-engine block conversion with faiss/f0/protect in numpy"
```

---

### Task 7: `compile_trt.py` — engine-cache priming on the L4

**Files:**
- Modify: `modal_deploy/compile_trt.py`

**Interfaces:**
- Consumes: ONNX files (Tasks 2–4), `TRTVoicePipeline` (Task 6).
- Produces: hot TRT engine cache at `/root/rvc-models/trt_cache/` on the volume; end-to-end parity numbers printed (TRT vs the deployed PyTorch path on identical audio).

- [ ] **Step 1: Add the priming + end-to-end parity job**

Append to `modal_deploy/compile_trt.py`:

```python
@app.function(image=trt_image, gpu="L4", timeout=3600,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def build_engines():
    """Build/refresh the TRT engine caches and compare TRT vs PyTorch end-to-end.

    Engine caches are L4(SM89)- and TRT-version-specific: re-run this after any
    GPU tier or onnxruntime/tensorrt bump, or every cold container pays a
    multi-minute in-place build on top of the ~75s cold start.
    """
    import os, sys, time
    import numpy as np

    os.chdir("/root/rvc")
    if "/root/rvc" not in sys.path:
        sys.path.insert(0, "/root/rvc")
    os.environ.setdefault("rmvpe_root", "/root/rvc/assets/rmvpe")

    try:
        from modal_deploy import trt_pipeline as tp
    except ImportError:
        import trt_pipeline as tp
    import faiss, glob, torch
    from infer.lib.rmvpe import MelSpectrogram

    idx_files = sorted(glob.glob("/root/rvc-models/logs/mi-test/added_*.index"))
    index = faiss.read_index(idx_files[-1])
    big_npy = index.reconstruct_n(0, index.ntotal)
    mel = MelSpectrogram(is_half=False, n_mel_channels=128, sampling_rate=16000,
                         win_length=1024, hop_length=160, mel_fmin=30,
                         mel_fmax=8000).to("cuda")

    cache_dir = "/root/rvc-models/trt_cache"
    os.makedirs(cache_dir, exist_ok=True)
    t0 = time.perf_counter()
    pipe = tp.TRTVoicePipeline("/root/rvc-models/onnx", cache_dir, index, big_npy, mel)
    pipe.warmup()   # first pass builds all three engines
    print(f"[TRT] engine build+warmup: {time.perf_counter()-t0:.1f}s")

    # timing: 10 warm blocks
    block = (np.sin(np.linspace(0, 700 * np.pi, tp.CANONICAL_IN)) * 8000).astype(np.int16)
    times = []
    for _ in range(10):
        t = time.perf_counter()
        out = pipe.convert_block(block, 0, 0.75, 0.75, 0.33)
        times.append((time.perf_counter() - t) * 1000)
    print(f"[TRT] warm convert_block ms: min={min(times):.0f} "
          f"median={sorted(times)[5]:.0f} max={max(times):.0f} (block=1400ms audio)")
    assert len(out) == 3 * tp.CANONICAL_IN
    volume.commit()
    print("[TRT] engine cache committed to volume")
```

- [ ] **Step 2: Run it** *(announce GPU cost; engine build takes several minutes)*

Run: `modal run modal_deploy/compile_trt.py::build_engines`
Expected: build time printed once (minutes), then warm `convert_block` timings. Success criterion for continuing: **median warm block ≤ 400 ms** (a 1400 ms audio block at <0.3× real-time). If the ORT log shows most generator nodes on `CUDAExecutionProvider` instead of TRT (Warning 8), set `opts.log_severity_level = 1` temporarily to dump provider assignment and evaluate.

- [ ] **Step 3: Commit**

```bash
git add modal_deploy/compile_trt.py
git commit -m "feat(trt): engine-cache priming job with warm-block timing gate"
```

---

### Task 8: `worker.py` integration (`USE_TRT` runtime path)

**Files:**
- Modify: `modal_deploy/worker.py`

**Interfaces:**
- Consumes: `TRTVoicePipeline` (Task 6), hot engine cache (Task 7).
- Produces: `RVCEngine.convert_block(pcm_int16, pitch, index_rate, rms_mix_rate, protect) -> np.ndarray` used by BOTH `ws_stream` and `convert_file_chunked`; `/health` gains `"engine": "trt"|"pytorch"` and `"trt_cache": "hot"|"cold"|"n/a"`.

- [ ] **Step 1: Mount the new module and switch `fastapi_app`/test functions to `trt_image`**

In the image chain, after `.add_local_python_source("streaming")`, add:

```python
    .add_local_python_source("trt_pipeline")
```

Then change `image=image` to `image=trt_image` on `fastapi_app`, `convert_file`, and `convert_file_chunked`, and move the `trt_image = (...)` block (Task 1) so it sits after `image` and gains the same `add_local_python_source` lines (layers on `image` inherit its mounts — verify in the deploy's mount list, which must show `PythonPackage:trt_pipeline`).

- [ ] **Step 2: Add the TRT branch to `RVCEngine`**

In `RVCEngine.__init__`, add:

```python
        self.trt_pipe = None
        self.engine_kind = "pytorch"
```

At the end of `RVCEngine.startup()` (before `self.ready = True`), add:

```python
        # ---- Optional TRT path (USE_TRT=1). Fail-closed philosophy is preserved:
        # if TRT init fails we fall back to the PyTorch CONVERTED path (never raw),
        # log loudly, and expose the degradation in /health.
        if os.environ.get("USE_TRT", "0") == "1":
            try:
                import glob as _glob
                try:
                    from modal_deploy import trt_pipeline as tp
                except ImportError:
                    import trt_pipeline as tp
                from infer.lib.rmvpe import MelSpectrogram
                import torch as _torch

                index = faiss.read_index(self.index_path)      # hits the lru_cache
                big_npy = index.reconstruct_n(0, index.ntotal)
                mel = MelSpectrogram(
                    is_half=False, n_mel_channels=128, sampling_rate=16000,
                    win_length=1024, hop_length=160, mel_fmin=30, mel_fmax=8000,
                ).to("cuda" if _torch.cuda.is_available() else "cpu")
                cache_dir = "/root/rvc-models/trt_cache"
                self._trt_cache_hot = bool(_glob.glob(f"{cache_dir}/*.engine"))
                print(f"[TRT] engine cache {'HOT' if self._trt_cache_hot else 'COLD — building now'}")
                t0 = time.perf_counter()
                self.trt_pipe = tp.TRTVoicePipeline(
                    "/root/rvc-models/onnx", cache_dir, index, big_npy, mel,
                )
                self.trt_pipe.warmup()
                self.engine_kind = "trt"
                print(f"[TRT] pipeline ready in {time.perf_counter()-t0:.1f}s")
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"[TRT] init FAILED ({e}) — falling back to PyTorch converted path")
                self.trt_pipe = None
```

- [ ] **Step 3: Add `convert_block` on `RVCEngine`**

Add below `run_conversion`:

```python
    def convert_block(
        self,
        pcm_int16,                      # np.int16 array, 16 kHz, <= 22400 samples
        pitch: int = 0,
        index_rate: float = 0.75,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
    ) -> bytes:
        """Single streaming-block conversion. TRT path when loaded, else the
        existing PyTorch run_conversion via WAV bytes. Returns 48 kHz int16 bytes,
        ~3x the input duration either way."""
        if self.trt_pipe is not None:
            out = self.trt_pipe.convert_block(
                pcm_int16, pitch, index_rate, rms_mix_rate, protect,
            )
            return out.tobytes()
        try:
            from modal_deploy import streaming as _st
        except ImportError:
            import streaming as _st
        return self.run_conversion(
            _st.pcm16_to_wav_bytes(pcm_int16), pitch, index_rate, 3,
            rms_mix_rate, protect,
        )
```

- [ ] **Step 4: Route `ws_stream` through `convert_block`**

In `ws_stream`, replace the inference call block:

```python
                        wav_bytes = st.pcm16_to_wav_bytes(infer_input)
                        try:
                            t0 = time.perf_counter()
                            async with _gpu_lock:  # single-tenant GPU
                                out_bytes = await asyncio.to_thread(
                                    engine.run_conversion,
                                    wav_bytes,
                                    session_pitch,
                                    index_rate,
                                    3,             # filter_radius default
                                    rms_mix_rate,
                                    protect,
                                )
                            infer_ms = (time.perf_counter() - t0) * 1000.0
```

with:

```python
                        try:
                            t0 = time.perf_counter()
                            async with _gpu_lock:  # single-tenant GPU
                                out_bytes = await asyncio.to_thread(
                                    engine.convert_block,
                                    infer_input,
                                    session_pitch,
                                    index_rate,
                                    rms_mix_rate,
                                    protect,
                                )
                            infer_ms = (time.perf_counter() - t0) * 1000.0
```

(The auto-detect probe above it still builds `probe_wav` itself — leave that code untouched.)

- [ ] **Step 5: Route `convert_file_chunked` the same way**

In `convert_file_chunked`, replace:

```python
            wav_bytes = st.pcm16_to_wav_bytes(infer_input)
            out_bytes = test_engine.run_conversion(wav_bytes, pitch=pitch)
```

with:

```python
            out_bytes = test_engine.convert_block(infer_input, pitch=pitch)
```

and change its decorator `gpu="T4"` to `gpu="L4"` (both offline test functions must match the deployed tier now that engines are SM89-specific; update the stale `# matches convert_file's GPU...` comment accordingly). Do the same `gpu="L4"` change on `convert_file`.

- [ ] **Step 6: Extend `/health`**

In the `health()` handler's returned dict, add:

```python
            "engine": engine.engine_kind,
            "trt_cache": ("hot" if getattr(engine, "_trt_cache_hot", False)
                          else "cold") if engine.engine_kind == "trt" else "n/a",
```

- [ ] **Step 7: Set `USE_TRT` on the app function**

`USE_TRT` gates rollout without code edits. Add to the image env (in `trt_image`): nothing — instead pass per-deploy: add to `fastapi_app`'s decorator NO env; instead read it from a Modal Secret or set `.env({"USE_TRT": "1"})` on `trt_image` **only after Task 9 passes offline**. For Task 9's offline runs, set it inside `convert_file_chunked` via a new function parameter:

```python
def convert_file_chunked(audio_bytes: bytes, pitch: int = -1, use_trt: int = 1) -> bytes:
    import numpy as np
    if use_trt:
        os.environ["USE_TRT"] = "1"
    test_engine = RVCEngine()
    ...
```

and thread it through `main_chunked`:

```python
@app.local_entrypoint()
def main_chunked(pitch: int = -1, use_trt: int = 1):
    ...
    output_pcm = convert_file_chunked.remote(audio_bytes, pitch=pitch, use_trt=use_trt)
```

- [ ] **Step 8: Run the local test suite (regressions only — no GPU here)**

Run: `python -m backend.test_pipeline` and `python -m pytest modal_deploy/test_trt_pipeline.py modal_deploy/test_streaming.py modal_deploy/test_faiss_index_cache.py -v`
Expected: all pass (nothing in this task touches those code paths' contracts).

- [ ] **Step 9: Commit**

```bash
git add modal_deploy/worker.py
git commit -m "feat(trt): USE_TRT runtime path in worker — convert_block routing, health fields, L4 test fns"
```

---

### Task 9: Offline benchmark & quality verification (the gate)

**Files:** none created (outputs are WAVs + a results table pasted into the PR/commit message).

- [ ] **Step 1: Baseline (PyTorch + pm, current production behavior)** *(announce GPU cost)*

Run: `modal run modal_deploy/worker.py::main_chunked --pitch 12 --use-trt 0`
(then rename the output) `mv D:\Kiera\test11_chunked.wav D:\Kiera\test11_chunked_baseline.wav`
Record from the logs: per-block `vc_single`/`total run_conversion` ms.

- [ ] **Step 2: TRT + RMVPE run**

Run: `modal run modal_deploy/worker.py::main_chunked --pitch 12 --use-trt 1`
`mv D:\Kiera\test11_chunked.wav D:\Kiera\test11_chunked_trt.wav`
Record per-block `convert_block` ms (add a `print` if not already visible via `[Timing]`).

- [ ] **Step 3: Evaluate against the gates**

| Gate | Threshold | Source |
|---|---|---|
| Warm per-block latency | median ≤ 400 ms, p95 ≤ 600 ms per 1000 ms block | Task 7 + Step 2 timings |
| Output length contract | TRT output byte length == baseline's ± one crossfade | file sizes |
| Voice identity | TRT+RMVPE ≥ baseline vs `test11.wav` reference on a human listen (user judges) | listen test |
| Edge artifacts (pad change, Warning 3) | no audible block-rate (1 Hz) artifacts in `test11_chunked_trt.wav` | listen test |
| RMVPE F0 sanity | no octave jumps / chipmunk segments vs baseline | listen test |

**The user performs the listen comparisons** (`test11.wav` = single-pass reference, `_baseline` vs `_trt`). If the pad-edge gate fails: raise `TRT_T_PAD` in BOTH `export_onnx.py` and `trt_pipeline.py`, re-run Tasks 2–4 exports and Task 7, repeat.

- [ ] **Step 4: Record results**

Append the results table + timings to `.agents/context/subsystem-notes.md` under the Modal worker section, and commit:

```bash
git add .agents/context/subsystem-notes.md
git commit -m "docs: TRT vs PyTorch benchmark results (task 9 gate)"
```

---

### Task 10: Live rollout — **[USER-RUN]**

- [ ] **Step 1: Enable TRT for the deployed app**

Add `.env({"USE_TRT": "1"})` to the end of the `trt_image` chain in `worker.py`; commit:

```bash
git add modal_deploy/worker.py
git commit -m "feat(trt): enable USE_TRT for deployed worker"
```

- [ ] **Step 2 [USER-RUN]: Deploy** — get explicit user go-ahead, then the user runs:

`modal deploy modal_deploy/worker.py`
Then verify (committed ≠ deployed — always check live state):
`GET <worker-url>/health` → expect `"engine": "trt"`, `"trt_cache": "hot"`, `"cuda_device": "NVIDIA L4"`.
If `trt_cache` is `"cold"`, run `modal run modal_deploy/compile_trt.py::build_engines` first and redeploy-check.

- [ ] **Step 3 [USER-RUN]: Live test call**

User places a test call (outbound flow). Watch `modal app logs rvc-worker` for per-block stats; the `/ws` `"stats"` messages' `infer_ms` should match Task 9 (median ≤ 400 ms). Confirm the fail-closed behaviors are intact: kill nothing mid-call, just observe a full call. Do **not** push to `main` during the call (Render auto-deploy).

- [ ] **Step 4: Update the second brain**

Record in `.agents/decisions/log.md`: TRT migration shipped, RMVPE re-enabled (pm retired from the live path), engine-cache invalidation rule (GPU/TRT version changes), and the phased buffer decision status.

```bash
git add .agents/decisions/log.md
git commit -m "docs: record TRT migration + rmvpe re-enable decision"
```

---

### Task 11: Playout buffer Phase 1 (1.25 s) + Phase 2 gate

**Files:**
- Modify: `backend/pipeline.py:41`

**Do not start until Task 10 Step 3 has confirmed live `infer_ms` numbers.**

- [ ] **Step 1: Reduce the target**

In `backend/pipeline.py`, change:

```python
    _PLAYOUT_BUFFER_TARGET_BYTES = int(48000 * 2 * 3.0)
```

to:

```python
    # Phase 1 of the TRT latency plan (2026-07-05): 1.25s is the floor for
    # BLOCK_MS=1000 — converted audio arrives in ~1s bursts, so the cushion must
    # exceed one block interval plus jitter. 0.25s (Phase 2) additionally requires
    # shrinking BLOCK_MS and is gated on live TRT p95 <= 0.4x BLOCK_MS.
    _PLAYOUT_BUFFER_TARGET_BYTES = int(48000 * 2 * 1.25)
```

Leave `_PLAYOUT_BUFFER_MAX_BYTES` at 5.0 s (it is an overflow cap, not a latency term).

- [ ] **Step 2: Run the pipeline tests**

Run: `python -m backend.test_pipeline`
Expected: pass (the buffer tests assert behavior against the constants, not hardcoded 3.0 s — if one hardcodes 3.0 s, update the test to reference `_PLAYOUT_BUFFER_TARGET_BYTES`).

- [ ] **Step 3 [USER-RUN]: Ship + live-verify**

Pushing to `main` auto-deploys Render — get user go-ahead, push, then a live test call: no audible gaps at 1.25 s cushion. If gaps occur, revert to 3.0 s (single-constant revert) and record the observed jitter in subsystem-notes.

- [ ] **Step 4: Commit + Phase 2 decision record**

```bash
git add backend/pipeline.py
git commit -m "feat: reduce playout buffer target to 1.25s (TRT phase 1)"
```

Phase 2 (**separate future plan, not this one**): if 7+ days of live `infer_ms` hold p95 ≤ 400 ms, propose `BLOCK_MS` 1000→320 + buffer 0.25 s, re-running the whole Task 9 gate at the new geometry (new canonical shapes → full re-export). Record the gate in `.agents/projects/active-backlog.md`.

---

## 3. Verification Plan (summary)

1. **Per-model parity** (Tasks 2–4): every export hard-fails below cosine 0.999 vs PyTorch on the same inputs — a green `modal run` is the proof.
2. **Local unit tests** (Task 5): `pytest modal_deploy/test_trt_pipeline.py` covers the NumPy ports (pad geometry incl. short first block, f0 coarse mapping bounds, RMVPE cents decode incl. unvoiced threshold, rms identity, protect on/off semantics).
3. **Warm-latency gate** (Task 7): `build_engines` prints warm `convert_block` timings; median ≤ 400 ms per 1400 ms input is the go/no-go for integration.
4. **End-to-end A/B** (Task 9): `main_chunked --use-trt 0|1` produces `test11_chunked_baseline.wav` vs `test11_chunked_trt.wav` against the `test11.wav` single-pass reference — identical DSP path (BlockAccumulator + SOLA) as live, isolating engine quality. Human listen gates identity, RMVPE F0 sanity, and pad-edge artifacts.
5. **Live** (Task 10, user-run): `/health` engine fields, `infer_ms` in `/ws` stats during a real call, fail-closed behavior unchanged.
6. **Buffer** (Task 11): live call at 1.25 s cushion with zero audible gaps; single-constant rollback path.
