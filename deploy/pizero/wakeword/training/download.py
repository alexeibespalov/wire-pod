"""Download everything openWakeWord's trainer needs (from its automatic_model_training notebook),
a bit more background audio than the notebook's demo. Re-runnable: skips what's there."""
import io
import math
import os
import subprocess
import tarfile
from pathlib import Path

import datasets
import numpy as np
import pyarrow.parquet
import scipy.io.wavfile
import scipy.signal
import soundfile
from tqdm import tqdm

ROOT = Path(__file__).parent
os.chdir(ROOT)


def wget(url, out):
    out = Path(out)
    if out.exists() and out.stat().st_size > 0:
        print("have", out)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["wget", "-q", "--show-progress", "--progress=dot:giga", "-c", "-O", str(out) + ".part", url], check=True)
    os.rename(str(out) + ".part", out)


def save(path, arr):
    scipy.io.wavfile.write(path, 16000, (np.clip(arr, -1, 1) * 32767).astype(np.int16))


# 1. TTS generator + openWakeWord's feature front end
wget("https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt",
     "piper-sample-generator/models/en_US-libritts_r-medium.pt")
for m in ("embedding_model", "melspectrogram"):
    for ext in ("onnx", "tflite"):
        wget(f"https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{m}.{ext}",
             f"openWakeWord/openwakeword/resources/models/{m}.{ext}")

# 2. negative features (~2,000 h of speech/noise/music) + false-positive validation set (~11 h)
wget("https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy",
     "validation_set_features.npy")
wget("https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
     "openwakeword_features_ACAV100M_2000_hrs_16bit.npy")

# 3. room impulse responses (MIT)
out = Path("mit_rirs")
if not (out.exists() and len(list(out.iterdir())) > 200):
    out.mkdir(exist_ok=True)
    for row in tqdm(datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True),
                    desc="rirs"):
        save(out / row["audio"]["path"].split("/")[-1], row["audio"]["array"])

# 4. background noise: 8 AudioSet balanced-train parquet parts (~22 h of everyday sounds)
out = Path("audioset_16k")
out.mkdir(exist_ok=True)
for part in [f"{i:02d}" for i in (0, 4, 8, 12, 16, 20, 24, 28)]:
    pq = Path(f"audioset/{part}.parquet")
    done = Path(f"audioset/{part}.done")
    if done.exists():
        continue
    wget(f"https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/{part}.parquet", pq)
    # (read with pyarrow: this old `datasets` can't parse the new parquet metadata)
    f = pyarrow.parquet.ParquetFile(pq)
    k = 0
    for g in tqdm(range(f.num_row_groups), desc=f"audioset {part}"):
        for a in f.read_row_group(g, columns=["audio"]).column("audio").to_pylist():
            try:
                x, sr = soundfile.read(io.BytesIO(a["bytes"]), dtype="float32", always_2d=True)
                x = x.mean(axis=1)
                if sr != 16000:
                    gcd = math.gcd(sr, 16000)
                    x = scipy.signal.resample_poly(x, 16000 // gcd, sr // gcd)
                save(out / f"as{part}_{k}.wav", x)
            except Exception as e:
                print("skip", part, k, e)
            k += 1
    done.touch()
    pq.unlink()

# 5. background music: 5 hours of the Free Music Archive (30 s clips)
out = Path("fma")
out.mkdir(exist_ok=True)
want = 5 * 3600 // 30
if len(list(out.iterdir())) < want:
    fma = iter(datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
               .cast_column("audio", datasets.Audio(sampling_rate=16000)))
    for _ in tqdm(range(want), desc="fma"):
        row = next(fma)
        save(out / row["audio"]["path"].split("/")[-1].replace(".mp3", ".wav"), row["audio"]["array"])

print("ALL DOWNLOADS DONE")
