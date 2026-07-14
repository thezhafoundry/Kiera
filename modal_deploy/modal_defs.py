"""Shared Modal volume and image definitions for the Kiera RVC worker.

This module is the single source of truth for `volume`, `image`, and `trt_image`.
It mounts itself (.add_local_python_source("modal_defs")) into both final images so
that any container — worker, compile_trt, export_onnx — can import these objects
without needing worker.py on the container path.

Import pattern (all files that need these):
    try:
        from modal_deploy.modal_defs import trt_image, volume
    except ImportError:           # inside container: modal_deploy package not present
        from modal_defs import trt_image, volume
"""
import modal

# ---- Persistent volume ----
volume = modal.Volume.from_name("rvc-models", create_if_missing=False)

# ---- Shared ignore list for the RVC source tree ----
_RVC_IGNORE = [
    "venv311", "dataset", "logs", "TEMP",
    "__pycache__", ".git", ".github",
]

# ---- Build-only base images (NO add_local_* calls) ----
# Modal forbids any build step (pip_install, run_commands, env) after add_local_*.
# Two clean build bases contain only installable layers; local files are attached
# at the very end of each final image below.

_build_base = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git", "ffmpeg")
    .run_commands("python -m pip install --upgrade 'pip<24.1'")
    .pip_install_from_requirements("modal_deploy/requirements.txt")
)

_trt_build_base = (
    _build_base
    .pip_install(
        "onnx==1.16.1",
        "onnxruntime-gpu==1.19.0",
        "tensorrt-cu12==10.0.1",
    )
    .env({
        "LD_LIBRARY_PATH": "/usr/local/lib/python3.10/site-packages/tensorrt_libs:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/cuda_runtime/lib:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/cublas/lib:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/cudnn/lib:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/cufft/lib:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/curand/lib:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/cusolver/lib:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/cusparse/lib:"
                           "/usr/local/lib/python3.10/site-packages/nvidia/nvjitlink/lib"
     })
)

# ---- Final images: build base + local files LAST ----
# add_local_python_source("modal_defs") mounts THIS file so containers can
# do `from modal_defs import trt_image, volume` without needing worker.py.

image = (
    _build_base
    .add_local_dir("RVC", remote_path="/root/rvc", ignore=_RVC_IGNORE)
    .add_local_python_source("streaming")
    .add_local_python_source("pitch_lock")
    .add_local_python_source("modal_defs")
)

trt_image = (
    _trt_build_base
    .add_local_dir("RVC", remote_path="/root/rvc", ignore=_RVC_IGNORE)
    .add_local_python_source("streaming")
    .add_local_python_source("trt_pipeline")
    .add_local_python_source("pitch_lock")
    .add_local_python_source("modal_defs")
)
