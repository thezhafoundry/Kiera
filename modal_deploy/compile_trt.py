"""TRT engine-cache management for the Keira RVC worker.

Task 1: environment/asset probe -- verifies ORT sees TensorrtExecutionProvider on L4.
Task 7: build_engines -- primes the TRT engine caches on the volume.

Run the probe:
  modal run modal_deploy/compile_trt.py::probe

Run the engine builder (after ONNX exports complete):
  modal run modal_deploy/compile_trt.py::build_engines
"""
import modal

try:
    from modal_deploy.modal_defs import trt_image, volume
except ImportError:   # inside container
    from modal_defs import trt_image, volume

app = modal.App("rvc-trt-tools")


@app.function(image=trt_image, gpu="L4", timeout=600,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def probe() -> dict:
    """Task 1: verify TRT EP is available and locate library files."""
    import os, sys, glob
    import onnxruntime as ort
    import torch

    print("=== Environment Variables ===")
    for k in sorted(os.environ.keys()):
        if "PATH" in k or "CUDA" in k or "TRT" in k:
            print(f"  {k}: {os.environ[k]}")

    print("=== Searching for libcublasLt.so* and libcudnn.so* ===")
    paths_to_search = ["/usr/local", "/usr/lib", "/usr/share", "/root"]
    for base in paths_to_search:
        pattern1 = os.path.join(base, "**/libcublasLt.so*")
        pattern2 = os.path.join(base, "**/libcudnn.so*")
        files1 = glob.glob(pattern1, recursive=True)
        files2 = glob.glob(pattern2, recursive=True)
        for f in files1 + files2:
            print(f"  Found: {f} (size={os.path.getsize(f)} bytes)")

    report = {
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "ort": ort.__version__,
        "ort_providers": ort.get_available_providers(),
        "hubert_pt_exists": os.path.exists("/root/rvc/assets/hubert/hubert_base.pt"),
        "contentvec_onnx_exists": os.path.exists("/root/rvc-models/onnx/vec-768-layer-12.onnx"),
        "rmvpe_pt_exists": os.path.exists("/root/rvc/assets/rmvpe/rmvpe.pt"),
        "rmvpe_pt_on_volume": os.path.exists("/root/rvc-models/assets/rmvpe/rmvpe.pt"),
        "generator_onnx_exists": os.path.exists("/root/rvc-models/onnx/generator.onnx"),
        "index_files": glob.glob("/root/rvc-models/logs/mi-test/*.index"),
    }
    for k, v in report.items():
        print(f"[Probe] {k}: {v}")

    assert "TensorrtExecutionProvider" in report["ort_providers"], \
        "TRT EP missing -- check LD_LIBRARY_PATH / tensorrt-cu12 install"

    if not report["rmvpe_pt_exists"] and not report["rmvpe_pt_on_volume"]:
        print("[Probe] WARNING: rmvpe.pt not found in either location!")
        print("[Probe] Upload it with: modal volume put rvc-models <local-path> assets/rmvpe/rmvpe.pt")

    return report


@app.function(image=trt_image, gpu="L4", timeout=3600,
              volumes={"/root/rvc-models": volume}, region="ap-southeast")
def build_engines():
    """Task 7: Build/refresh the TRT engine caches and time warm-block throughput.

    Engine caches are L4(SM89)- and TRT-version-specific: re-run this after any
    GPU tier or onnxruntime/tensorrt bump, or every cold container pays a
    multi-minute in-place build on top of the cold start.
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
    if not idx_files:
        idx_files = sorted(glob.glob("/root/rvc-models/logs/mi-test/*.index"))
    if not idx_files:
        raise FileNotFoundError("No FAISS index found in /root/rvc-models/logs/mi-test/")

    index = faiss.read_index(idx_files[-1])
    big_npy = index.reconstruct_n(0, index.ntotal)
    mel = MelSpectrogram(is_half=False, n_mel_channels=128, sampling_rate=16000,
                         win_length=1024, hop_length=160, mel_fmin=30,
                         mel_fmax=8000).to("cuda" if torch.cuda.is_available() else "cpu")

    cache_dir = "/root/rvc-models/trt_cache"
    os.makedirs(cache_dir, exist_ok=True)

    print(f"[TRT] Building TRT engine caches at {cache_dir}...")
    t0 = time.perf_counter()
    pipe = tp.TRTVoicePipeline("/root/rvc-models/onnx", cache_dir, index, big_npy, mel)
    pipe.warmup()
    print(f"[TRT] engine build+warmup: {time.perf_counter()-t0:.1f}s")

    block = (np.sin(np.linspace(0, 700 * np.pi, tp.CANONICAL_IN)) * 8000).astype(np.int16)
    times = []
    for i in range(10):
        t = time.perf_counter()
        out = pipe.convert_block(block, pitch_shift=0, index_rate=0.75,
                                 rms_mix_rate=0.75, protect=0.33)
        elapsed_ms = (time.perf_counter() - t) * 1000
        times.append(elapsed_ms)
        print(f"[TRT] block {i+1}/10: {elapsed_ms:.0f}ms")

    times_sorted = sorted(times)
    print(f"[TRT] warm convert_block ms: min={min(times):.0f} "
          f"median={times_sorted[5]:.0f} p95={times_sorted[9]:.0f} max={max(times):.0f} "
          f"(block=1400ms audio, target median<=400ms)")
    assert len(out) == 3 * tp.CANONICAL_IN, f"output length contract violated: {len(out)}"

    volume.commit()
    print("[TRT] engine cache committed to volume")

    median_ms = times_sorted[5]
    if median_ms <= 400:
        print(f"[TRT] GATE PASSED: median {median_ms:.0f}ms <= 400ms -- safe to proceed to Task 9")
    else:
        raise AssertionError(
            f"[TRT] GATE FAILED: median {median_ms:.0f}ms > 400ms. "
            "Do NOT proceed to live rollout. Review ORT provider assignment "
            "(set opts.log_severity_level=1) to check if generator nodes are on TRT."
        )



@app.local_entrypoint()
def main():
    probe.remote()
