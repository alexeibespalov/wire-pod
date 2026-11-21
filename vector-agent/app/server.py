"""OpenAI-compatible front door for wire-pod's "Custom" knowledge provider.

wire-pod sends its own system prompt + its own short chat history; we ignore both
and only take the latest user utterance. Conversation history, persona and tools
belong to the agent. Replies stream back as OpenAI chat.completion.chunk SSE so
wire-pod can start speaking at the first sentence.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from . import config
from .agent import build_agent

log = logging.getLogger("vector-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATE = {"agent": None}
_turn_lock = asyncio.Lock()  # one robot, one conversation at a time
_THREAD_FILE = config.DATA_DIR / "thread.json"
_CUE = re.compile(r"\{\{.*?\}\}")


def _thread_id() -> str:
    """Same thread while the conversation is live; a fresh one after a quiet spell."""
    now = time.time()
    try:
        t = json.loads(_THREAD_FILE.read_text())
    except (FileNotFoundError, ValueError):
        t = {}
    if not t.get("id") or now - t.get("last", 0) > config.THREAD_IDLE_MINUTES * 60:
        t = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]}
        log.info("new conversation thread %s", t["id"])
    t["last"] = now
    _THREAD_FILE.write_text(json.dumps(t))
    return t["id"]


def _user_text(body: dict) -> str:
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # multimodal parts
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return (c or "").strip()
    return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(config.DATA_DIR / "checkpoints.sqlite")) as saver:
        STATE["agent"] = build_agent(saver)
        log.info("vector-agent ready: model=%s base=%s", config.LLM_MODEL, config.LLM_BASE_URL)
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return PlainTextResponse("ok")


def _chunk(cid: str, model: str, content: str | None = None, finish: str | None = None) -> str:
    delta = {"role": "assistant", "content": content} if content is not None else {}
    return "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = _user_text(body)
    stream = bool(body.get("stream"))
    model = config.LLM_MODEL
    cid = "chatcmpl-" + uuid.uuid4().hex
    if not text:
        text = "(the user said something but speech-to-text caught nothing)"

    async def run_stream():
        async with _turn_lock:
            thread = _thread_id()
            cfg = {"configurable": {"thread_id": thread}}
            log.info("[%s] user: %s", thread, text)
            t0, said, final = time.time(), [], None
            try:
                async for mode, data in STATE["agent"].astream(text, cfg, stream_mode=["messages", "updates"]):
                    if mode == "messages":
                        msg, meta = data
                        if isinstance(msg, AIMessageChunk) and msg.content:
                            if not said:
                                log.info("[%s] first token after %.2fs", thread, time.time() - t0)
                            said.append(msg.content)
                            yield _chunk(cid, model, msg.content)
                    elif mode == "updates":
                        for node, val in data.items():
                            if node == "call_tool" and config.DEBUG:
                                log.info("[%s] tool -> %s", thread, getattr(val, "content", val))
                            if node == "vector_agent":
                                final = val
            except Exception as e:  # never leave wire-pod hanging mid-stream
                log.exception("agent error")
                fallback = "{{playAnimationWI||confused}} Hmm, my brain glitched for a second. Try me again?"
                yield _chunk(cid, model, fallback)
                said.append(fallback)
            if not said and final:  # e.g. canned reply not produced by the LLM
                yield _chunk(cid, model, str(final))
                said.append(str(final))
            # Deterministic conversation mode: a reply that ends in a question keeps
            # Vector listening, whether or not the model remembered the cue.
            full = "".join(said)
            spoken = _CUE.sub("", full).strip()
            if spoken.endswith("?") and "newVoiceRequest" not in full:
                cue = " {{newVoiceRequest||now}}"
                yield _chunk(cid, model, cue)
                said.append(cue)
            log.info("[%s] vector (%.2fs): %s", thread, time.time() - t0, "".join(said))
            yield _chunk(cid, model, None, "stop")
            yield "data: [DONE]\n\n"

    if stream:
        return StreamingResponse(run_stream(), media_type="text/event-stream")

    parts = []
    async for line in run_stream():
        if line.startswith("data: {"):
            d = json.loads(line[6:])
            parts.append(d["choices"][0]["delta"].get("content") or "")
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)},
                     "finish_reason": "stop"}],
    })


def main():
    import uvicorn
    uvicorn.run(app, host=config.LISTEN_HOST, port=config.LISTEN_PORT, log_level="warning")


if __name__ == "__main__":
    main()
