"""MIDI from midiworld.com: search, download, validate, render to audio.

Downloaded files are untrusted, so:
* only https://www.midiworld.com URLs (the final URL after redirects is checked too)
* max 512 KB, must start with the MIDI header "MThd"
* parsed with mido first; rejected if malformed or longer than MAX_MIDI_MINUTES
* rendered by fluidsynth (no shell, fixed argv, time-limited) in a private temp dir
  that is deleted after playback; runs as the container's non-root user
"""
import asyncio
import html
import re
import shutil
import tempfile
import urllib.parse
from pathlib import Path

import httpx
import mido

from . import config

HOST = "www.midiworld.com"
MAX_BYTES = 512 * 1024
_http = httpx.AsyncClient(timeout=15, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (vector-agent home robot)"})
_ITEM = re.compile(r'<li>\s*(.*?)\s*-\s*<a href="(https://www\.midiworld\.com/download/\d+)"', re.S)


async def search(query: str) -> list[dict]:
    q = re.sub(r"[^\w\s'-]", " ", query)[:80].strip()
    r = await _http.get(f"https://{HOST}/search/", params={"q": q})
    r.raise_for_status()
    out = []
    for title, url in _ITEM.findall(r.text):
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        if title:
            out.append({"title": title, "url": url})
    return out[:30]


def best(results: list[dict], query: str) -> dict | None:
    if not results:
        return None
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    return max(results, key=lambda x: sum(w in x["title"].lower() for w in words))


async def fetch_and_render(url: str) -> tuple[Path, str]:
    """Returns (wav_path, temp_dir). Caller deletes temp_dir."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != HOST or not re.fullmatch(r"/download/\d+", u.path):
        raise ValueError("refusing non-midiworld download URL")
    async with _http.stream("GET", url) as r:
        r.raise_for_status()
        final = urllib.parse.urlparse(str(r.url))
        if final.hostname != HOST:
            raise ValueError(f"download redirected off midiworld ({final.hostname})")
        data = b""
        async for chunk in r.aiter_bytes():
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("MIDI file too large")
    if not data.startswith(b"MThd"):
        raise ValueError("not a MIDI file")

    tmp = tempfile.mkdtemp(prefix="midi-")
    try:
        return await _render(data, tmp), tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


async def _render(data: bytes, tmp: str) -> Path:
    mid, wav = Path(tmp) / "song.mid", Path(tmp) / "song.wav"
    mid.write_bytes(data)
    try:
        length = mido.MidiFile(str(mid)).length  # also validates the structure
    except Exception as e:
        raise ValueError(f"malformed MIDI ({type(e).__name__})")
    if length > config.MAX_MIDI_MINUTES * 60:
        raise ValueError(f"MIDI is {length/60:.0f} min long (max {config.MAX_MIDI_MINUTES})")

    proc = await asyncio.create_subprocess_exec(
        "fluidsynth", "-ni", "-q", "-F", str(wav), "-r", "16000", "-g", "0.7",
        "-o", "synth.polyphony=64", config.SOUNDFONT, str(mid),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValueError("rendering took too long")
    if proc.returncode != 0 or not wav.exists() or wav.stat().st_size < 1000:
        raise ValueError("couldn't render that MIDI")
    return wav
