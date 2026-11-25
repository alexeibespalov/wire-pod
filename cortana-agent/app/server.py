"""Cortana's process: the voice loop with the Pi Zero satellite, timers and reminders,
nightly chores, and a small localhost HTTP door:

  GET  /health                     "ok"
  GET  /status                     satellite link, timers
  POST /chat     {"text", "speak"} a turn by text (speak=true also says it on the satellite)
  POST /announce {"text"}          say something on the satellite
  POST /nightly                    run the nightly chores now (testing)

and, when A2A_TOKEN is set, an A2A door for other agents on A2A_PORT (app/a2a_server.py).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from . import activity, beliefs, compaction, config, music, reminders, sleepbatch, turn
from agent_core import ytmusic
from .agent import build_agent
from .conversation import STATE, TeeSink, TextSink, is_never_mind, respond
from .mcp_tools import load_mcp_tools


def _setup_logging() -> None:
    """Console + a rotating file on the data volume (survives container recreation)."""
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = config.DATA_DIR / "logs" / "agent.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(path, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUPS,
                                encoding="utf-8")
    except OSError as e:
        logging.getLogger("cortana-agent").warning("file log disabled: %s", e)
        return
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("cortana-agent").addHandler(h)


_setup_logging()
log = logging.getLogger("cortana-agent")
SAT: dict = {"sat": None}


def _want_follow_up(spoken: str) -> bool:
    if config.FOLLOW_UP_MODE == "always":
        return bool(spoken)
    if config.FOLLOW_UP_MODE == "questions":
        return spoken.rstrip().endswith("?")
    return False


async def on_voice_request(text: str, followup: bool) -> None:
    """The satellite heard something: answer it out loud."""
    sat = SAT["sat"]
    if config.MPD_HOST and music.is_stop_request(text):
        try:
            await music.stop_for_voice()
        except Exception as e:
            log.warning("music: couldn't pause: %s", e)
        await sat.end_quietly("music paused")
        return
    if is_never_mind(text):
        log.info("never mind: %r", text)
        await sat.end_quietly("never mind")
        return
    try:
        async with sat.speak_lock:
            u = sat.utterance()
            spoken = await respond(text, u, channel="voice")
            played = await u.close()
    except turn.Busy as e:
        log.error("turn skipped: %s", e)
        await sat.say_text("Sorry, I'm still busy with the last thing. Try me again in a moment?")
        return
    if played and _want_follow_up(spoken) and sat.connected:
        await sat.listen_again()


async def announce_item(item: dict) -> None:
    """A timer or reminder went off (raises if the satellite can't say it: it's retried)."""
    sat = SAT["sat"]
    if sat is None or not sat.connected:
        raise ConnectionError("satellite offline")
    await sat.ring_timer(item["id"])
    await sat.say_text(reminders.spoken(item))


async def _nightly_loop() -> None:
    h, m = (int(x) for x in config.NIGHTLY_AT.split(":"))
    while True:
        now = datetime.now(config.TIMEZONE)
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await run_nightly()


async def run_nightly() -> dict:
    """The night's chores. Filing notes as beliefs may have to wait (the batch model's answer
    takes a while, or the NUC is down): it is tried again every 20 minutes for a few hours."""
    res: dict = {}
    try:
        # wait for a quiet moment: never while someone's talking to her
        async with turn.hold(600):
            res = await compaction.nightly()
        log.info("nightly chores done: %s", res)
        for _ in range(9):
            rb = res.get("reconcile_beliefs")
            if not (isinstance(rb, dict) and rb.get("waiting")):
                break
            await asyncio.sleep(20 * 60)
            async with turn.hold(600):
                res["reconcile_beliefs"] = await compaction.reconcile_beliefs()
            log.info("nightly: beliefs: %s", res["reconcile_beliefs"])
    except Exception as e:
        log.warning("nightly chores failed: %s", e)
    return res


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tasks = []
    beliefs.start()        # long-term memory on the NUC: cached current beliefs, refreshed in the background
    sleepbatch.start()     # collect batch answers queued before a restart
    mcp = await load_mcp_tools()
    async with AsyncSqliteSaver.from_conn_string(str(config.DATA_DIR / "checkpoints.sqlite")) as saver:
        STATE["agent"] = build_agent(saver, mcp)
        if config.SATELLITE_URI:
            from .satellite import Satellite
            SAT["sat"] = Satellite(config.SATELLITE_URI, on_voice_request)
            tasks.append(asyncio.create_task(SAT["sat"].run()))
            if config.MPD_HOST:
                tasks.append(music.start(SAT["sat"].idle))
        else:
            log.info("SATELLITE_URI empty: text only")
        tasks.append(reminders.start(announce_item))
        tasks.append(asyncio.create_task(_nightly_loop()))
        log.info("%s ready: model=%s effort=%s satellite=%s", config.ASSISTANT_NAME, config.LLM_MODEL,
                 config.LLM_EFFORT, config.SATELLITE_URI or "-")
        activity.record("voice", "agent started")
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()


app = FastAPI(lifespan=lifespan)
app.include_router(ytmusic.router)     # /yt/stream/<id>: YouTube Music for a player on this machine


@app.get("/health")
async def health():
    return PlainTextResponse("ok")


@app.get("/status")
async def status():
    sat = SAT["sat"]
    return {"satellite": {"uri": config.SATELLITE_URI, "connected": bool(sat and sat.connected)},
            "timers": reminders.list_timers.invoke({}), "model": config.LLM_MODEL}


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    sink = TextSink()
    sat = SAT["sat"]
    if body.get("speak") and sat is not None and sat.connected:
        async with sat.speak_lock:
            u = sat.utterance()
            await respond(text, TeeSink(sink, u), channel="text")
            await u.close()
    else:
        await respond(text, sink, channel="text")
    return {"reply": " ".join(sink.sentences), "sentences": sink.sentences}


@app.post("/announce")
async def announce(request: Request):
    text = str((await request.json()).get("text", "")).strip()
    sat = SAT["sat"]
    if not text or sat is None or not sat.connected:
        return JSONResponse({"error": "no text or satellite offline"}, status_code=400)
    await sat.say_text(text)
    return {"ok": True}


@app.post("/nightly")
async def nightly_now():
    """Run the nightly chores now, once (no 20-minute retries): for testing."""
    async with turn.hold(config.TURN_LOCK_WAIT_S):
        return await compaction.nightly()


def main():
    import uvicorn
    servers = [uvicorn.Server(uvicorn.Config(app, host=config.LISTEN_HOST, port=config.LISTEN_PORT,
                                             log_level="warning"))]
    if config.A2A_TOKEN:
        from .a2a_server import build_app
        servers.append(uvicorn.Server(uvicorn.Config(build_app(), host=config.A2A_HOST, port=config.A2A_PORT,
                                                     log_level="warning")))
        log.info("A2A enabled on %s:%s (card: %s/.well-known/agent-card.json)", config.A2A_HOST,
                 config.A2A_PORT, config.A2A_PUBLIC_URL)
    else:
        log.info("A2A disabled (no A2A_TOKEN)")

    if ytmusic.available() and config.YTMUSIC_STREAM_PORT:
        # the same route for players on other devices; nothing else is reachable there
        servers.append(uvicorn.Server(uvicorn.Config(ytmusic.stream_app(), host=config.YTMUSIC_STREAM_HOST,
                                                     port=config.YTMUSIC_STREAM_PORT, log_level="warning")))
        log.info("YouTube Music streams on %s:%s (%s)", config.YTMUSIC_STREAM_HOST,
                 config.YTMUSIC_STREAM_PORT, "signed in" if ytmusic.signed_in() else "not signed in")
    async def serve():
        await asyncio.gather(*(s.serve() for s in servers))
    asyncio.run(serve())


if __name__ == "__main__":
    main()
