"""YouTube Music for both agents: find tracks, and serve them to whatever plays them.

Two libraries do the YouTube work: ytmusicapi finds things (catalogue search, and the
owner's playlists and liked songs when signed in), yt-dlp turns a track id into an audio
URL. Those URLs expire within hours and only work from this network address, so players
are never given one. They get a stable local address instead:

    <YTMUSIC_STREAM_BASE>/yt/stream/<video id>?s=<signature>

and this module resolves the id and relays the audio when the player fetches it. MPD on
Cortana's satellite and Vector's ffmpeg both just see an ordinary http audio file.

Signing in is optional. Without it the whole catalogue still plays (128 kbps); with it
the owner's playlists and liked songs work too, at Premium quality. The session is a
cookie file, <DATA_DIR>/ytmusic/cookies.txt, written by deploy/pi/ytmusic-login.py and
never kept in the repository. When Google ends that session the tools say so.
"""
import asyncio
import hashlib
import hmac
import logging
import random
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .settings import cfg as config

log = logging.getLogger(config.LOGGER_NAME)

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_TRACKS = 100
RESOLVE_MARGIN_S = 900       # stop using a resolved URL this long before it expires
UPSTREAM_RETRIES = 4         # a relay that loses YouTube mid-song picks up where it stopped
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/141.0.0.0 Safari/537.36")
EXPIRED = ("my YouTube Music sign-in has expired; John needs to run deploy/pi/ytmusic-login.py "
           "on the laptop. Songs from the general catalogue still play")
NOT_SIGNED_IN = ("I'm not signed in to YouTube Music, so playlists and liked songs aren't "
                 "available; songs, artists and albums from the catalogue are")


class YTMusicError(Exception):
    """Something to tell the person; str() is the sentence."""


class SignInExpired(YTMusicError):
    def __init__(self):
        super().__init__(EXPIRED)


# ----------------------------------------------------------------------------- files
def _dir() -> Path:
    return Path(config.DATA_DIR) / "ytmusic"


def _cookies() -> Path:
    return _dir() / "cookies.txt"


def signed_in() -> bool:
    return _cookies().is_file()


def available() -> bool:
    """YouTube Music can be offered: switched on, and both libraries are installed."""
    if not config.YTMUSIC_ENABLED:
        return False
    try:
        import yt_dlp  # noqa: F401
        import ytmusicapi  # noqa: F401
    except ImportError:
        return False
    return True


def cookie_header(path: Path) -> str:
    """The youtube.com cookies of a Netscape cookie file, as one Cookie header."""
    out = []
    for line in path.read_text().splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 7 and parts[0].lstrip(".").endswith("youtube.com"):
            out.append(f"{parts[5]}={parts[6]}")
    return "; ".join(out)


# ---------------------------------------------------------------------------- finding
_client_lock = threading.Lock()
_client_cache: tuple[float, object] | None = None


def _client():
    """A ytmusicapi client: signed in when the cookie file is there, anonymous otherwise.
    Rebuilt when the cookie file changes (a fresh sign-in needs no restart)."""
    global _client_cache
    from ytmusicapi import YTMusic
    stamp = _cookies().stat().st_mtime if signed_in() else 0.0
    with _client_lock:
        if _client_cache and _client_cache[0] == stamp:
            return _client_cache[1]
        if stamp:
            # ytmusicapi works the per-request authorization out of the cookies itself; the
            # header only has to be present for it to treat this as a browser session
            client = YTMusic(auth={
                "cookie": cookie_header(_cookies()), "x-goog-authuser": "0",
                "authorization": "SAPISIDHASH x", "origin": "https://music.youtube.com",
                "content-type": "application/json", "accept": "*/*", "user-agent": _UA})
        else:
            client = YTMusic()
        _client_cache = (stamp, client)
        return client


def _length(t: dict) -> int:
    if t.get("duration_seconds"):
        return int(t["duration_seconds"])
    secs = 0
    for part in str(t.get("length") or t.get("duration") or "").split(":"):
        secs = secs * 60 + int(part) if part.isdigit() else secs
    return secs


def _track(t: dict, album: str = "") -> dict | None:
    vid = t.get("videoId")
    if not vid or not VIDEO_ID.match(vid) or t.get("isAvailable") is False:
        return None
    alb = t.get("album")
    return {"id": vid, "title": t.get("title") or "?",
            "artist": ", ".join(a["name"] for a in t.get("artists") or [] if a.get("name")),
            "album": (alb.get("name") if isinstance(alb, dict) else alb) or album,
            "duration": _length(t)}


def _tracks(raw, album: str = "") -> list[dict]:
    return [t for t in (_track(r, album) for r in raw or []) if t]


def _words(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def _find(query: str, mode: str) -> tuple[list[dict], str]:
    y = _client()
    query = query.strip()
    if mode in ("playlist", "liked") or (mode == "shuffle" and not query):
        if not signed_in():
            return [], NOT_SIGNED_IN
        if mode == "playlist":
            lists = y.get_library_playlists(limit=None)
            q = _words(query)
            hits = [p for p in lists if q and q <= _words(p.get("title", ""))]
            hits.sort(key=lambda p: len(p.get("title", "")))
            if not hits:
                names = ", ".join(p["title"] for p in lists[:12] if p.get("title"))
                return [], f"no playlist called {query!r}. The playlists are: {names}"
            picks = _tracks(y.get_playlist(hits[0]["playlistId"], limit=MAX_TRACKS).get("tracks"))
            return picks, f"the playlist {hits[0]['title']}"
        picks = _tracks(y.get_liked_songs(limit=300).get("tracks"))
        random.shuffle(picks)
        return picks[:MAX_TRACKS], "a shuffle of your liked songs"
    if not query:
        return [], "say what to play"

    if mode == "artist":
        found = y.search(query, filter="artists", limit=3)
        if not found:
            return [], f"no artist called {query!r} on YouTube Music"
        artist = y.get_artist(found[0]["browseId"])
        songs = artist.get("songs") or {}
        picks = []
        if songs.get("browseId"):
            picks = _tracks(y.get_playlist(songs["browseId"], limit=60).get("tracks"))
        picks = picks or _tracks(songs.get("results"))
        random.shuffle(picks)
        return picks, f"songs by {artist.get('name') or found[0].get('artist') or query}"

    if mode == "album":
        found = y.search(query, filter="albums", limit=3)
        if not found:
            return [], f"no album matching {query!r} on YouTube Music"
        album = y.get_album(found[0]["browseId"])
        by = ", ".join(a["name"] for a in album.get("artists") or [] if a.get("name"))
        return (_tracks(album.get("tracks"), album.get("title", "")),
                f"the album {album.get('title', query)}" + (f" by {by}" if by else ""))

    found = _tracks(y.search(query, filter="songs", limit=5))
    if not found:
        return [], f"nothing on YouTube Music matches {query!r}"
    best = found[0]
    if mode == "shuffle":                      # that song, then music like it
        radio = _tracks(y.get_watch_playlist(videoId=best["id"], radio=True, limit=40).get("tracks"))
        rest = [t for t in radio if t["id"] != best["id"]]
        return [best, *rest], f"a mix starting from {best['title']} by {best['artist']}"
    return [best], f"{best['title']} by {best['artist']}"


def _session_alive() -> bool:
    try:
        return bool(_client().get_account_info().get("accountName"))
    except Exception:
        return False


async def search(query: str, mode: str) -> tuple[list[dict], str]:
    """mode: song | artist | album | shuffle | playlist | liked.
    Returns (tracks, description) like the NAS library search; [] means the description
    says why. Tracks are {id, title, artist, album, duration}."""
    def run():
        try:
            return _find(query, mode)
        except Exception as e:
            log.warning("ytmusic: %s search for %r failed: %s: %s", mode, query, type(e).__name__, e)
            if signed_in() and not _session_alive():
                return [], EXPIRED
            return [], "YouTube Music didn't answer; try again in a moment"
    picks, desc = await asyncio.to_thread(run)
    return picks[:MAX_TRACKS], desc


# --------------------------------------------------------------------------- resolving
_resolved: dict[str, tuple[float, str, dict]] = {}      # id -> (good until, url, headers)
_resolve_lock = asyncio.Lock()


def _working_cookies() -> str | None:
    """yt-dlp rewrites the cookie file it is given. It gets its own copy, refreshed when a
    new sign-in arrives, so the file the login script wrote is never touched."""
    if not signed_in():
        return None
    work = _dir() / "cookies.work.txt"
    if not work.exists() or work.stat().st_mtime < _cookies().stat().st_mtime:
        shutil.copyfile(_cookies(), work)
        work.chmod(0o600)
    return str(work)


def _extract(video_id: str) -> tuple[str, dict]:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "format": "bestaudio[ext=m4a]/bestaudio", "source_address": "0.0.0.0",   # IPv4: the URL is tied to it
            "cachedir": str(_dir() / "cache"), "socket_timeout": 15}
    cookies = _working_cookies()
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False)
    except DownloadError as e:
        text = str(e)
        if cookies and re.search(r"no longer valid|Sign in to confirm|account has been", text, re.I):
            raise SignInExpired() from e
        raise YTMusicError(f"YouTube would not give me that track ({text[:160]})") from e
    if not info or not info.get("url"):
        raise YTMusicError("YouTube returned no audio for that track")
    return info["url"], dict(info.get("http_headers") or {})


async def resolve(video_id: str, fresh: bool = False) -> tuple[str, dict]:
    """The audio URL for a track (and the headers to fetch it with), cached until shortly
    before YouTube's own expiry."""
    if not VIDEO_ID.match(video_id):
        raise YTMusicError("not a YouTube track id")
    async with _resolve_lock:                  # one yt-dlp at a time: they share the cookie copy
        hit = _resolved.get(video_id)
        if hit and not fresh and hit[0] > time.time():
            return hit[1], hit[2]
        url, headers = await asyncio.to_thread(_extract, video_id)
        expire = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("expire", [""])[0]
        until = (int(expire) if expire.isdigit() else time.time() + 3600) - RESOLVE_MARGIN_S
        _resolved[video_id] = (until, url, headers)
        for k in [k for k, v in _resolved.items() if v[0] <= time.time()]:
            del _resolved[k]
        return url, headers


def warm(video_id: str) -> None:
    """Resolve a track in the background as it is queued, so the player's first fetch a
    moment later is answered at once (yt-dlp takes a few seconds)."""
    async def run():
        try:
            await resolve(video_id)
        except Exception as e:
            log.warning("ytmusic: could not prepare %s: %s", video_id, e)
    asyncio.get_running_loop().create_task(run())


# ----------------------------------------------------------------------------- serving
def _key() -> bytes:
    path = _dir() / "stream.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def sign(video_id: str) -> str:
    return hmac.new(_key(), video_id.encode(), hashlib.sha256).hexdigest()[:20]


def stream_url(video_id: str) -> str:
    """The address a player fetches. Signed, so only tracks an agent queued can be fetched
    by other devices on the network."""
    return f"{config.YTMUSIC_STREAM_BASE}/yt/stream/{video_id}?s={sign(video_id)}"


router = APIRouter()
_PASS = ("content-type", "content-length", "content-range", "accept-ranges")


async def _open(client: httpx.AsyncClient, video_id: str, range_header: str | None) -> httpx.Response:
    """Start fetching the track from YouTube; a URL that has gone stale is resolved again once."""
    for attempt in (0, 1):
        url, headers = await resolve(video_id, fresh=bool(attempt))
        if range_header:
            headers = {**headers, "Range": range_header}
        up = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        if up.status_code in (200, 206):
            return up
        await up.aclose()
        if up.status_code not in (403, 404, 410):
            break
    raise YTMusicError(f"YouTube answered {up.status_code} for that track")


@router.api_route("/yt/stream/{video_id}", methods=["GET", "HEAD"])
async def stream(video_id: str, request: Request, s: str = ""):
    if not VIDEO_ID.match(video_id) or not hmac.compare_digest(s, sign(video_id)):
        raise HTTPException(403)
    client = httpx.AsyncClient(timeout=httpx.Timeout(20, read=60), follow_redirects=True)
    try:
        up = await _open(client, video_id, request.headers.get("range"))
    except YTMusicError as e:
        await client.aclose()
        log.warning("ytmusic: %s: %s", video_id, e)
        raise HTTPException(502, str(e))
    except Exception as e:
        await client.aclose()
        log.warning("ytmusic: %s: %s: %s", video_id, type(e).__name__, e)
        raise HTTPException(502, "could not reach YouTube")
    headers = {k: up.headers[k] for k in _PASS if k in up.headers}
    headers.setdefault("accept-ranges", "bytes")
    if request.method == "HEAD":
        await up.aclose()
        await client.aclose()
        return Response(status_code=up.status_code, headers=headers)

    # where this response starts in the file, and how many bytes it should carry
    m = re.match(r"bytes (\d+)-(\d+)/", up.headers.get("content-range", ""))
    start = int(m.group(1)) if m else 0
    total = int(up.headers.get("content-length", 0))

    async def body():
        nonlocal up
        sent, retries = 0, 0
        try:
            while True:
                try:
                    async for chunk in up.aiter_raw():
                        sent += len(chunk)
                        yield chunk
                    if not total or sent >= total:
                        return
                    reason = "ended early"
                except httpx.HTTPError as e:
                    reason = type(e).__name__
                # YouTube dropped the connection mid-song: carry on from the next byte
                await up.aclose()
                retries += 1
                if not total or retries > UPSTREAM_RETRIES:
                    log.warning("ytmusic: %s: stream %s at %d of %d bytes; giving up", video_id, reason, sent, total)
                    return
                up = await _open(client, video_id, f"bytes={start + sent}-{start + total - 1}")
        except YTMusicError as e:
            log.warning("ytmusic: %s: %s", video_id, e)
        finally:
            await up.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=up.status_code, headers=headers)


def stream_app() -> FastAPI:
    """Only the stream route, for a second listener that other devices may reach (the main
    door of an agent stays on 127.0.0.1)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    return app
