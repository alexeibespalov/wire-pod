"""A turn must never keep the turn lock after its client goes away (2026-11-22 outage:
wire-pod dropped a stream, the reply generator kept the lock, Vector went silent for an hour)."""
import asyncio

from app import config, server, turn


def _gen(tag):
    async def g():
        async with turn.hold(2):
            for i in range(3):
                await asyncio.sleep(0.02)
                yield f"data: {tag}{i}\n\n"
            yield "data: [DONE]\n\n"
    return g()


def test_abandoned_client_releases_lock():
    turn._lock = None
    async def main():
        r = server._detached(_gen("a"), "c", "m")
        await r.__anext__()
        del r
        await asyncio.sleep(0.3)
        assert not turn.lock().locked()
        out = [x async for x in server._detached(_gen("b"), "c", "m")]
        assert out[-1].startswith("data: [DONE]")
    asyncio.run(main())


def test_stuck_lock_gives_busy_reply_not_silence():
    async def main():
        await turn.lock().acquire()
        try:
            out = [x async for x in server._detached(_gen("c"), "c", "m")]
        finally:
            turn.lock().release()
        assert any("busy" in x for x in out) and out[-1].startswith("data: [DONE]")
    turn._lock = None
    asyncio.run(main())


def test_hung_turn_is_cancelled(monkeypatch):
    monkeypatch.setattr(config, "TURN_HARD_LIMIT_S", 0.2)

    async def hung():
        async with turn.hold(2):
            yield "data: x\n\n"
            await asyncio.sleep(99)

    async def main():
        out = [x async for x in server._detached(hung(), "c", "m")]
        assert out[-1].startswith("data: [DONE]") and not turn.lock().locked()
    turn._lock = None
    asyncio.run(main())
