"""What the screen on Cortana's satellite shows about an agent (deploy/pizero/dashboard).

Two parts:

* Activity events, as one line each: "think", "tool <name>", "skill <name>", "done", and
  "a2a on" / "a2a off" around any exchange with the other agent (asking it, or answering
  it). The loop and the A2A code call emit(); whoever registered with listen() gets the
  line. Cortana forwards them to the Zero over MPD's `cortana` channel. With no listener
  nothing is sent anywhere.
* GET /dash on a second listener (DASH_PORT, reachable from the LAN while the agent's
  main door stays on 127.0.0.1): read-only counts, nothing personal.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Callable

from fastapi import FastAPI

from .settings import cfg as config
from . import beliefs

log = logging.getLogger(config.LOGGER_NAME)

_listeners: list[Callable[[str], None]] = []
_a2a_open = 0
state: dict = {"activity": "idle", "detail": "", "at": 0.0, "a2a": False}


def listen(fn: Callable[[str], None]) -> None:
    """Call fn(line) for every activity event from now on."""
    _listeners.append(fn)


def _send(line: str) -> None:
    for fn in _listeners:
        try:
            fn(line)
        except Exception as e:  # a screen must never break a turn
            log.debug("dash: listener failed: %s", e)


def emit(kind: str, detail: str = "") -> None:
    detail = " ".join(str(detail).split())[:80]
    state.update(activity=kind, detail=detail, at=time.time())
    _send(f"{kind} {detail}".strip())


@contextlib.contextmanager
def a2a_exchange():
    """Around one exchange with the other agent; nested or parallel ones count once."""
    global _a2a_open
    _a2a_open += 1
    if _a2a_open == 1:
        state["a2a"] = True
        _send("a2a on")
    try:
        yield
    finally:
        _a2a_open = max(0, _a2a_open - 1)
        if _a2a_open == 0:
            state["a2a"] = False
            _send("a2a off")


def stats() -> dict:
    return {
        "agent": config.ASSISTANT_NAME,
        "beliefs": len(beliefs.current()) if beliefs.ENABLED else None,
        "activity": state["activity"], "detail": state["detail"], "a2a": state["a2a"],
    }


def dash_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/dash")
    async def dash() -> dict:
        return stats()

    return app
