import modal
from pathlib import Path

# Local dataset path (on YOUR machine)
LOCAL_DATASET = Path("D:/Voice Clon/Keira/dataset/processed")

# Modal volume
volume = modal.Volume.from_name(
    "rvc-models",
    create_if_missing=False,
)

def main():
    files = list(LOCAL_DATASET.glob("*.wav"))

    if not files:
        print(f"ERROR: No .wav files found in {LOCAL_DATASET}")
        return

    print(f"Found {len(files)} files to upload:")
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  {f.name} ({size_mb:.1f} MB)")

    print()

    for file in files:
        remote_path = f"datasets/mi-test/{file.name}"
        print(f"Uploading {file.name}...", end=" ", flush=True)
        with open(file, "rb") as f:
            # write_file is the correct API for Modal volumes
            volume.write_file(remote_path, f.read())
        print("✓")

    volume.commit()
    print()
    print("Dataset upload complete!")
    print()

    # Verify
    print("Verifying files in volume:")
    for entry in volume.listdir("datasets/mi-test"):
        print(f"  {entry.path}")

if __name__ == "__main__":
    main()