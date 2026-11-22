"""Music library on the OMV NAS (read-only bind mount of the Pi's CIFS share).

Layout: <MUSIC_DIR>/<Artist>/<Album>/<track>. Indexed once (cached in the data volume,
refreshed daily) because walking 7k+ files over SMB takes a while.
"""
import difflib
import json
import logging
import os
import random
import re
import threading
import time
import unicodedata
from pathlib import Path

from . import config

log = logging.getLogger("vector-agent")
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"}
_INDEX_FILE = config.DATA_DIR / "music_index.json"
_lock = threading.Lock()
_tracks: list[dict] = []
_state = "not indexed"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\.[a-z0-9]{2,4}$", "", s.lower())
    s = re.sub(r"^\d{1,3}[\s._-]+", "", s)           # leading track numbers
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _scan() -> list[dict]:
    root = Path(config.MUSIC_DIR)
    out = []
    for dirpath, _dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root).parts
        for f in files:
            if Path(f).suffix.lower() in AUDIO_EXT:
                artist = rel[0] if len(rel) >= 1 else ""
                album = rel[1] if len(rel) >= 2 else ""
                out.append({"path": str(Path(dirpath) / f), "artist": artist, "album": album,
                            "title": re.sub(r"\.[A-Za-z0-9]{2,4}$", "", f)})
    return out


def _index_forever() -> None:
    global _tracks, _state
    try:
        cached = json.loads(_INDEX_FILE.read_text())
        with _lock:
            _tracks = cached["tracks"]
            _state = f"ready ({len(_tracks)} tracks, cached)"
        if time.time() - cached.get("built", 0) < 86400:
            log.info("music: loaded cached index, %d tracks", len(_tracks))
            time.sleep(86400 - (time.time() - cached.get("built", 0)))
    except (FileNotFoundError, ValueError, KeyError):
        pass
    while True:
        try:
            if not Path(config.MUSIC_DIR).is_dir():
                raise FileNotFoundError(config.MUSIC_DIR)
            t0 = time.time()
            with _lock:
                if not _tracks:
                    _state = "indexing"
            tracks = _scan()
            with _lock:
                _tracks, _state = tracks, f"ready ({len(tracks)} tracks)"
            _INDEX_FILE.write_text(json.dumps({"built": time.time(), "tracks": tracks}))
            log.info("music: indexed %d tracks in %.0fs", len(tracks), time.time() - t0)
            time.sleep(86400)
        except Exception as e:
            with _lock:
                if not _tracks:
                    _state = f"unavailable ({type(e).__name__})"
            log.warning("music: indexing failed (%s); retrying in 10 min", e)
            time.sleep(600)


def start() -> None:
    threading.Thread(target=_index_forever, name="music-indexer", daemon=True).start()


def status() -> str:
    return _state


def _words(s: str) -> set[str]:
    return set(_norm(s).split())


def _all_in(q: list[str], words: set[str]) -> bool:
    return all(w in words for w in q)


def _closest(phrase: str, names: set[str], n: int = 3) -> list[str]:
    by_norm = {_norm(x): x for x in names if x}
    return [by_norm[m] for m in difflib.get_close_matches(phrase, list(by_norm), n=n, cutoff=0.72)]


def search(query: str, mode: str) -> tuple[list[dict], str]:
    """mode: song | artist | album | shuffle. Returns (tracks, description).
    Every query word must match (no single-word false hits like 'punk' -> NOFX);
    fuzzy matching only as a fallback for mishearings. Empty list = not found."""
    with _lock:
        tracks = list(_tracks)
    if not tracks:
        return [], f"the music library is {_state}"
    phrase = _norm(query)
    q = [w for w in phrase.split() if len(w) > 1]
    if not q:
        if mode == "shuffle":
            return random.sample(tracks, min(len(tracks), 40)), "a random shuffle of the whole library"
        return [], "say what to play"
    artists = {t["artist"] for t in tracks}

    if mode == "artist":
        hits = sorted((a for a in artists if _all_in(q, _words(a))), key=lambda a: len(a))
        if not hits:
            hits = _closest(phrase, artists, 1)
        if not hits:
            near = _closest(phrase, artists, 3) or []
            return [], (f"no artist called {query!r} in the music library"
                        + (f" (closest: {', '.join(near)})" if near else ""))
        artist = hits[0]
        picks = [t for t in tracks if t["artist"] == artist]
        random.shuffle(picks)
        return picks, f"songs by {artist}"

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

    # song / shuffle: all words across title + artist + album, title matches first
    hits = [t for t in tracks if _all_in(q, _words(t["title"]) | _words(t["artist"]) | _words(t["album"]))]
    if not hits:
        return [], f"nothing in the music library matches {query!r}"
    if mode == "shuffle":
        random.shuffle(hits)
        return hits[:40], f"a shuffle of music matching {query!r}"
    hits.sort(key=lambda t: (-(phrase in _norm(t["title"])), -_all_in(q, _words(t["title"])), len(t["title"])))
    best = hits[0]
    return [best], f"{best['title']} by {best['artist']}"
