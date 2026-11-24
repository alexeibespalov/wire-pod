"""One conversation turn at a time (voice, the text door and reminders share one voice)."""
import asyncio
import contextlib

channel = "voice"          # "voice" (spoken on the satellite), "text" (the /chat test door) or "a2a" (another agent)

_lock: asyncio.Lock | None = None


def lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


class Busy(Exception):
    """The turn lock couldn't be had in time (another turn is running or stuck)."""


@contextlib.asynccontextmanager
async def hold(wait_s: float):
    """The turn lock, but give up after wait_s instead of waiting forever."""
    lk = lock()
    try:
        await asyncio.wait_for(lk.acquire(), wait_s)
    except (TimeoutError, asyncio.TimeoutError):
        raise Busy(f"turn lock busy for {wait_s:.0f}s") from None
    try:
        yield
    finally:
        lk.release()
