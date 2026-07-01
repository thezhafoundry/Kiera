import os
import sys
import json
import subprocess
import modal
import shutil
from scipy.io import wavfile
import numpy as np
import faiss

app = modal.App("voice-conversion")

# Persistent Modal Volume
volume = modal.Volume.from_name(
    "rvc-models",
    create_if_missing=False,
)

# Image
image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install(
        "git",
        "ffmpeg",
    )
    .run_commands(
        "python -m pip install --upgrade 'pip<24.1'"
    )
    .pip_install_from_requirements(
        "modal_deploy/requirements.txt"
    )
    .pip_install(
    "faiss-cpu",
    )
    .add_local_dir(
        "Retrieval-based-Voice-Conversion-WebUI",
        remote_path="/root/rvc",
        ignore=[
            "venv311",
            "dataset",
            "logs",
            "TEMP",
            "__pycache__",
            ".git",
            ".github",
        ],
    )
)


@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={
        "/root/rvc-models": volume,
    },
)
def preprocess():

    # Reload volume so we can see data uploaded by upload_dataset.py
    volume.reload()

    os.chdir("/root/rvc")

    # Dataset location
    dataset_path = "/root/rvc-models/datasets/mi-test"

    # Output directory
    experiment_dir = "/root/rvc-models/logs/mi-test"

    os.makedirs(experiment_dir, exist_ok=True)

    # Debug: confirm dataset files are visible
    print("=" * 80)
    print("Dataset contents:")
    if os.path.exists(dataset_path):
        files = os.listdir(dataset_path)
        print(f"  Found {len(files)} files:")
        for f in files:
            print(f"    {f}")
    else:
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")
    print("=" * 80)

    command = [
        sys.executable,
        "infer/modules/train/preprocess.py",
        dataset_path,
        "48000",
        str(os.cpu_count()),
        experiment_dir,
        "False",
        "3.7",
    ]

    print("Starting RVC Preprocessing")
    print("Running command:")
    print(" ".join(command))
    print()

    subprocess.run(command, check=True)

    # Commit results so next function can see them
    volume.commit()

    print()
    print("=" * 80)
    print("Generated Files")
    print("=" * 80)

    for root, dirs, files in os.walk(experiment_dir):
        print(root)
        for f in files:
            print("   ", f)

    return "Preprocessing Complete"


@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={
        "/root/rvc-models": volume,
    },
)
def extract_f0():

    # Reload volume to see preprocessed files from preprocess()
    volume.reload()

    os.chdir("/root/rvc")

    experiment_dir = "/root/rvc-models/logs/mi-test"

    command = [
        sys.executable,
        "infer/modules/train/extract/extract_f0_print.py",
        experiment_dir,
        str(os.cpu_count()),
        "rmvpe",
    ]

    print("=" * 80)
    print("Starting RMVPE F0 Extraction")
    print("=" * 80)

    print("Running command:")
    print(" ".join(command))
    print()

    input_dir = os.path.join(experiment_dir, "1_16k_wavs")

    print("Input directory:", input_dir)
    print("Exists:", os.path.exists(input_dir))

    files = os.listdir(input_dir)
    print("Number of files:", len(files))

    for f in files[:10]:
        print(f)

    subprocess.run(command, check=True)

    # Commit so next function sees f0 files
    volume.commit()

    print()
    print("=" * 80)
    print("Generated Files")
    print("=" * 80)

    for folder in ["2a_f0", "2b-f0nsf"]:
        path = os.path.join(experiment_dir, folder)
        print(path)
        if os.path.exists(path):
            print(f"Files: {len(os.listdir(path))}")

    return "F0 Extraction Complete"


@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={"/root/rvc-models": volume},
)
def extract_features():

    # Reload volume to see f0 files from extract_f0()
    volume.reload()

    os.chdir("/root/rvc")

    experiment_dir = "/root/rvc-models/logs/mi-test"

    command = [
        sys.executable,
        "infer/modules/train/extract_feature_print.py",
        "cpu",  # Use GPU for HuBERT feature extraction (10x faster than cpu)
        "1",
        "0",
        experiment_dir,
        "v2",
        "False",
    ]

    print("=" * 80)
    print("Starting HuBERT Feature Extraction")
    print("=" * 80)

    print("Running command:")
    print(" ".join(command))
    print()

    subprocess.run(command, check=True)

    # Commit so next function sees feature files
    volume.commit()

    feature_dir = os.path.join(experiment_dir, "3_feature768")

    print()
    print("=" * 80)
    print("Generated Files")
    print("=" * 80)

    print(feature_dir)
    if os.path.exists(feature_dir):
        print(f"Files: {len(os.listdir(feature_dir))}")
    print()
    print("Checking experiment folders")

    for folder in [
        "0_gt_wavs",
        "2a_f0",
        "2b-f0nsf",
        "3_feature768",
    ]:
        path = os.path.join(experiment_dir, folder)
        print(folder, os.path.exists(path))

    return "Feature Extraction Complete"

@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={"/root/rvc-models": volume},
)
def generate_filelist():

    # Reload volume to see feature files
    volume.reload()

    os.chdir("/root/rvc")

    experiment_dir = "/root/rvc-models/logs/mi-test"

    gt_dir = os.path.join(experiment_dir, "0_gt_wavs")
    feature_dir = os.path.join(experiment_dir, "3_feature768")
    f0_dir = os.path.join(experiment_dir, "2a_f0")
    f0nsf_dir = os.path.join(experiment_dir, "2b-f0nsf")

    filelist_path = os.path.join(experiment_dir, "filelist.txt")

    lines = []

    for file in sorted(os.listdir(gt_dir)):
        if not file.endswith(".wav"):
            continue

        name = os.path.splitext(file)[0]

        line = "|".join([
            f"0_gt_wavs/{file}",
            f"3_feature768/{name}.npy",
            f"2a_f0/{name}.wav.npy",
            f"2b-f0nsf/{name}.wav.npy",
            "0",
        ])

        lines.append(line)

    with open(filelist_path, "w") as f:
        f.write("\n".join(lines))

    volume.commit()

    print("=" * 80)
    print("Generated filelist.txt")
    print("=" * 80)
    print(f"Entries: {len(lines)}")
    print()
    print("First 5 entries:")
    print("-" * 80)

    with open(filelist_path, "r") as f:
        for i in range(5):
            print(f.readline().strip())
    return "filelist.txt created"

@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={"/root/rvc-models": volume},
)
def generate_config():

    # Reload volume to see filelist
    volume.reload()

    os.chdir("/root/rvc")

    experiment_dir = "/root/rvc-models/logs/mi-test"
    os.makedirs(experiment_dir, exist_ok=True)
    import sys

    repo_root = "/root/rvc"

    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    print(sys.path)

    from configs.config import Config
    config = Config()

    config_path = os.path.join(experiment_dir, "config.json")

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(
            config.json_config["v2/48k.json"],
            f,
            ensure_ascii=False,
            indent=4,
            sort_keys=True,
        )
        f.write("\n")

    volume.commit()

    print("=" * 80)
    print("config.json generated")
    print("=" * 80)
    print(config_path)

    return "config.json created"

def train_index(exp_name: str, version: str = "v2"):

    exp_dir = f"/root/rvc-models/logs/{exp_name}"

    feature_dim = 768 if version == "v2" else 256

    feature_dir = os.path.join(
        exp_dir,
        f"3_feature{feature_dim}",
    )

    if not os.path.exists(feature_dir):
        raise FileNotFoundError(feature_dir)

    feature_files = sorted(
        [
            f
            for f in os.listdir(feature_dir)
            if f.endswith(".npy")
        ]
    )

    if len(feature_files) == 0:
        raise RuntimeError("No feature files found.")

    print("=" * 80)
    print("Loading feature vectors")
    print("=" * 80)

    features = []

    for file in feature_files:
        features.append(
            np.load(
                os.path.join(feature_dir, file)
            )
        )

    big_npy = np.concatenate(features, axis=0)

    big_npy = big_npy.astype(np.float32)

    np.random.shuffle(big_npy)

    print("Feature shape:", big_npy.shape)
    np.save(
        os.path.join(exp_dir, "total_fea.npy"),
        big_npy,
    )

    n_ivf = min(
        int(16 * np.sqrt(big_npy.shape[0])),
        big_npy.shape[0] // 39,
    )

    print("IVF =", n_ivf)

    index = faiss.index_factory(
        feature_dim,
        f"IVF{n_ivf},Flat",
    )

    index_ivf = faiss.extract_index_ivf(index)
    index_ivf.nprobe = 1

    print("=" * 80)
    print("Training FAISS index")
    print("=" * 80)

    index.train(big_npy)

    trained_index = os.path.join(
        exp_dir,
        f"trained_IVF{n_ivf}_Flat_nprobe_1_{exp_name}_{version}.index",
    )

    faiss.write_index(
        index,
        trained_index,
    )

    print("=" * 80)
    print("Adding vectors")
    print("=" * 80)

    batch = 8192

    for i in range(0, len(big_npy), batch):
        index.add(
            big_npy[i:i + batch]
        )

    added_index = os.path.join(
        exp_dir,
        f"added_IVF{n_ivf}_Flat_nprobe_1_{exp_name}_{version}.index",
    )

    faiss.write_index(
        index,
        added_index,
    )

    print("=" * 80)
    print("Index Generated")
    print("=" * 80)

    print(added_index)
    print("=" * 80)
    print("Index Generated")
    print("=" * 80)

    print("Path:", added_index)
    print("Exists:", os.path.exists(added_index))
    print("Size:", os.path.getsize(added_index), "bytes")

    return added_index

@app.function(
    image=image,
    gpu="L4",
    timeout=43200,  # 12 hours — 300 epochs takes ~4-6 hrs on L4
    volumes={"/root/rvc-models": volume},
)
def train():

    # Reload volume to see config, filelist, features from previous steps
    volume.reload()

    os.chdir("/root/rvc")
    if not os.path.exists("/root/rvc/logs"):
        os.symlink("/root/rvc-models/logs", "/root/rvc/logs")
    exp = "/root/rvc/logs/mi-test"

    for folder in [
        "0_gt_wavs",
        "2a_f0",
        "2b-f0nsf",
        "3_feature768",
    ]:
        src = os.path.join(exp, folder)
        dst = os.path.join("/root/rvc", folder)

        if not os.path.exists(dst):
            os.symlink(src, dst)

    print("/root/rvc/logs exists:", os.path.exists("/root/rvc/logs"))
    print("Contents:", os.listdir("/root/rvc/logs"))

    print("Current directory:", os.getcwd())

    print("/root/rvc exists:", os.path.exists("/root/rvc"))
    print("/root/rvc/logs exists:", os.path.exists("/root/rvc/logs"))
    print("/root/rvc-models/logs exists:", os.path.exists("/root/rvc-models/logs"))

    if os.path.exists("/root/rvc/logs"):
        print("Contents of /root/rvc/logs:")
        print(os.listdir("/root/rvc/logs"))

    if os.path.exists("/root/rvc-models/logs"):
        print("Contents of /root/rvc-models/logs:")
        print(os.listdir("/root/rvc-models/logs"))

    if "/root/rvc" not in sys.path:
        sys.path.insert(0, "/root/rvc")
    command = [
        sys.executable,
        "infer/modules/train/train.py",
        "-e", "mi-test",
        "-sr", "48k",
        "-f0", "1",
        "-bs", "8",
        "-g", "0",
        "-te", "150",
        "-se", "50",
        "-pg", "assets/pretrained_v2/f0G48k.pth",
        "-pd", "assets/pretrained_v2/f0D48k.pth",
        "-l", "0",
        "-c", "0",
        "-sw", "1",   # save .pth weights every 50 epochs (safety against crashes)
        "-v", "v2",
    ]

        
    print("Starting RVC Training")
        
    print("Running command:")
    print(" ".join(command))
    print()
        
    # Run without capture_output so training progress streams live to Modal logs
    result = subprocess.run(command, check=True)

    print("Training Finished")

    print("=" * 80)
    print("Generating FAISS Index")
    print("=" * 80)

    index_path = train_index(
        "mi-test",
        "v2",
    )

    print("Generated index:")
    print(index_path)

    print("=" * 80)
    print("Checking assets")
    print("=" * 80)

    print("assets exists:", os.path.exists("/root/rvc/assets"))
    print("assets/weights exists:", os.path.exists("/root/rvc/assets/weights"))

    if os.path.exists("/root/rvc/assets"):
        print("Assets contents:")
        print(os.listdir("/root/rvc/assets"))

    if os.path.exists("/root/rvc/assets/weights"):
        print("Weights contents:")
        print(os.listdir("/root/rvc/assets/weights"))
    
    for root, dirs, files in os.walk("/root/rvc-models/logs/mi-test"):
        print(root)
        for f in files:
            print("   ", f)

    print("=" * 80)
    print("Checking root")
    print("=" * 80)

    weight_file = "/root/rvc/assets/weights/mi-test.pth"

    print("Exists:", os.path.exists(weight_file))

    if os.path.exists(weight_file):
        print("Size:", os.path.getsize(weight_file), "bytes")

    os.makedirs("/root/rvc-models/weights", exist_ok=True)

    shutil.copy2(
        "/root/rvc/assets/weights/mi-test.pth",
        "/root/rvc-models/weights/mi-test.pth",
    )

    print("Copied model to Modal Volume")
    print(os.listdir("/root/rvc-models/weights"))

    for f in os.listdir("/root/rvc"):
        print(f)
    volume.commit()
    return "Training Complete"

@app.function(
    image=image,
    volumes={"/root/rvc-models": volume},
)
def download_model():

    model_path = "/root/rvc-models/weights/mi-test.pth"

    if not os.path.exists(model_path):
        raise FileNotFoundError(f"{model_path} not found")

    print("Model found:", model_path)
    print("Size:", os.path.getsize(model_path), "bytes")

    with open(model_path, "rb") as f:
        return f.read()

@app.function(
    image=image,
    gpu="T4",
    timeout=10800,
    volumes={"/root/rvc-models": volume},
)
def inference():

    os.chdir("/root/rvc")

    if "/root/rvc" not in sys.path:
        sys.path.insert(0, "/root/rvc")

    from configs.config import Config
    from infer.modules.vc.modules import VC

    os.environ["weight_root"] = "/root/rvc-models/weights"
    os.environ["index_root"] = "/root/rvc-models/logs/mi-test"

    config = Config()

    vc = VC(config)

    print("Loading model...")

    vc.get_vc("mi-test.pth")

    print("Model loaded successfully")

@app.function(
    image=image,
    volumes={"/root/rvc-models": volume},
)
def delete_checkpoints():
    exp_dir = "/root/rvc-models/logs/mi-test"
    weights_dir = "/root/rvc-models/weights"

    # Wipe entire experiment folder (preprocessed audio, features, f0, checkpoints, indexes)
    if os.path.exists(exp_dir):
        print(f"Deleting entire experiment directory: {exp_dir}")
        shutil.rmtree(exp_dir)

    # Recreate empty experiment directory ready for fresh training
    os.makedirs(exp_dir, exist_ok=True)
    print(f"Recreated empty: {exp_dir}")

    # Remove old weight file
    model_pth = os.path.join(weights_dir, "mi-test.pth")
    if os.path.exists(model_pth):
        print(f"Deleting weight file: {model_pth}")
        os.remove(model_pth)

    volume.commit()
    print("All previous training data, logs, and weights successfully deleted.")

@app.function(
    image=image,
    volumes={"/root/rvc-models": volume},
)
def list_indexes():
    import glob

    indexes = glob.glob("/root/rvc-models/logs/mi-test/*.index")

    print("Index files:")
    for idx in indexes:
        print(idx)



@app.local_entrypoint()
def main():

    print("--- Step 1: Cleaning up ALL old training data ---")
    delete_checkpoints.remote()

    print("--- Step 2: Preprocessing raw audio ---")
    preprocess.remote()

    print("--- Step 3: Extracting F0 pitch (RMVPE) ---")
    extract_f0.remote()

    print("--- Step 4: Extracting HuBERT features (GPU) ---")
    extract_features.remote()

    print("--- Step 5: Generating filelist and config ---")
    generate_filelist.remote()
    generate_config.remote()

    print("--- Step 6: Training model (300 epochs) ---")
    train.remote()

    print("--- Step 7: Done! Listing indexes ---")
    list_indexes.remote()


@app.local_entrypoint()
def train_only():
    """Skip preprocessing — jump straight to training + index.
    Use this when preprocessed files + features are already in the volume
    and you just want to (re)train or resume from a checkpoint.
    """
    print("--- Training model (300 epochs on L4) ---")
    train.remote()

    print("--- Done! Listing indexes ---")
    list_indexes.remote()
