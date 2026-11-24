"""The shimmer voice effect: same length, silence stays silent, streaming == one go."""
import math
from array import array

from app.voice import Shimmer


def tone(n, rate=22050):
    return array("h", (int(8000 * math.sin(2 * math.pi * 220 * k / rate)) for k in range(n))).tobytes()


def test_shimmer_keeps_length_and_silence():
    fx = Shimmer(22050)
    assert fx.process(bytes(4000)) == bytes(4000)
    assert len(fx.process(tone(1000))) == 2000
    assert len(fx.tail()) == int(22050 * 0.06) * 2
    assert any(fx.tail()) is False                 # rung out: silence again


def test_shimmer_streams_like_one_block():
    pcm = tone(5000)
    whole = Shimmer(22050).process(pcm)
    fx = Shimmer(22050)
    parts = b"".join(fx.process(pcm[i:i + 2048]) for i in range(0, len(pcm), 2048))
    assert parts == whole and parts != pcm
