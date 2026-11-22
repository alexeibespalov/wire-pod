#!/usr/bin/env python3
"""Compare speech recognisers on Vector's captured requests (16 kHz mono s16le PCM).

Engines: Vosk (transcript captured live), faster-whisper over Wyoming (temporary
containers), whisper.cpp CLI. Prints a side-by-side table and writes results.json.
Usage: stt_bench.py <samples_dir> <wcpp_dir>
"""
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

WYOMING = {"fw-base": ("127.0.0.1", 10310), "fw-small": ("127.0.0.1", 10311)}
WCPP_SERVERS = {"cpp-base.en": 10320, "cpp-small.en": 10321}   # whisper-server, model loaded once


async def _send(w, etype, data=None, payload=b""):
    hdr = {"type": etype}
    body = json.dumps(data or {}).encode()
    hdr["data_length"] = len(body)
    if payload:
        hdr["payload_length"] = len(payload)
    w.write(json.dumps(hdr).encode() + b"\n" + body + payload)
    await w.drain()


async def _recv(r):
    hdr = json.loads(await r.readline())
    data = hdr.get("data") or {}
    if hdr.get("data_length"):
        data.update(json.loads(await r.readexactly(hdr["data_length"])))
    if hdr.get("payload_length"):
        await r.readexactly(hdr["payload_length"])
    return hdr["type"], data


async def wyoming(host, port, pcm: bytes) -> str:
    r, w = await asyncio.open_connection(host, port)
    fmt = {"rate": 16000, "width": 2, "channels": 1}
    await _send(w, "transcribe", {"language": "en"})
    await _send(w, "audio-start", fmt)
    for i in range(0, len(pcm), 3200):                       # 100 ms chunks
        await _send(w, "audio-chunk", fmt, pcm[i:i + 3200])
    await _send(w, "audio-stop")
    while True:
        etype, data = await asyncio.wait_for(_recv(r), 120)
        if etype == "transcript":
            w.close()
            return data.get("text", "").strip()


def _wav(pcm: bytes) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wv:
        wv.setnchannels(1)
        wv.setsampwidth(2)
        wv.setframerate(16000)
        wv.writeframes(pcm + b"\0" * max(0, 32000 - len(pcm)))   # whisper.cpp wants >= 1 s
    return buf.getvalue()


def wcpp(port: int, pcm: bytes) -> str:
    import urllib.request
    b = "----vbench"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + _wav(pcm) + (
            f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson"
            f"\r\n--{b}--\r\n").encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/inference", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return " ".join(json.loads(r.read())["text"].split()).strip()


async def main(samples: Path, wcpp_dir: Path):
    clips = sorted(samples.glob("*.pcm"))
    engines = ["vosk", *WYOMING, *WCPP_SERVERS]
    rows = []
    for c in clips:
        pcm = c.read_bytes()
        row = {"clip": c.stem, "seconds": round(len(pcm) / 32000, 1),
               "vosk": {"text": c.with_suffix(".txt").read_text().strip(), "s": None}}
        for name, (h, p) in WYOMING.items():
            t = time.time()
            try:
                row[name] = {"text": await wyoming(h, p, pcm), "s": round(time.time() - t, 2)}
            except Exception as e:
                row[name] = {"text": f"ERROR {e}", "s": None}
        for name, port in WCPP_SERVERS.items():
            t = time.time()
            try:
                row[name] = {"text": wcpp(port, pcm), "s": round(time.time() - t, 2)}
            except Exception as e:
                row[name] = {"text": f"ERROR {e}", "s": None}
        rows.append(row)
        print(f"\n[{row['clip']}] {row['seconds']}s audio")
        for e in engines:
            r = row.get(e, {})
            print(f"  {e:13} {('%.2fs' % r['s']) if r.get('s') else '     '}  {r.get('text', '')}")
    print("\nAVERAGE TIME PER REQUEST")
    for e in engines[1:]:
        ts = [r[e]["s"] for r in rows if r.get(e, {}).get("s")]
        if ts:
            print(f"  {e:13} {statistics.mean(ts):.2f}s  (max {max(ts):.2f}s, {len(ts)} clips)")
    (samples.parent / "stt-bench-results.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
