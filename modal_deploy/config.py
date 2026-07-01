import modal

image = (
    modal.Image.debian_slim()
    .pip_install(
        "torch",
        "torchaudio",
        "numpy",
        "scipy",
        "soundfile",
        "librosa",
        "fastapi",
        "uvicorn"
    )
)

volume = modal.Volume.from_name("rvc-models")