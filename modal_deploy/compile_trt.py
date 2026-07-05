"""TRT engine-cache management for the Keira RVC worker.

Task 1: environment/asset probe — verifies ORT+TensorRT EP is available on L4.
Task 7: build_engines — primes the engine cache on the volume (run before live deploy).

Run the probe:
    modal run modal_deploy/compile_trt.py

Run the engine builder (announce GPU cost — engine compile takes several minutes):
    modal run modal_deploy/compile_trt.py::build_engines

Context:
- Engine caches are SM89 (L4) + TRT-version specific.
- Re-run build_engines after any GPU tier change or onnxruntime/tensorrt upgrade.
- A cold cache means the first container boot pays a multi-minute engine build on top
  of the ~75s cold start. Pre-prime before live calls via this script.
"""
import modal

try:
    from modal_deploy.worker import trt_image, volume
except ImportError:  # running with modal_deploy/ on sys.path
    from worker import trt_image, volume

app = modal.App("rvc-trt-tools")


# ---------------------------------------------------------------------------
# Task 1: Toolchain & asset probe (hard gate — nothing else runs until this passes)
# ---------------------------------------------------------------------------

@app.function(
    image=trt_image,
    gpu="L4",
    timeout=600,
    volumes={"/root/rvc-models": volume},
    region="ap-southeast",
)
def probe() -> dict:
    """Verify the TRT runtime environment on the L4.

    Checks:
    - TensorrtExecutionProvider is in ORT's available providers
    - LD_LIBRARY_PATH points at TRT libs (env set in trt_image)
    - hubert_base.pt and rmvpe.pt are reachable from the container
    - At least one FAISS index file exists

    Hard-asserts on TRT EP presence. If it fails, iterate on the pip pins
    in the trt_image definition in worker.py (see implementation_plan.md Warning 4).
    """
    import os
    import glob
    import onnxruntime as ort
    import torch

    report = {
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "cuda_device": (
            torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
        ),
        "ort": ort.__version__,
        "ort_providers": ort.get_available_providers(),
        "ld_library_path": os.environ.get("LD_LIBRARY_PATH", "<not set>"),
        "hubert_pt_exists": os.path.exists("/root/rvc/assets/hubert/hubert_base.pt"),
        "rmvpe_pt_exists": os.path.exists("/root/rvc/assets/rmvpe/rmvpe.pt"),
        "rmvpe_pt_on_volume": os.path.exists("/root/rvc-models/assets/rmvpe/rmvpe.pt"),
        "index_files": glob.glob("/root/rvc-models/logs/mi-test/*.index"),
        "onnx_dir_exists": os.path.exists("/root/rvc-models/onnx"),
        "trt_cache_exists": os.path.exists("/root/rvc-models/trt_cache"),
    }

    for k, v in report.items():
        print(f"[Probe] {k}: {v}")

    assert "TensorrtExecutionProvider" in report["ort_providers"], (
        "TRT EP missing — check LD_LIBRARY_PATH / tensorrt-cu12 install. "
        "See implementation_plan.md Warning 4 for candidate version pins."
    )

    if not report["rmvpe_pt_exists"] and not report["rmvpe_pt_on_volume"]:
        print(
            "\n[Probe] WARNING: rmvpe.pt not found! "
            "Upload before Task 4:\n"
            "  modal volume put rvc-models <local-path-to-rmvpe.pt> assets/rmvpe/rmvpe.pt"
        )

    print("\n[Probe] PASSED — TRT EP confirmed on L4")
    return report


# ---------------------------------------------------------------------------
# Task 7: Engine-cache priming job
# ---------------------------------------------------------------------------

@app.function(
    image=trt_image,
    gpu="L4",
    timeout=3600,
    volumes={"/root/rvc-models": volume},
    region="ap-southeast",
)
def build_engines():
    """Build / refresh TRT engine caches and run warm-block timing benchmarks.

    First call: builds all three engines (several minutes for the TRT compilation).
    Subsequent calls: loads cached engines (~seconds), still runs timing.

    Success criterion: median warm convert_block <= 400 ms per 1400 ms audio block.
    If most generator nodes show CUDAExecutionProvider instead of TensorRT in ORT logs,
    set opts.log_severity_level = 1 in TRTVoicePipeline.__init__ to dump provider
    assignment and evaluate (see implementation_plan.md Warning 8).
    """
    import os
    import sys
    import time
    import glob
    import numpy as np

    os.chdir("/root/rvc")
    if "/root/rvc" not in sys.path:
        sys.path.insert(0, "/root/rvc")
    os.environ.setdefault("rmvpe_root", "/root/rvc/assets/rmvpe")

    try:
        from modal_deploy import trt_pipeline as tp
    except ImportError:
        import trt_pipeline as tp

    import faiss
    import torch
    from infer.lib.rmvpe import MelSpectrogram

    # Load FAISS index (same path as production startup)
    idx_files = sorted(glob.glob("/root/rvc-models/logs/mi-test/added_*.index"))
    if not idx_files:
        idx_files = sorted(glob.glob("/root/rvc-models/logs/mi-test/*.index"))
    assert idx_files, "No FAISS index found — check the rvc-models volume."
    index = faiss.read_index(idx_files[-1])
    big_npy = index.reconstruct_n(0, index.ntotal)
    print(f"[TRT] FAISS index loaded: {idx_files[-1]} ({index.ntotal} vectors)")

    # RMVPE mel frontend (stays in PyTorch — torch.stft can't be exported reliably)
    mel = MelSpectrogram(
        is_half=False, n_mel_channels=128, sampling_rate=16000,
        win_length=1024, hop_length=160, mel_fmin=30, mel_fmax=8000,
    ).to("cuda" if torch.cuda.is_available() else "cpu")

    cache_dir = "/root/rvc-models/trt_cache"
    os.makedirs(cache_dir, exist_ok=True)

    # Check if engines are already cached
    existing = glob.glob(f"{cache_dir}/*.engine")
    print(f"[TRT] Engine cache: {'HOT' if existing else 'COLD — building now'} "
          f"({len(existing)} existing .engine files)")

    print("[TRT] Creating TRTVoicePipeline sessions (builds or loads engines)...")
    t0 = time.perf_counter()
    pipe = tp.TRTVoicePipeline(
        "/root/rvc-models/onnx", cache_dir, index, big_npy, mel,
    )
    pipe.warmup()   # first pass builds all three engines (slow) or loads cache (fast)
    print(f"[TRT] Engine build+warmup: {time.perf_counter() - t0:.1f}s")

    # Timing benchmark: 10 warm blocks
    block = (np.sin(np.linspace(0, 700 * np.pi, tp.CANONICAL_IN)) * 8000).astype(np.int16)
    print("[TRT] Running 10 warm convert_block passes for timing...")
    times = []
    for i in range(10):
        t = time.perf_counter()
        out = pipe.convert_block(block, pitch_shift=0, index_rate=0.75,
                                 rms_mix_rate=0.75, protect=0.33)
        elapsed_ms = (time.perf_counter() - t) * 1000.0
        times.append(elapsed_ms)
        print(f"[TRT]   block {i+1}/10: {elapsed_ms:.0f} ms")

    times.sort()
    print(f"\n[TRT] Warm convert_block timings (1400ms audio → 48kHz output):")
    print(f"[TRT]   min={times[0]:.0f}ms  median={times[5]:.0f}ms  p95={times[9]:.0f}ms  max={times[-1]:.0f}ms")

    # Output length contract check
    assert len(out) == 3 * tp.CANONICAL_IN, (
        f"Output contract violated: {len(out)} != {3 * tp.CANONICAL_IN}"
    )

    # Gate check
    median_ms = times[5]
    if median_ms <= 400:
        print(f"\n[TRT] ✅ GATE PASSED: median {median_ms:.0f}ms <= 400ms")
    else:
        print(f"\n[TRT] ❌ GATE FAILED: median {median_ms:.0f}ms > 400ms — "
              "check GPU provider assignment; see implementation_plan.md Warning 8")

    volume.commit()
    print("[TRT] Engine cache committed to volume.")


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

@app.local_entrypoint()
def main():
    """Default: run the toolchain probe. Run with: modal run modal_deploy/compile_trt.py"""
    print("[Probe] Running TRT environment probe on L4...")
    probe.remote()
