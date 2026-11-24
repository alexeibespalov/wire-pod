# Cortana wake word

`cortana.tflite` is an [openWakeWord](https://github.com/dscripka/openWakeWord) model that
fires on **"Cortana"** and **"Hey Cortana"** (one model, both phrases). `../setup.sh` installs it
on the Pi Zero (`~/wyoming-openwakeword/custom/`), points wyoming-openwakeword at it and runs
the satellite with `--wake-word-name cortana --wake-word-name okay_nabu` ("Okay Nabu" stays as
a fallback). Sensitivity: `WAKE_THRESHOLD=0.4 ./setup.sh <MAC>` (default 0.5; lower = more eager).

## How it was trained (2026-11-24, RTX 4060 laptop, ~2 h)

No recordings of anyone: every "Cortana" is synthetic (Piper's LibriTTS-R generator, ~800
voices, 3 speeds), mixed with room echo (MIT RIRs), background noise (AudioSet) and music (FMA),
against ~2,000 h of precomputed negative speech/noise features (ACAV100M).

Recipe: copy `training/*` into a scratch dir (e.g. `~/Development/cortana-wakeword`) and work there:

1. `git clone https://github.com/dscripka/openWakeWord` (at 368c037) and
   `git clone https://github.com/rhasspy/piper-sample-generator && git -C piper-sample-generator checkout v2.0.0`
   (train.py imports its old `generate_samples.py` layout), then apply the two `.patch` files
   (`git apply --ignore-whitespace`).
2. Python 3.10 venv: torch/torchaudio 2.1.2+cu121, `piper-phonemize==1.1.0`, and the
   notebook's pins (`tensorflow-cpu==2.8.1`, `onnx_tf==1.10.0`, `datasets==2.14.6`,
   `speechbrain==0.5.14`, `audiomentations==0.33.0`, `torch-audiomentations==0.11.0`, ...) plus
   `protobuf==3.20.1 onnx==1.12.0 pyarrow<15 setuptools<70`, and `faster-whisper==1.0.3
   ctranslate2==4.3.1` (needs `patchelf --clear-execstack` on its libs on new kernels, and
   `LD_LIBRARY_PATH=<venv>/.../torch/lib` for cuDNN 8).
3. `python download.py` (~25 GB).
4. `python gen_negatives.py`, then `python gen_positives.py 25000 2500`
   (several in parallel; `ONLY=train` for the extras).
5. `python openWakeWord/openwakeword/train.py --training_config cortana.yaml --augment_clips`
   then `... --train_model --convert_to_tflite`. Output: `my_custom_model/cortana.tflite`.

## Gotchas found on the way (the patches)

* **The K of "Cortana" went missing.** Piper's generator swallows the first consonant of a
  clip ("Ortana", "for Tana"), and the stock silence trimmer also cut quiet consonants out of
  the middle. Fixes: trim silence only at the ends (`piper-sample-generator.patch`), three
  pause slots before the word, and `gen_positives.py` transcribes every clip with Whisper on
  the GPU and keeps only ones heard as Cortana (about half; "Hey Cortana" too).
* **"cortana" was being trained as a negative.** For the phrase "hey cortana" the adversarial
  generator returns partial phrases that can be just "cortana" (~15%). `train.py` now drops
  any negative containing the shared word (`_drop_targets`).
* DeepPhonemizer's model URL is dead; "cortana" isn't in CMUdict, so `data.py` adds it
  (`K AO0 R T AA1 N AH0`).
* The stock generator walks speaker pairs in order (only the first few dozen voices ever used);
  `gen_positives.py` shuffles them.
* AudioSet on Hugging Face is parquet now (read with pyarrow); feature extraction runs on CPU
  (onnxruntime here has no CUDA build).
