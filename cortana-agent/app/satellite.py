"""The Pi Zero satellite (wyoming-satellite) as Cortana's ears and mouth.

wyoming-satellite is a Wyoming *server*; whoever connects to it runs the voice pipeline
(that used to be Home Assistant). We connect, send run-satellite, and then:

  satellite: wake word heard -> detection, run-pipeline, audio-chunk...
  us:        VAD says they've finished -> transcript (satellite stops streaming, plays
             its "done" chime) -> Whisper -> on_request(text) -> the agent
  us:        reply audio-start, audio-chunk... (Piper, sentence by sentence), audio-stop
  satellite: played (the speaker has finished)
  us:        a question? -> detection: the patched satellite (deploy/pizero) opens the
             mic again without the wake word (follow-up listening)

Unprompted speech (timers, reminders, /announce) is just more audio-start..audio-stop.
One utterance at a time (speak lock). Reconnects forever with backoff.
"""
import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.ping import Ping, Pong
from wyoming.pipeline import RunPipeline
from wyoming.satellite import RunSatellite
from wyoming.snd import Played
from wyoming.timer import TimerFinished
from wyoming.wake import Detection

from . import activity, config, music, voice

log = logging.getLogger("cortana-agent")

FOLLOW_UP_NAME = "follow_up"
FOLLOW_UP_START_S = 5.0          # after a follow-up chime: this long to start talking
PING_EVERY_S = 20.0


class Satellite:
    def __init__(self, uri: str, on_request: Callable[[str, bool], Awaitable[None]]):
        self.host, self.port = voice.host_port(uri)
        self.on_request = on_request
        self.client: AsyncTcpClient | None = None
        self._wlock = asyncio.Lock()
        self.speak_lock = asyncio.Lock()
        self._played = asyncio.Event()
        self._ep: voice.Endpointer | None = None
        self._ep_followup = False
        self._followup_asked = 0.0
        self._mic = AudioChunkConverter(rate=voice.RATE, width=voice.WIDTH, channels=voice.CHANNELS)
        # what the satellite's speaker command expects (sent in run-pipeline; these are its defaults)
        self.snd = {"rate": 22050, "width": 2, "channels": 1}
        self._tasks: set[asyncio.Task] = set()
        self.last_seen = 0.0

    @property
    def connected(self) -> bool:
        return self.client is not None

    def idle(self) -> bool:
        """Not listening, transcribing or talking (the music may come back)."""
        return self._ep is None and not self._tasks and not self.speak_lock.locked()

    # ------------------------------------------------------------------ connection
    async def run(self) -> None:
        delay = 2.0
        while True:
            client = AsyncTcpClient(self.host, self.port, connect_timeout=10)
            since = time.monotonic()
            try:
                await client.connect()
                self.client = client
                accepted = False
                await self._send(RunSatellite().event())
                # An idle satellite sends nothing; our pings (it answers pong, and starts
                # pinging us too) keep the link checked so a dead one is noticed.
                pinger = asyncio.get_running_loop().create_task(self._ping_loop())
                try:
                    while True:
                        ev = await asyncio.wait_for(client.read_event(), 3 * PING_EVERY_S)
                        if ev is None:
                            raise ConnectionError("satellite closed the connection")
                        self.last_seen = time.monotonic()
                        if not accepted:            # it answered: the session is ours
                            accepted = True
                            log.info("satellite: connected to %s:%s", self.host, self.port)
                            activity.record("voice", "satellite connected")
                        await self._handle(ev)
                finally:
                    pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                lasted = time.monotonic() - since
                if self.client is not None and lasted > 30:
                    log.warning("satellite: disconnected after %.0fs (%s)", lasted, e)
                    activity.record("voice", f"satellite disconnected ({e})")
                    delay = 1.0                         # it was a real session: retry soon
                elif self.client is not None and delay <= 2.0:
                    # rejected straight away: usually another server (Home Assistant) holds it
                    log.warning("satellite: connection refused by the satellite (%s); is another "
                                "server (Home Assistant's Wyoming integration) connected to it?", e)
                elif self.client is None and delay <= 2.0:
                    log.info("satellite: can't connect (%s); retrying", e)
            finally:
                self.client = None
                self._ep = None
                self._played.set()                      # nobody waits for a lost speaker
                with contextlib.suppress(Exception):
                    await client.disconnect()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _ping_loop(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.sleep(1)
            while True:
                await self._send(Ping(text=str(int(time.time()))).event())
                await asyncio.sleep(PING_EVERY_S)

    async def _send(self, ev: Event) -> None:
        if self.client is None:
            raise ConnectionError("satellite not connected")
        if not Ping.is_type(ev.type) and not Pong.is_type(ev.type):
            music.PLAYER.touch()
        async with self._wlock:
            await self.client.write_event(ev)

    # ---------------------------------------------------------------------- events
    async def _handle(self, ev: Event) -> None:
        if not Ping.is_type(ev.type) and not Pong.is_type(ev.type):
            music.PLAYER.touch()
        if AudioChunk.is_type(ev.type):
            if self._ep is not None:
                chunk = self._mic.convert(AudioChunk.from_event(ev))
                if reason := self._ep.feed(chunk.audio):
                    await self._end_capture(reason)
        elif Ping.is_type(ev.type):
            await self._send(Pong(text=Ping.from_event(ev).text).event())
        elif RunPipeline.is_type(ev.type):
            if fmt := (ev.data or {}).get("snd_format"):
                self.snd = {k: int(fmt.get(k, v)) for k, v in self.snd.items()}
            followup = time.monotonic() - self._followup_asked < 4
            self._followup_asked = 0.0
            self._ep = voice.Endpointer(start_timeout_s=FOLLOW_UP_START_S if followup else None)
            self._ep_followup = followup
            self._spawn(music.PLAYER.duck())
            log.info("satellite: listening%s", " (follow-up)" if followup else "")
        elif AudioStop.is_type(ev.type):
            if self._ep is not None:
                await self._end_capture("stopped")
        elif Played.is_type(ev.type):
            self._played.set()
        elif Detection.is_type(ev.type):
            log.info("satellite: wake word %s", Detection.from_event(ev).name)
            self._spawn(music.PLAYER.duck())
        elif Error.is_type(ev.type):
            log.warning("satellite: error %s", Error.from_event(ev).text)

    async def _end_capture(self, reason: str) -> None:
        ep, followup = self._ep, self._ep_followup
        self._ep = None
        if ep is None:
            return
        if reason == "timeout" or not ep.started:
            # nobody spoke: stop streaming quietly (an error, not a transcript: no chime)
            log.info("satellite: no speech (%s, %.1fs)", reason, ep.t)
            await self._send(Error(text="no speech", code="no-speech").event())
            return
        log.info("satellite: end of speech (%s) after %.1fs, %.1fs voiced", reason, ep.t, ep.speech_s)
        await self._send(Transcript(text="").event())    # stop streaming + "done" chime now
        self._spawn(self._understand(bytes(ep.audio), ep.speech_s, followup))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _understand(self, pcm: bytes, speech_s: float, followup: bool) -> None:
        t0 = time.monotonic()
        try:
            raw = await voice.transcribe(pcm)
        except Exception as e:
            log.error("satellite: speech-to-text failed: %s", e)
            activity.record("voice", f"couldn't transcribe ({e})")
            await self.say_text("Sorry, my ears aren't working right now.")
            return
        text = voice.tidy_transcript(raw, speech_s)
        log.info("satellite: heard %r in %.1fs%s", raw, time.monotonic() - t0,
                 "" if text else " (treated as silence)")
        if text:
            await self.on_request(text, followup)

    # --------------------------------------------------------------------- speaking
    def utterance(self) -> "Utterance":
        return Utterance(self)

    async def say_text(self, text: str) -> None:
        """Say something on its own (announcements, reminders, errors)."""
        async with self.speak_lock:
            u = self.utterance()
            await u.say(text)
            await u.close()

    async def end_quietly(self, why: str) -> None:
        """A request she won't answer ("never mind"): tell the satellite it's over (its
        error hook turns the LED off) instead of leaving it waiting for a reply."""
        with contextlib.suppress(Exception):
            await self._send(Error(text=why, code="no-reply").event())

    async def ring_timer(self, timer_id: str) -> None:
        """The satellite's own timer chime (--timer-finished-wav)."""
        await self._send(TimerFinished(id=timer_id).event())

    async def listen_again(self) -> None:
        """Re-open the mic without the wake word (needs the satellite patch)."""
        self._followup_asked = time.monotonic()
        await self._send(Detection(name=FOLLOW_UP_NAME, timestamp=int(time.time() * 1000)).event())


class Utterance:
    """One reply: sentences go in as they're written, Piper turns each into audio, and it
    all plays as a single audio-start ... audio-stop on the satellite. Hold sat.speak_lock
    for the utterance's lifetime."""

    def __init__(self, sat: Satellite):
        self.sat = sat
        self.q: asyncio.Queue[str | None] = asyncio.Queue()
        self.started = False
        self.seconds = 0.0
        self.first_audio_at: float | None = None
        self._out = AudioChunkConverter(**sat.snd)
        self._worker = asyncio.get_running_loop().create_task(self._run())

    async def say(self, sentence: str) -> None:
        if sentence.strip():
            self.q.put_nowait(sentence)

    async def _run(self) -> None:
        while (s := await self.q.get()) is not None:
            music.tell_speaker(f"say {s}")
            try:
                async for chunk in voice.synthesize(s):
                    chunk = self._out.convert(chunk)
                    if not self.started:
                        self.sat._played.clear()
                        await self.sat._send(AudioStart(rate=chunk.rate, width=chunk.width,
                                                        channels=chunk.channels).event())
                        self.started = True
                        self.first_audio_at = time.monotonic()
                    await self.sat._send(chunk.event())
                    self.seconds += len(chunk.audio) / (chunk.rate * chunk.width * chunk.channels)
            except Exception as e:
                log.error("satellite: couldn't speak %r: %s", s[:60], e)
                if not self.sat.connected:
                    return

    async def close(self) -> bool:
        """Finish: wait for the audio to be sent and played. True if it was played."""
        self.q.put_nowait(None)
        await self._worker
        if not self.started or not self.sat.connected:
            return False
        await self.sat._send(AudioStop().event())   # (cortana-play says "quiet" when it really ends)
        played_by = (self.first_audio_at or time.monotonic()) + self.seconds + 10
        try:
            await asyncio.wait_for(self.sat._played.wait(), max(1.0, played_by - time.monotonic()))
            return self.sat.connected
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("satellite: no 'played' after %.1fs of audio", self.seconds)
            return False
