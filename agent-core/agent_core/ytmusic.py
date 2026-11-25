"""YouTube Music for both agents: find tracks, and serve them to whatever plays them.

Two libraries do the YouTube work: ytmusicapi finds things (catalogue search, and the
owner's playlists and liked songs when signed in), yt-dlp turns a track id into an audio
URL. Those URLs expire within hours and only work from this network address, so players
are never given one. They get a stable local address instead:

    <YTMUSIC_STREAM_BASE>/yt/stream/<video id>?s=<signature>

and this module resolves the id and relays the audio when the player fetches it. MPD on
Cortana's satellite and Vector's ffmpeg both just see an ordinary http audio file.

Signing in is optional. Without it the whole catalogue still plays (128 kbps); with it
the owner's playlists and liked songs work too, at Premium quality. The session is a set
of cookies handed over by deploy/pi/ytmusic-login.py and kept encrypted in
<DATA_DIR>/ytmusic/cookies.enc; the key is YTMUSIC_KEY, which lives with the agent's other
secrets in its env file, not in the data folder. They are only ever decrypted in memory:
yt-dlp reads them from a text stream, not a file. When Google ends that session the
tools say so, and a daily check (`watch`) records how long each sign-in lasted in
<DATA_DIR>/ytmusic/session.log.
"""
import asyncio
import hashlib
import hmac
import io
import json
import logging
import random
import re
import secrets
import sys
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


def _store() -> Path:
    return _dir() / "cookies.enc"


def _fernet():
    from cryptography.fernet import Fernet
    if not config.YTMUSIC_KEY:
        raise YTMusicError("YTMUSIC_KEY is not set, so the YouTube Music sign-in cannot be kept")
    return Fernet(config.YTMUSIC_KEY.encode())


def signed_in() -> bool:
    return bool(config.YTMUSIC_KEY) and _store().is_file()


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


_jar: tuple[float, str] | None = None        # (the stored file's mtime, the cookies as yt-dlp last left them)


def store_cookies(text: str) -> None:
    """Keep a new sign-in (Netscape cookie text), encrypted. Replaces the previous one."""
    global _jar
    if "\t" not in text:
        raise YTMusicError("that is not a cookie file")
    token = _fernet().encrypt(text.encode())
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _store().with_suffix(".new")
    tmp.write_bytes(token)
    tmp.chmod(0o600)
    tmp.replace(_store())
    _jar = None
    for old in ("cookies.txt", "cookies.work.txt"):      # from before they were encrypted
        (_dir() / old).unlink(missing_ok=True)


def _cookie_text() -> str:
    """The cookies, decrypted in memory. yt-dlp's later changes to them are kept for as
    long as the process runs; a new sign-in replaces them."""
    global _jar
    from cryptography.fernet import InvalidToken
    stamp = _store().stat().st_mtime
    if _jar is None or _jar[0] != stamp:
        try:
            _jar = (stamp, _fernet().decrypt(_store().read_bytes()).decode())
        except InvalidToken:
            raise YTMusicError("the stored YouTube Music sign-in cannot be read with this YTMUSIC_KEY; "
                               "run deploy/pi/ytmusic-login.py again") from None
    return _jar[1]


def cookie_header(text: str) -> str:
    """The youtube.com cookies of Netscape cookie text, as one Cookie header."""
    out = []
    for line in text.splitlines():
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
    """A ytmusicapi client: signed in when there is a stored sign-in, anonymous otherwise.
    Rebuilt when the sign-in is replaced (a fresh one needs no restart)."""
    global _client_cache
    from ytmusicapi import YTMusic
    stamp = _store().stat().st_mtime if signed_in() else 0.0
    with _client_lock:
        if _client_cache and _client_cache[0] == stamp:
            return _client_cache[1]
        if stamp:
            # ytmusicapi works the per-request authorization out of the cookies itself; the
            # header only has to be present for it to treat this as a browser session
            client = YTMusic(auth={
                "cookie": cookie_header(_cookie_text()), "x-goog-authuser": "0",
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


def session_state() -> bool | None:
    """True = the sign-in works, False = Google no longer accepts it, None = could not tell
    (not signed in, or YouTube could not be reached)."""
    import requests
    if not signed_in():
        return None
    try:
        return bool(_client().get_account_info().get("accountName"))
    except (requests.ConnectionError, requests.Timeout):
        return None
    except Exception:
        return False


def _session_alive() -> bool:
    return session_state() is True


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


def _extract(video_id: str) -> tuple[str, dict]:
    global _jar
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "format": "bestaudio[ext=m4a]/bestaudio", "source_address": "0.0.0.0",   # IPv4: the URL is tied to it
            "cachedir": str(_dir() / "cache"), "socket_timeout": 15}
    cookies = None
    if signed_in():
        # a text stream, not a file: yt-dlp reads the cookies from it and writes them back
        # to it when it closes, so they never touch the disk unencrypted
        cookies = io.StringIO(_cookie_text())
        opts["cookiefile"] = cookies
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False)
    except DownloadError as e:
        text = str(e)
        if cookies and re.search(r"no longer valid|Sign in to confirm|account has been", text, re.I):
            raise SignInExpired() from e
        raise YTMusicError(f"YouTube would not give me that track ({text[:160]})") from e
    if cookies and _jar:
        left = cookies.getvalue().lstrip("\x00")           # it truncates the stream without rewinding
        if "\t" in left:
            _jar = (_jar[0], left)
    if not info or not info.get("url"):
        raise YTMusicError("YouTube returned no audio for that track")
    return info["url"], dict(info.get("http_headers") or {})


async def resolve(video_id: str, fresh: bool = False) -> tuple[str, dict]:
    """The audio URL for a track (and the headers to fetch it with), cached until shortly
    before YouTube's own expiry."""
    if not VIDEO_ID.match(video_id):
        raise YTMusicError("not a YouTube track id")
    async with _resolve_lock:                  # one yt-dlp at a time: they share the cookies
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


# ------------------------------------------------------------------- watching the sign-in
CHECK_EVERY_S = 24 * 3600


def check_session() -> dict:
    """Look once, and add a line to session.log: when, whether the sign-in works, and how
    many days it has lasted. Reading that file later shows how long a sign-in really lives."""
    state = session_state()
    since = _store().stat().st_mtime if signed_in() else None
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "state": {True: "alive", False: "expired", None: "unknown" if since else "not signed in"}[state],
             "signed_in_on": time.strftime("%Y-%m-%d", time.localtime(since)) if since else None,
             "days": round((time.time() - since) / 86400, 1) if since else None}
    _dir().mkdir(parents=True, exist_ok=True)
    with (_dir() / "session.log").open("a") as f:
        f.write(json.dumps(entry) + "\n")
    if state is False:
        log.warning("ytmusic: the sign-in from %s has expired after %s days: run deploy/pi/ytmusic-login.py",
                    entry["signed_in_on"], entry["days"])
    else:
        log.info("ytmusic: sign-in %s (signed in on %s, %s days ago)", entry["state"],
                 entry["signed_in_on"], entry["days"])
    return entry


async def watch() -> None:
    """Check the sign-in a minute after start and then once a day."""
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(check_session)
        except Exception as e:
            log.warning("ytmusic: sign-in check failed: %s", e)
        await asyncio.sleep(CHECK_EVERY_S)


_watching: asyncio.Task | None = None      # held here so the loop keeps it alive


def start_watch() -> asyncio.Task | None:
    global _watching
    if available():
        _watching = asyncio.get_running_loop().create_task(watch())
    return _watching


def cli() -> None:
    """For deploy/pi/ytmusic-login.py, run inside the agent's container after its config is
    loaded:  store = keep the cookie text on stdin as the new sign-in, then check it;
    check = say whether the stored sign-in works."""
    what = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:
        if what == "store":
            store_cookies(sys.stdin.read())
        entry = check_session()
    except YTMusicError as e:
        sys.exit(f"{config.ASSISTANT_NAME}: {e}")
    print(f"{config.ASSISTANT_NAME}: sign-in {entry['state']}"
          + (f" (signed in on {entry['signed_in_on']})" if entry["signed_in_on"] else ""))
    if what == "store" and entry["state"] == "expired":
        sys.exit(1)

