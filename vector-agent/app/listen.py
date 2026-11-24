"""Overhearing: Vector listens for a few seconds (no wake word) and we get the words.

Vector's SDK audio feed is a placeholder on WireOS (a synthetic test tone, checked
2026-11-24), so this goes through wire-pod (patched, see chipper/pkg/wirepod/overhear):
/api-sdk/overhear arms a capture and starts a wake-word-less listen; wire-pod records the
full N seconds, transcribes them locally (Vosk + Whisper on the Pi) and sends the text to
our chat endpoint as "[overheard] ...". The server hands it to the waiting `overhear()`
call here instead of starting a conversation turn. No audio is kept anywhere.
"""
import asyncio
import logging
import re
import time

from . import robot

log = logging.getLogger("vector-agent")

PREFIX = "[overheard]"
MAX_SECONDS = 15.0
# Listen at most about once a minute: with a look every 15 s that's every 4th look. The gap
# is a bit under 60 s so timing jitter doesn't push it to every 5th.
MIN_GAP_S = 55.0
_waiter: asyncio.Future | None = None
_last_try = float("-inf")
_ADDRESSED = re.compile(r"\b(hey|hi|ok|okay)?\s*,?\s*vector\b", re.I)


def is_capture(text: str) -> bool:
    return text.startswith(PREFIX)


def strip(text: str) -> str:
    return text[len(PREFIX):].strip() if is_capture(text) else text


def addressed_to_him(heard: str) -> bool:
    """Someone said his name during a capture: the wake word can't fire while he's already
    listening, so treat it as a request too."""
    return bool(_ADDRESSED.search(heard))


def due() -> bool:
    """True if it's been long enough since the last listen (tried or not) to listen again."""
    return time.monotonic() - _last_try >= MIN_GAP_S


def deliver(text: str) -> bool:
    """From the chat endpoint: a capture's transcript arrived. False if nobody's waiting
    (the capture timed out); the text is dropped then."""
    if _waiter is not None and not _waiter.done():
        _waiter.set_result(strip(text))
        return True
    return False


async def overhear(seconds: float = 10) -> str | None:
    """Listen for `seconds` and return what was heard ("(nothing clear...)" for quiet),
    or None if listening didn't work (busy, robot asleep, wire-pod unpatched)."""
    global _waiter, _last_try
    if _waiter is not None and not _waiter.done():
        return None                              # one capture at a time
    _last_try = time.monotonic()                 # failures count too: don't retry every look
    seconds = max(2.0, min(float(seconds), MAX_SECONDS))
    fut = asyncio.get_running_loop().create_future()
    _waiter = fut
    try:
        res = await robot.sdk("overhear", seconds=f"{seconds:g}")
        if res != "listening":
            log.warning("overhear: wire-pod said %r (patched chipper?)", res[:80])
            return None
        # the listen starts ~1-2 s after the intent, then N s of audio, then Whisper
        return await asyncio.wait_for(fut, seconds + 15)
    except Exception as e:                       # timeout, wire-pod/robot unreachable
        log.warning("overhear: no transcript (%s)", type(e).__name__ if isinstance(e, asyncio.TimeoutError) else e)
        return None
    finally:
        if _waiter is fut:
            _waiter = None
