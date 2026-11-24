"""The adversarial-negative half of openWakeWord's train.py --generate_clips (same settings),
run separately so it doesn't race gen_positives.py. train.py skips these dirs once filled."""
import os, sys, uuid, yaml, torch
sys.path.insert(0, os.path.abspath("piper-sample-generator"))
from generate_samples import generate_samples
from openwakeword.data import generate_adversarial_texts
from openwakeword.train import _drop_targets

cfg = yaml.safe_load(open("cortana.yaml"))
base = os.path.join(cfg["output_dir"], cfg["model_name"])
for sub, n, noise in (("negative_test", cfg["n_samples_val"], 1.0), ("negative_train", cfg["n_samples"], 0.98)):
    out = os.path.join(base, sub)
    os.makedirs(out, exist_ok=True)
    have = len(os.listdir(out))
    if have > 0.95 * n:
        continue
    texts = [t for t in cfg["custom_negative_phrases"] for _ in range(8)]
    for tp in cfg["target_phrase"]:
        texts += generate_adversarial_texts(input_text=tp, N=n // len(cfg["target_phrase"]),
                                            include_partial_phrase=1.0, include_input_words=0.2)
    texts = _drop_targets(texts, cfg["target_phrase"])
    import random; random.shuffle(texts)
    generate_samples(text=texts, max_samples=n - have, batch_size=cfg["tts_batch_size"] // 7,
                     noise_scales=[noise], noise_scale_ws=[noise], length_scales=[0.75, 1.0, 1.25],
                     model="piper-sample-generator/models/en_US-libritts_r-medium.pt",
                     output_dir=out, auto_reduce_batch_size=True,
                     file_names=[uuid.uuid4().hex + ".wav" for _ in range(n)])
    torch.cuda.empty_cache()
    print(sub, "done", len(os.listdir(out)), flush=True)
print("NEGATIVES DONE")
