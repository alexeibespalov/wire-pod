"""Low-level access to Vector through wire-pod's local HTTP APIs.

* /api-sdk/*          simple SDK actions (wheels, head, lift, settings, battery, intents)
* /api-lua/run_script runs a Lua script against the robot (animations, face images)
* /cam-stream         MJPEG camera feed
"""
import json
import re

import httpx

from . import config

_client = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=5))
_SAFE = re.compile(r"^[A-Za-z0-9_./\-]+$")


async def sdk(endpoint: str, **params) -> str:
    params["serial"] = config.VECTOR_SERIAL
    r = await _client.get(f"{config.WIREPOD_URL}/api-sdk/{endpoint}", params=params)
    text = r.text.strip()
    if r.status_code >= 400 or text.startswith("error"):
        raise RuntimeError(f"{endpoint}: {text[:200]}")
    return text


def lua_str(value: str) -> str:
    """Quote a value for Lua. Only plain identifiers/paths are allowed (no injection)."""
    if not _SAFE.match(value):
        raise ValueError(f"unsafe value for Lua: {value!r}")
    return json.dumps(value)


def control() -> str:
    """Lua prefix to take behaviour control, unless wire-pod already holds it this turn
    (then asking would steal it from wire-pod and cut Vector's speech; see app/turn.py)."""
    from . import turn
    return "" if turn.wirepod_has_control else "assumeBehaviorControl(20) "


async def lua(script: str) -> None:
    """Run Lua on the robot. Blocks until the script (and anything synchronous in it) ends.
    wire-pod releases behaviour control automatically when the script finishes."""
    r = await _client.post(f"{config.WIREPOD_URL}/api-lua/run_script",
                           json={"esn": config.VECTOR_SERIAL, "script": script})
    if r.status_code >= 400:
        raise RuntimeError(f"lua: {r.text.strip()[:200]}")


async def camera_frame(skip: int = 1) -> bytes:
    """Grab one JPEG from the camera stream (skipping `skip` frames so it's fresh)."""
    buf, seen = b"", 0
    async with _client.stream("GET", f"{config.WIREPOD_URL}/cam-stream",
                              params={"serial": config.VECTOR_SERIAL}, timeout=20) as r:
        async for chunk in r.aiter_bytes():
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8\xff")
                e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                if s < 0 or e < 0:
                    break
                frame, buf = buf[s:e + 2], buf[e + 2:]
                if seen >= skip:
                    return frame
                seen += 1
            if len(buf) > 2_000_000:
                buf = buf[-200_000:]
    raise RuntimeError("camera stream ended without a frame")
