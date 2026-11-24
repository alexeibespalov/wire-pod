"""The Wyoming conversation with a (fake) wyoming-satellite, end to end minus the models."""
import asyncio
import math
import random
import struct

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import async_read_event, async_write_event
from wyoming.ping import Ping
from wyoming.pipeline import PipelineStage, RunPipeline
from wyoming.snd import Played
from wyoming.wake import Detection

from app import satellite, voice

RATE = 16000


def tone(seconds: float, amp: int = 9000) -> bytes:
    """A voice-ish buzz: a 160 Hz harmonic stack with a little noise (webrtcvad says speech)."""
    rnd = random.Random(1)
    out = []
    for i in range(int(RATE * seconds)):
        t = i / RATE
        v = sum(math.sin(2 * math.pi * 160 * k * t) / k for k in range(1, 12))
        out.append(int(max(-32767, min(32767, amp * v / 3 + rnd.uniform(-300, 300)))))
    return struct.pack(f"<{len(out)}h", *out)


def silence(seconds: float) -> bytes:
    return bytes(int(RATE * seconds) * 2)


def test_endpointer():
    ep = voice.Endpointer(silence_s=0.6, start_timeout_s=2, max_s=10, mode=2)
    assert ep.feed(silence(0.5)) is None
    assert ep.feed(tone(1.0)) is None and ep.started
    assert ep.feed(silence(1.0)) == "done"
    ep = voice.Endpointer(start_timeout_s=1.0, mode=2)
    assert ep.feed(silence(1.5)) == "timeout"


def test_tidy_transcript():
    assert voice.tidy_transcript("[BLANK_AUDIO]", 0.1) == ""
    assert voice.tidy_transcript(" Thank you. ", 0.3) == ""
    assert voice.tidy_transcript("(music) What's the weather?", 2.0) == "What's the weather?"


def run_with_fake_satellite(script, monkeypatch, heard_text="what time is it"):
    """script(reader, writer, events) drives the fake satellite side."""
    heard = []

    async def fake_transcribe(pcm):
        return heard_text

    async def fake_synth(text):
        yield AudioChunk(rate=22050, width=2, channels=1, audio=bytes(4410))

    monkeypatch.setattr(voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(voice, "synthesize", fake_synth)

    async def main():
        done = asyncio.Event()
        received = []

        async def handle(reader, writer):
            try:
                await script(reader, writer, received, sat)
            finally:
                done.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def on_request(text, followup):
            heard.append((text, followup))

        sat = satellite.Satellite(f"tcp://127.0.0.1:{port}", on_request)
        task = asyncio.create_task(sat.run())
        await asyncio.wait_for(done.wait(), 10)
        task.cancel()
        server.close()
        return received

    return asyncio.run(main()), heard


async def read_until(reader, typ, received, timeout=5):
    while True:
        ev = await asyncio.wait_for(async_read_event(reader), timeout)
        received.append(ev.type)
        if ev.type == typ:
            return ev


def test_wake_to_transcript_to_reply(monkeypatch):
    async def script(reader, writer, received, sat):
        await read_until(reader, "run-satellite", received)
        await async_write_event(Ping(text="x").event(), writer)
        await read_until(reader, "pong", received)
        await async_write_event(Detection(name="okay_nabu").event(), writer)
        await async_write_event(RunPipeline(start_stage=PipelineStage.ASR, end_stage=PipelineStage.TTS).event(), writer)
        for pcm in (silence(0.3), tone(1.2), silence(1.2)):
            for i in range(0, len(pcm), 2048):
                await async_write_event(AudioChunk(rate=RATE, width=2, channels=1, audio=pcm[i:i + 2048]).event(), writer)
        ev = await read_until(reader, Transcript.Type if hasattr(Transcript, "Type") else "transcript", received)
        # the agent would reply now: speak through an Utterance, satellite reports played
        async def reply():
            async with sat.speak_lock:
                u = sat.utterance()
                await u.say("It's ten past six.")
                return await u.close()
        t = asyncio.create_task(reply())
        await read_until(reader, "audio-start", received)
        await read_until(reader, "audio-stop", received)
        await async_write_event(Played().event(), writer)
        assert await t is True
        await sat.listen_again()
        await read_until(reader, "detection", received)
        # follow-up: nobody answers -> quiet error, no transcript
        await async_write_event(RunPipeline(start_stage=PipelineStage.ASR, end_stage=PipelineStage.TTS).event(), writer)
        pcm = silence(6)
        for i in range(0, len(pcm), 2048):
            await async_write_event(AudioChunk(rate=RATE, width=2, channels=1, audio=pcm[i:i + 2048]).event(), writer)
        await read_until(reader, "error", received)

    received, heard = run_with_fake_satellite(script, monkeypatch)
    assert heard == [("what time is it", False)]
    assert received.count("transcript") == 1 and "audio-chunk" in received
