"""Ears and voice, all on the Pi 5:

* Endpointer: webrtcvad on the satellite's 16 kHz mic stream decides when the person has
  finished speaking (or never started).
* transcribe(): whisper.cpp's whisper-server (the `vector-whisper` container Vector's
  wire-pod also uses), POST /inference with a WAV.
* synthesize(): Wyoming Piper (the `piper` container), one sentence at a time.
"""
import io
import math
import logging
import re
import wave
from array import array
from collections import deque
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import httpx
import webrtcvad
from wyoming.audio import AudioChunk, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.tts import Synthesize, SynthesizeVoice

from . import config

log = logging.getLogger("cortana-agent")

RATE, WIDTH, CHANNELS = 16000, 2, 1          # what Whisper and the VAD want
FRAME_MS = 30
FRAME_BYTES = RATE * WIDTH * FRAME_MS // 1000
_FRAME_S = FRAME_MS / 1000


def host_port(uri: str) -> tuple[str, int]:
    u = urlparse(uri)
    if u.scheme != "tcp" or not u.hostname or not u.port:
        raise ValueError(f"expected tcp://host:port, got {uri!r}")
    return u.hostname, u.port


class Endpointer:
    """feed(pcm) -> None while listening, then "done" (spoke, then went quiet), "timeout"
    (never started speaking) or "max" (spoke too long; use what we have)."""

    START_FRAMES, START_WINDOW = 6, 10       # ~180 ms of speech within 300 ms = started

    def __init__(self, silence_s: float | None = None, start_timeout_s: float | None = None,
                 max_s: float | None = None, mode: int | None = None):
        self.vad = webrtcvad.Vad(config.VAD_MODE if mode is None else mode)
        self.silence_s = config.VAD_SILENCE_S if silence_s is None else silence_s
        self.start_timeout_s = config.VAD_START_TIMEOUT_S if start_timeout_s is None else start_timeout_s
        self.max_s = config.VAD_MAX_S if max_s is None else max_s
        self.audio = bytearray()
        self._buf = b""
        self._recent: deque[bool] = deque(maxlen=self.START_WINDOW)
        self.t = 0.0                  # seconds of audio seen
        self.speech_s = 0.0           # seconds of it that sounded like speech
        self.started = False
        self._quiet = 0.0
        self.result: str | None = None

    def feed(self, pcm: bytes) -> str | None:
        if self.result:
            return self.result
        self.audio += pcm
        self._buf += pcm
        while len(self._buf) >= FRAME_BYTES:
            frame, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
            self.t += _FRAME_S
            voiced = self.vad.is_speech(frame, RATE)
            self._recent.append(voiced)
            if voiced:
                self.speech_s += _FRAME_S
            if not self.started:
                if sum(self._recent) >= self.START_FRAMES:
                    self.started, self._quiet = True, 0.0
                elif self.t >= self.start_timeout_s:
                    self.result = "timeout"
            else:
                self._quiet = 0.0 if voiced else self._quiet + _FRAME_S
                if self._quiet >= self.silence_s:
                    self.result = "done"
            if not self.result and self.t >= self.max_s:
                self.result = "max"
            if self.result:
                return self.result
        return None


def wav_bytes(pcm: bytes) -> bytes:
    if (pad := RATE * WIDTH - len(pcm)) > 0:        # whisper.cpp wants at least 1 s
        pcm = pcm + bytes(pad)
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(WIDTH)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return b.getvalue()


# Whisper invents text on silence and noise: stock phrases, [BLANK_AUDIO], (music)...
_TAG = re.compile(r"\[[A-Za-z_ ]+\]|\([a-zA-Z ]+\)|\*[a-zA-Z ]+\*")
_GHOST = re.compile(r"(?i)^\W*(thank you( (so much|for watching))?|thanks( for watching)?|you|bye|"
                    r"okay|oh|um+|uh+|hmm+|\.+)\W*$")
_NOISE_WORDS = {"", "the", "a", "an", "uh", "um", "hmm", "huh", "ah", "oh", "and", "it", "i"}


def tidy_transcript(text: str, speech_s: float) -> str:
    """'' when it's (probably) not real speech."""
    text = " ".join(_TAG.sub(" ", text or "").split())
    if _GHOST.match(text) and speech_s < 0.8:
        return ""
    words = re.sub(r"[^a-z' ]", " ", text.lower()).split()
    if not words or (len(words) == 1 and words[0] in _NOISE_WORDS):
        return ""
    return text


async def transcribe(pcm: bytes) -> str:
    async with httpx.AsyncClient(timeout=config.WHISPER_TIMEOUT) as c:
        r = await c.post(f"{config.WHISPER_URL}/inference",
                         files={"file": ("audio.wav", wav_bytes(pcm), "audio/wav")},
                         data={"response_format": "json", "temperature": "0"})
        r.raise_for_status()
        return " ".join(str(r.json().get("text", "")).split())


class Shimmer:
    """A light "digital AI" sheen on a mono 16-bit voice (VOICE_FX=shimmer): two slowly
    wavering chorus copies (12 and 21 ms, swept a few ms at 0.6 / 0.93 Hz) and a faint 45 ms
    echo under the dry voice. Streaming: keeps its delay line between chunks; tail() lets the
    last echoes ring out. Plain Python; ~a few % of a Pi 5 core at Piper's 22 kHz."""

    DRY, C1, C2, ECHO = 0.72, 0.30, 0.20, 0.14

    def __init__(self, rate: int):
        self.rate = rate
        self.n = int(rate * 0.08)                # longest delay (21+4 ms, 45 ms echo) fits
        self.buf = [0.0] * self.n
        self.i = 0
        self.t = 0
        self.echo = int(rate * 0.045)

    def process(self, pcm: bytes) -> bytes:
        src = array("h")
        src.frombytes(pcm[: len(pcm) // 2 * 2])
        out = array("h", bytes(len(src) * 2))
        buf, n, rate, sin = self.buf, self.n, self.rate, math.sin
        w1, w2 = 2 * math.pi * 0.6 / rate, 2 * math.pi * 0.93 / rate
        i, t = self.i, self.t

        def tap(d: float) -> float:
            p = i - d
            f = math.floor(p)
            fr = p - f
            return buf[f % n] * (1 - fr) + buf[(f + 1) % n] * fr

        for k, x in enumerate(src):
            buf[i] = x
            d1 = (0.012 + 0.003 * sin(w1 * t)) * rate
            d2 = (0.021 + 0.004 * sin(w2 * t + 1.3)) * rate
            y = self.DRY * x + self.C1 * tap(d1) + self.C2 * tap(d2) + self.ECHO * buf[(i - self.echo) % n]
            out[k] = 32767 if y > 32767 else -32768 if y < -32768 else int(y)
            i = (i + 1) % n
            t += 1
        self.i, self.t = i, t
        return out.tobytes()

    def tail(self) -> bytes:
        return self.process(bytes(int(self.rate * 0.06) * 2))


async def synthesize(text: str) -> AsyncIterator[AudioChunk]:
    """Piper's audio for one sentence, chunk by chunk (with the voice effect, if any)."""
    fx: Shimmer | None = None
    last: AudioChunk | None = None
    async for chunk in _piper(text):
        if config.VOICE_FX == "shimmer" and chunk.width == 2 and chunk.channels == 1:
            fx = fx or Shimmer(chunk.rate)
            chunk = AudioChunk(rate=chunk.rate, width=2, channels=1, audio=fx.process(chunk.audio))
        last = chunk
        yield chunk
    if fx and last:
        yield AudioChunk(rate=last.rate, width=2, channels=1, audio=fx.tail())


async def _piper(text: str) -> AsyncIterator[AudioChunk]:
    """Piper's audio for one sentence, chunk by chunk."""
    host, port = host_port(config.PIPER_URI)
    client = AsyncTcpClient(host, port, connect_timeout=5, read_timeout=20)
    await client.connect()
    try:
        voice = SynthesizeVoice(name=config.PIPER_VOICE) if config.PIPER_VOICE else None
        await client.write_event(Synthesize(text=text, voice=voice).event())
        while True:
            ev = await client.read_event()
            if ev is None:
                raise ConnectionError("piper closed the connection")
            if AudioChunk.is_type(ev.type):
                yield AudioChunk.from_event(ev)
            elif AudioStop.is_type(ev.type):
                return
            elif Error.is_type(ev.type):
                raise RuntimeError(f"piper: {Error.from_event(ev).text}")
    finally:
        await client.disconnect()
