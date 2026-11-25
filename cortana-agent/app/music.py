"""Music from the family library on the NAS, played on Cortana's speaker (the Echo).

MPD runs on the Pi Zero (deploy/pizero/music.sh): it reads the NAS share and plays the
files as they are, straight to the Bluetooth speaker. Nothing is copied or converted
ahead of time. We just tell it what to queue, over its plain-text protocol on :6600.

The speaker takes one stream at a time. The satellite's speaker command
(/usr/local/bin/cortana-play) pauses MPD before any chime or reply, and the watcher here
starts or resumes the music once the conversation is over (as Vector does):

    play_music(...)  -> queue now, start when she's finished talking
    wake word        -> paused (by cortana-play, and by us on `detection`)
    quiet again      -> resumed
    paused/stopped with nobody talking (the queue ended, someone pressed pause) -> leave it

Matching is Vector's (vector-agent/app/library.py): the library is laid out
<Artist>/<Album>/<track>, and every query word must match.

What the NAS doesn't have comes from YouTube Music (agent_core/ytmusic.py): those tracks
are queued as http addresses on this agent, which MPD plays like any other stream, with
the title and artist attached as tags since a stream carries none.
"""
import asyncio
import contextlib
import difflib
import logging
import random
import re
import time
import unicodedata
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from . import config
from agent_core import ytmusic

log = logging.getLogger("cortana-agent")
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"}
QUIET_BEFORE_RESUME_S = 2.5   # this long after the last voice activity, the music comes back
OURS_WINDOW_S = 6.0           # a pause this close to voice activity was for her, not a person


class MPDError(Exception):
    pass


# ------------------------------------------------------------------------- protocol
def quote(arg: str) -> str:
    return '"' + str(arg).replace("\\", "\\\\").replace('"', '\\"') + '"'


async def command(*lines: str, timeout: float = 15.0) -> list[tuple[str, str]]:
    """Run MPD commands (several = one command list) and return the key/value lines."""
    async def run():
        reader, writer = await asyncio.open_connection(config.MPD_HOST, config.MPD_PORT, limit=1 << 20)
        try:
            hello = (await reader.readline()).decode()
            if not hello.startswith("OK MPD"):
                raise MPDError(f"not MPD: {hello.strip()!r}")
            body = "\n".join(lines) if len(lines) == 1 else \
                "\n".join(["command_list_begin", *lines, "command_list_end"])
            writer.write((body + "\n").encode())
            await writer.drain()
            out = []
            while True:
                line = (await reader.readline()).decode("utf-8", "replace").rstrip("\n")
                if line == "OK":
                    return out
                if line.startswith("ACK"):
                    raise MPDError(line)
                if not line:
                    raise MPDError("MPD closed the connection")
                k, _, v = line.partition(": ")
                out.append((k, v))
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    return await asyncio.wait_for(run(), timeout)


async def status() -> dict:
    return dict(await command("status"))


def tell_speaker(what: str) -> None:
    """For the Echo's screen (cortana-nowplaying on the Zero, over MPD's `cortana` channel):
    "say <sentence>" as she starts a sentence (cortana-play on the Zero sends "busy" and
    "quiet" around the audio itself, as it really plays). Fire and forget."""
    if not config.MPD_HOST:
        return

    async def send():
        with contextlib.suppress(Exception):
            await command(f"sendmessage cortana {quote(what[:200])}", timeout=3)
    asyncio.get_running_loop().create_task(send())


# --------------------------------------------------------------------------- library
_tracks: list[dict] = []
_db_update = ""
_checked = 0.0


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\.[a-z0-9]{2,4}$", "", s.lower())
    s = re.sub(r"^\d{1,3}[\s._-]+", "", s)           # leading track numbers
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tracks_from_listing(pairs: list[tuple[str, str]]) -> list[dict]:
    out = []
    for k, v in pairs:
        if k != "file" or "." not in v or "." + v.rsplit(".", 1)[1].lower() not in AUDIO_EXT:
            continue
        parts = v.split("/")
        out.append({"path": v,
                    "artist": parts[0] if len(parts) >= 2 else "",
                    "album": parts[1] if len(parts) >= 3 else "",
                    "title": re.sub(r"\.[A-Za-z0-9]{2,4}$", "", parts[-1])})
    return out


async def library() -> list[dict]:
    """The track list, re-read from MPD whenever its database changes (checked ≤ 1/min)."""
    global _tracks, _db_update, _checked
    if _tracks and time.monotonic() - _checked < 60:
        return _tracks
    stats = dict(await command("stats"))
    _checked = time.monotonic()
    if stats.get("db_update") != _db_update or not _tracks:
        _tracks = tracks_from_listing(await command("listall", timeout=60))
        _db_update = stats.get("db_update", "")
        log.info("music: %d tracks in the library", len(_tracks))
    return _tracks


def _words(s: str) -> set[str]:
    return set(_norm(s).split())


def _all_in(q: list[str], words: set[str]) -> bool:
    return all(w in words for w in q)


def _closest(phrase: str, names: set[str], n: int = 3) -> list[str]:
    by_norm = {_norm(x): x for x in names if x}
    return [by_norm[m] for m in difflib.get_close_matches(phrase, list(by_norm), n=n, cutoff=0.72)]


def search(tracks: list[dict], query: str, mode: str) -> tuple[list[dict], str]:
    """mode: song | artist | album | shuffle. Returns (tracks, description); [] = not found.
    Every query word must match (no 'punk' -> NOFX); fuzzy only as a fallback."""
    if not tracks:
        return [], "the music library is empty (still being scanned?)"
    phrase = _norm(query)
    q = [w for w in phrase.split() if len(w) > 1]
    if not q:
        if mode == "shuffle":
            return random.sample(tracks, min(len(tracks), 60)), "a random shuffle of the whole library"
        return [], "say what to play"
    artists = {t["artist"] for t in tracks}

    if mode == "artist":
        hits = sorted((a for a in artists if _all_in(q, _words(a))), key=len)
        if not hits:
            hits = _closest(phrase, artists, 1)
        if not hits:
            near = _closest(phrase, artists, 3)
            return [], (f"no artist called {query!r} in the music library"
                        + (f" (closest: {', '.join(near)})" if near else ""))
        picks = [t for t in tracks if t["artist"] == hits[0]]
        random.shuffle(picks)
        return picks, f"songs by {hits[0]}"

    if mode == "album":
        albums = {(t["artist"], t["album"]) for t in tracks if t["album"]}
        hits = [k for k in albums if _all_in(q, _words(k[1]) | _words(k[0]))]
        hits.sort(key=lambda k: (-_all_in(q, _words(k[1])), len(k[1])))
        if not hits:
            near = _closest(phrase, {a for _, a in albums}, 1)
            hits = [k for k in albums if near and k[1] == near[0]]
        if not hits:
            return [], f"no album matching {query!r} in the music library"
        key = hits[0]
        picks = sorted((t for t in tracks if (t["artist"], t["album"]) == key), key=lambda t: t["path"])
        return picks, f"the album {key[1]} by {key[0]}"

    hits = [t for t in tracks if _all_in(q, _words(t["title"]) | _words(t["artist"]) | _words(t["album"]))]
    if not hits:
        return [], f"nothing in the music library matches {query!r}"
    if mode == "shuffle":
        random.shuffle(hits)
        return hits[:60], f"a shuffle of music matching {query!r}"
    hits.sort(key=lambda t: (-(phrase in _norm(t["title"])), -_all_in(q, _words(t["title"])), len(t["title"])))
    best = hits[0]
    return [best], f"{best['title']} by {best['artist']}"


# ---------------------------------------------------------------- playing around her voice
class Player:
    """Keeps the music out of the way of the conversation and brings it back after."""

    def __init__(self):
        self.want = False             # music should be playing when she's quiet
        self.last_voice = 0.0         # monotonic time of the last satellite activity
        self.idle = lambda: True      # server wires in: the satellite isn't listening/talking
        self._last_state = ""

    def touch(self) -> None:
        self.last_voice = time.monotonic()

    def quiet(self) -> bool:
        return time.monotonic() - self.last_voice >= QUIET_BEFORE_RESUME_S and self.idle()

    async def duck(self) -> None:
        """Someone said the wake word: pause now (cortana-play does too; this covers a
        satellite without chimes and gives Whisper a quiet room)."""
        self.touch()
        if self.want:
            with contextlib.suppress(Exception):
                if (await status()).get("state") == "play":
                    await command("pause 1")

    async def step(self) -> None:
        state = (await status()).get("state", "")
        if not self.want:
            if state == "play" and self.quiet():
                # started from somewhere else (the Echo's play button): look after it too
                self.want = True
                log.info("music: started from outside; resuming it after conversations too")
            self._last_state = state
            return
        if state != "play" and self._last_state == "play" \
                and time.monotonic() - self.last_voice > OURS_WINDOW_S:
            # it stopped with nobody talking: the queue ran out, or a person paused it
            log.info("music: %s by someone else / end of queue; not resuming", state)
            self.want = False
        elif state != "play" and self.quiet():
            await command("play")
            state = "play"
            log.info("music: playing")
        self._last_state = state

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception as e:
                log.warning("music: %s", e)
                await asyncio.sleep(10)
            await asyncio.sleep(1.0)


PLAYER = Player()


def start(idle) -> asyncio.Task:
    PLAYER.idle = idle
    return asyncio.get_running_loop().create_task(PLAYER.run())


# A bare "stop" while music plays means the music (it would otherwise be a "never mind").
_STOP = re.compile(r"^\W*(?:stop|stop it|pause|quiet|be quiet|shush|shh+|enough|that's enough|"
                   r"shut up|turn it off|off)\W*$", re.I)


def is_stop_request(text: str) -> bool:
    return PLAYER.want and bool(_STOP.match(text.strip()))


async def stop_for_voice() -> None:
    """They said "stop" over the music: pause it (so "carry on" can resume) and keep it paused."""
    PLAYER.want = False
    await command("pause 1")
    log.info("music: paused by voice")


# ------------------------------------------------------------------------------ tools
_AFTER = ("It starts by itself as soon as you've finished talking: say one short sentence "
          "and stop.")


_NAS_MODES = ("song", "artist", "album", "shuffle")


async def _queue(cmds: list[str], volume: int | None) -> None:
    if volume is not None:
        cmds.append(f"setvol {int(volume)}")
    await command(*cmds)
    PLAYER.want = True
    PLAYER._last_state = ""


async def _queue_youtube(picks: list[dict], volume: int | None) -> None:
    """Queue stream addresses, then name them: `addid` answers with the new song's id."""
    ytmusic.warm(picks[0]["id"])
    await command("stop", "clear")
    ids = await command(*(f"addid {quote(ytmusic.stream_url(t['id']))}" for t in picks))
    tags = []
    for (_, song_id), t in zip(ids, picks):
        tags.append(f"addtagid {song_id} Title {quote(t['title'])}")
        if t["artist"]:
            tags.append(f"addtagid {song_id} Artist {quote(t['artist'])}")
        if t["album"]:
            tags.append(f"addtagid {song_id} Album {quote(t['album'])}")
    await _queue(tags, volume)


@tool
async def play_music(
        query: Annotated[str, Field(description="Song, artist, album or playlist words to find; "
                                                "empty with mode=shuffle or mode=liked")] = "",
        mode: Literal["song", "artist", "album", "shuffle", "playlist", "liked"] = "song",
        source: Annotated[Literal["auto", "nas", "youtube"], Field(
            description="auto = the family's library first, YouTube Music if it isn't there; "
                        "nas or youtube = only that one")] = "auto",
        volume: Annotated[int | None, Field(ge=5, le=100, description="Music volume 5-100; "
                                            "leave out to keep the current one")] = None) -> str:
    """Play music: from the family's library on the NAS, or from YouTube Music (any song,
    and John's playlists and liked songs). Load the 'music' skill first."""
    youtube = ytmusic.available()
    nas_desc = ""
    if source != "youtube" and mode in _NAS_MODES:
        picks, nas_desc = search(await library(), query, mode)
        if picks:
            await _queue(["stop", "clear", *(f"add {quote(t['path'])}" for t in picks[:200])], volume)
            more = f" ({min(len(picks), 200)} tracks queued)" if len(picks) > 1 else ""
            return f"queued {nas_desc}{more}, from the family library. {_AFTER}"
    if source == "nas" or not youtube:
        if mode not in _NAS_MODES:
            return "error: playlists and liked songs come from YouTube Music, which is not set up"
        return f"error: {nas_desc}"
    picks, desc = await ytmusic.search(query, mode)
    if not picks:
        return f"error: {desc}" if not nas_desc else f"error: {nas_desc}; and on YouTube Music: {desc}"
    await _queue_youtube(picks, volume)
    more = f" ({len(picks)} tracks queued)" if len(picks) > 1 else ""
    return f"queued {desc}{more}, from YouTube Music. {_AFTER}"


@tool
async def music_control(
        action: Literal["pause", "resume", "stop", "next", "previous", "volume"],
        level: Annotated[int | None, Field(ge=0, le=100, description="For action=volume: 0-100")] = None) -> str:
    """Pause, resume, stop or skip the music, or set its volume. Load the 'music' skill first."""
    st = await status()
    if action in ("pause", "stop"):
        PLAYER.want = False
        await command("pause 1" if action == "pause" else "stop")
        return "music paused" if action == "pause" else "music stopped"
    if action == "resume":
        if st.get("playlistlength", "0") == "0":
            return "error: nothing queued; use play_music"
        PLAYER.want, PLAYER._last_state = True, ""
        return f"resuming. {_AFTER}"
    if action in ("next", "previous"):
        if st.get("playlistlength", "0") == "0":
            return "error: nothing is queued"
        await command(action)
        PLAYER.want, PLAYER._last_state = True, ""
        # MPD starts playing on next/previous: hold it until she's finished talking
        await command("pause 1")
        return f"skipped. {_AFTER}"
    if level is None:
        return f"the music volume is {st.get('volume', '?')}; give level=0-100"
    await command(f"setvol {int(level)}")
    return f"music volume set to {int(level)}"


@tool
async def now_playing() -> str:
    """What song is playing (or paused), and the music volume. Load the 'music' skill first."""
    st = await status()
    song = dict(await command("currentsong"))
    if not song:
        return "nothing is playing"
    t = tracks_from_listing([("file", song.get("file", ""))])
    title = song.get("Title") or (t[0]["title"] if t else song.get("file", "?"))
    artist = song.get("Artist") or (t[0]["artist"] if t else "")
    state = {"play": "playing", "pause": "paused", "stop": "stopped"}.get(st.get("state", ""), "?")
    pos = f"track {int(st.get('song', 0)) + 1} of {st.get('playlistlength', '?')}"
    return f"{state}: {title}{' by ' + artist if artist else ''} ({pos}, volume {st.get('volume', '?')})"


MUSIC_TOOLS = [play_music, music_control, now_playing]
