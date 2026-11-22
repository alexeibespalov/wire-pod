"""Audio player: anything ffmpeg can read -> Vector's speaker.

Verified on the robot (spike 2026-11-22): gRPC ExternalAudioStreamPlayback accepts
16 kHz mono 16-bit PCM WITHOUT behaviour control, so Vector keeps being himself while
music plays, and his built-in dance-to-the-beat can hear his own speaker.

* ffmpeg decodes files/URLs to 16 kHz mono s16le, high-passed (his tiny speaker can't
  do bass) and level-normalised; we read it paced to real time (<=0.5 s ahead).
* Runs on the face watcher's SDK connection (app/faces.py), in the SDK's event loop.
* Voice turns: playback is queued and starts after Vector has said his reply
  (server calls turn_ended() with an estimate). A2A turns start immediately.

Wake word while playing: Vector's firmware does NOT stream the microphone to wire-pod
while external audio is playing (commands arrive empty), so "Hey Vector" PAUSES the
music (faces.py forwards the wake-word event). After the voice turn, music resumes from
the same spot unless the turn stopped or replaced it; with no turn within
AUTO_RESUME_SECONDS (e.g. a false wake) it resumes on its own.
"""
import asyncio
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace

from . import config, faces
from . import robot as robot_http

log = logging.getLogger("vector-agent")
RATE = 16000
CHUNK = 1024  # bytes; the robot's maximum
AUTO_RESUME_SECONDS = 12
REWIND_SECONDS = 1.0  # on resume, replay a little of what the pause cut off


@dataclass
class Item:
    title: str
    source: str                 # local path or http(s) URL
    is_url: bool = False
    cleanup: str | None = None  # temp dir to delete when done
    offset: float = 0.0         # start position (seconds) - used when resuming


@dataclass
class _Job:
    items: list[Item]
    volume: int
    dance: bool
    label: str
    created: float = field(default_factory=time.time)


class Player:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: _Job | None = None
        self._paused: _Job | None = None
        self._task: asyncio.Task | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._turn_active = False
        self._auto_resume: asyncio.Task | None = None
        self.current: Item | None = None
        self.current_label = ""
        self.started_at = 0.0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ---------------------------------------------------------------- state
    def is_playing(self) -> bool:
        return self._task is not None and not self._task.done()

    def has_pending(self) -> bool:
        return self._pending is not None

    def is_paused(self) -> bool:
        return self._paused is not None

    def active(self) -> bool:
        return self.is_playing() or self.has_pending() or self.is_paused()

    def describe(self) -> str:
        if self.is_playing() and self.current:
            mins = (time.time() - self.started_at + self.current.offset) / 60
            return f"playing '{self.current.title}' ({self.current_label}), {mins:.0f} min in"
        if self._paused:
            return f"paused: '{self._paused.items[0].title}' ({self._paused.label})"
        if self._pending:
            return f"about to play {self._pending.label}"
        return "nothing is playing"

    # ---------------------------------------------------------------- control
    def queue(self, items: list[Item], volume: int, dance: bool, label: str) -> None:
        """Replace whatever was queued (and any paused music); starts after the turn."""
        for j in (self._pending, self._paused):
            if j:
                _cleanup(j.items)
        self._paused = None
        self._pending = _Job(items[: config.MAX_QUEUE], max(10, min(volume, 100)), dance, label)

    def turn_started(self) -> None:
        self._turn_active = True
        if self._auto_resume and not self._auto_resume.done():
            self._auto_resume.cancel()

    def turn_ended(self, delay: float) -> None:
        """Called after every voice/A2A turn: start new music, or resume paused music."""
        self._turn_active = False
        if self._pending:
            self._start(delay)
        elif self._paused:
            self._pending, self._paused = self._paused, None
            log.info("audio: resuming after the conversation")
            self._start(delay)

    async def stop(self) -> bool:
        was = self.active()
        for j in (self._pending, self._paused):
            if j:
                _cleanup(j.items)
        self._pending = self._paused = None
        self._stop.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=4)
            except Exception:
                self._task.cancel()
        self.current = None
        return was

    def on_wake_word(self) -> None:
        """Called from the SDK thread when Vector hears "Hey Vector"."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._handle_wake)

    def _handle_wake(self) -> None:
        if not self.is_playing():
            return
        log.info("audio: wake word heard - pausing so he can listen")
        self._pause.set()

        async def auto_resume():
            await asyncio.sleep(AUTO_RESUME_SECONDS)
            if self._paused and not self._turn_active:
                log.info("audio: no command after wake word - resuming")
                self._pending, self._paused = self._paused, None
                self._start(0)

        self._auto_resume = self._loop.create_task(auto_resume())

    def _start(self, delay: float) -> None:
        job, self._pending = self._pending, None
        old = self._task

        async def runner():
            if old and not old.done():
                self._stop.set()
                try:
                    await asyncio.wait_for(asyncio.shield(old), timeout=5)
                except Exception:
                    pass
            await asyncio.sleep(max(0.0, delay))
            self._stop.clear()
            self._pause.clear()
            await self._run(job)

        loop = self._loop or asyncio.get_running_loop()
        self._task = loop.create_task(runner())
        log.info("audio: %s starts in %.1fs", job.label, delay)

    # ---------------------------------------------------------------- playback
    async def _run(self, job: _Job) -> None:
        r = faces.get_robot()
        if r is None:
            log.warning("audio: no SDK connection to Vector, can't play %s", job.label)
            _cleanup(job.items)
            return
        keep_files = False
        try:
            for i, item in enumerate(job.items):
                if self._stop.is_set():
                    break
                self.current, self.current_label = item, job.label
                self.started_at = time.time()
                log.info("audio: now playing %r (%d/%d) from %.0fs", item.title, i + 1,
                         len(job.items), item.offset)
                if job.dance and i == 0 and item.offset == 0:
                    asyncio.get_running_loop().create_task(self._start_dance())
                fut = r.conn.run_coroutine(self._stream(r, item, job.volume))
                try:
                    played = await asyncio.wrap_future(fut)
                except Exception as e:
                    log.warning("audio: %r failed: %s", item.title, e)
                    played = 0.0
                if self._pause.is_set() and not self._stop.is_set():
                    pos = max(0.0, item.offset + played - REWIND_SECONDS)
                    rest = [replace(item, offset=pos)] + job.items[i + 1:]
                    self._paused = _Job(rest, job.volume, False, job.label)
                    keep_files = True
                    log.info("audio: paused %r at %.0fs", item.title, pos)
                    break
        finally:
            self.current = None
            if not keep_files:
                _cleanup(job.items)

    async def _start_dance(self) -> None:
        await asyncio.sleep(3)  # let the beat get going; his routine listens 10-15 s
        if self.is_playing() and not self._stop.is_set():
            try:
                await robot_http.sdk("cloud_intent", intent="intent_imperative_dance")
                log.info("audio: dance-to-the-beat started")
            except Exception as e:
                log.warning("audio: couldn't start dancing: %s", e)

    async def _stream(self, r, item: Item, volume: int) -> float:
        """Runs in the SDK's event loop. Returns seconds of audio sent."""
        from anki_vector.messaging import protocol
        proc = subprocess.Popen(_ffmpeg_cmd(item), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        loop = asyncio.get_running_loop()
        stop, pause = self._stop, self._pause
        max_samples = config.MAX_PLAY_MINUTES * 60 * RATE
        sent = 0

        async def requests():
            nonlocal sent
            yield protocol.ExternalAudioStreamRequest(
                audio_stream_prepare=protocol.ExternalAudioStreamPrepare(
                    audio_frame_rate=RATE, audio_volume=volume))
            t0 = time.time()
            while not stop.is_set() and not pause.is_set() and sent < max_samples:
                chunk = await loop.run_in_executor(None, proc.stdout.read, CHUNK)
                if not chunk:
                    break
                if len(chunk) % 2:
                    chunk = chunk[:-1]
                yield protocol.ExternalAudioStreamRequest(
                    audio_stream_chunk=protocol.ExternalAudioStreamChunk(
                        audio_chunk_size_bytes=len(chunk), audio_chunk_samples=chunk))
                sent += len(chunk) // 2
                ahead = sent / RATE - (time.time() - t0)
                if ahead > 0.5:
                    await asyncio.sleep(ahead - 0.5)
            yield protocol.ExternalAudioStreamRequest(
                audio_stream_complete=protocol.ExternalAudioStreamComplete())

        try:
            async for _ in r.conn.grpc_interface.ExternalAudioStreamPlayback(requests()):
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
        return sent / RATE


def _ffmpeg_cmd(item: Item) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if item.is_url:
        cmd += ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-rw_timeout", "15000000",
                "-reconnect", "1", "-reconnect_streamed", "1"]
    if item.offset > 0:
        cmd += ["-ss", f"{item.offset:.1f}"]
    cmd += ["-i", item.source, "-vn", "-ac", "1", "-ar", str(RATE),
            "-af", "highpass=f=150,dynaudnorm=f=200:g=15,volume=0.9",
            "-t", str(config.MAX_PLAY_MINUTES * 60), "-f", "s16le", "pipe:1"]
    return cmd


def _cleanup(items: list[Item]) -> None:
    for it in items:
        if it.cleanup:
            shutil.rmtree(it.cleanup, ignore_errors=True)


PLAYER = Player()
