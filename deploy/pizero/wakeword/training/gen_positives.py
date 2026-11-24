"""Positive clips for the Cortana wake word, checked by ear (Whisper).

Piper's LibriTTS generator tends to swallow the K of "Cortana" when it's the first sound of a
clip ("Ortana", "for Tana"). Three pause slots before the word fix that about half the time,
so we generate more than needed, transcribe every clip with Whisper on the GPU, and keep only
the ones heard as Cortana with the K. Speaker pairs are shuffled across all voices (the stock
generator walks them in order, so only the first few dozen ever get used).

Writes into the dirs openWakeWord's train.py reads (it then skips its own positive generation).
"""
import itertools
import os
import random
import re
import shutil
import sys
import uuid
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "piper-sample-generator"))
import generate_samples as g  # noqa: E402

site = Path(torch.__file__).parent / "lib"
os.environ["LD_LIBRARY_PATH"] = f"{site}:{os.environ.get('LD_LIBRARY_PATH', '')}"
from faster_whisper import WhisperModel  # noqa: E402

MODEL = str(ROOT / "piper-sample-generator/models/en_US-libritts_r-medium.pt")
OUT = ROOT / "my_custom_model/cortana"
PHRASES = {  # text -> (share of clips, pause slots before it, accepted transcript)
    "cortana": (0.6, 3, re.compile(r"^[ck]oh?r?t[aeiou]h?n+[aeiou]h?$")),
    "hey cortana": (0.4, 0, re.compile(r"^(hey|hay|hi) ?[ck]oh?r?t[aeiou]h?n+[aeiou]h?$")),
}


class _ShuffledPairs:
    """itertools, but product() comes back shuffled (spread clips over all speakers)."""
    def __getattr__(self, k):
        return getattr(itertools, k)

    def product(self, *a, **k):
        pairs = list(itertools.product(*a, **k))
        random.shuffle(pairs)
        return pairs


g.it = _ShuffledPairs()
_orig_phonemes = g.get_phonemes
PAD = {"n": 0}


def _padded(voice, config, text, verbose):
    ids = _orig_phonemes(voice, config, text, verbose)
    return ids[:1] + config["phoneme_id_map"]["_"] * PAD["n"] + ids[1:]


g.get_phonemes = _padded

whisper = WhisperModel("base.en", device="cuda", compute_type="float16")


def heard(path: Path) -> str:
    segs, _ = whisper.transcribe(str(path), beam_size=1, language="en", without_timestamps=True,
                                 condition_on_previous_text=False)
    return re.sub(r"[^a-z ]", "", " ".join(s.text for s in segs).lower()).strip()


def fill(dest: Path, n_total: int, noise: float):
    dest.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / f"tmp_gen_{os.getpid()}"
    for text, (share, pad, ok) in PHRASES.items():
        want = int(n_total * share)
        tag = text.replace(" ", "_")
        have = len(list(dest.glob(f"{tag}_*.wav")))
        tried = passed = 0
        while (have := len(list(dest.glob(f"{tag}_*.wav")))) < want:   # (others may be filling too)
            shutil.rmtree(tmp, ignore_errors=True)
            PAD["n"] = pad
            chunk = min(600, int((want - have) * 1.8) + 20)
            g.generate_samples(text=[text], max_samples=chunk, batch_size=40, output_dir=str(tmp), model=MODEL,
                               noise_scales=[noise], noise_scale_ws=[noise], length_scales=[0.75, 1.0, 1.25],
                               max_speakers=800, auto_reduce_batch_size=True)
            torch.cuda.empty_cache()                       # give the GPU back before Whisper runs
            for f in tmp.glob("*.wav"):
                tried += 1
                if have < want and ok.match(heard(f)):
                    shutil.move(f, dest / f"{tag}_{uuid.uuid4().hex}.wav")
                    have += 1
                    passed += 1
            print(f"{dest.name} {text!r}: {have}/{want} (kept {passed}/{tried})", flush=True)
            torch.cuda.empty_cache()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    n_train, n_test = int(sys.argv[1]), int(sys.argv[2])
    if os.environ.get("ONLY") != "train":
        fill(OUT / "positive_test", n_test, 1.0)
    fill(OUT / "positive_train", n_train, 0.98)
    print("POSITIVES DONE")
