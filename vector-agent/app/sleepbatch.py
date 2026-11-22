"""Sleep jobs on Claude Sonnet through the Message Batches API (half price; Claude only).

Sleep jobs (photo labels, places, practice review, memory harvest, conversation folding)
aren't urgent, so they run on a stronger model (config.SLEEP_MODEL) as batch requests:

    res = await sleepbatch.model(max_tokens=900).ainvoke(messages)

* If this exact request already has an answer from an earlier batch: returns it at once
  (an AIMessage, like any LangChain chat model).
* Otherwise it's queued and Pending is raised. Jobs treat Pending as "not yet" (the work
  stays pending, exactly like a failure that doesn't count) and pick the answer up in a
  later sleep session: sessions repeat every ~20 min while he sleeps, and jobs are
  idempotent.
* Queued requests are sent as ONE batch when a job ends (run_session calls flush()), or
  after a short idle timer. A background poller collects finished batches into
  DATA_DIR/sleep_batches.json.

Requests are keyed by a hash of their content: the same prompt next session finds its
answer; a changed prompt is simply a new request (stale answers expire). With the openai
provider or SLEEP_BATCH=0, model() returns the normal direct chat model instead.
"""
import asyncio
import base64
import hashlib
import json
import logging
import threading
import time

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from . import config

log = logging.getLogger("vector-agent")

_FILE = config.DATA_DIR / "sleep_batches.json"
_LOCK = threading.Lock()
_POLL_S = 60
_FLUSH_IDLE_S = 20
_MAX_PER_BATCH = 200

_queue: dict[str, dict] = {}       # key -> request params, waiting to be sent
_flush_timer: asyncio.TimerHandle | None = None
_poller: asyncio.Task | None = None
_client = None


class Pending(Exception):
    """The answer isn't ready yet: it was queued for (or is in) a batch."""


def _load() -> dict:
    try:
        d = json.loads(_FILE.read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("inflight", {})   # batch_id -> {"keys": [...], "created": t}
    d.setdefault("results", {})    # key -> {"text": ..., "t": ...} or {"error": ..., "t": ...}
    return d


def _save(d: dict) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(_FILE)


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=120, max_retries=2)
    return _client


# ------------------------------------------------------------- LangChain -> API params
def _block(part) -> dict:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if part.get("type") == "image_url":
        url = part["image_url"]["url"] if isinstance(part["image_url"], dict) else part["image_url"]
        head, data = url.split(",", 1)                     # data:image/jpeg;base64,....
        media = head[5:].split(";")[0]
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    if part.get("type") == "text":
        return {"type": "text", "text": part.get("text", "")}
    raise ValueError(f"unsupported content block for a batch: {part.get('type')}")


def _params(messages: list[BaseMessage], max_tokens: int) -> dict:
    system, out = [], []
    for m in messages:
        if isinstance(m, SystemMessage):
            system.append(m.content if isinstance(m.content, str) else json.dumps(m.content))
            continue
        role = "assistant" if isinstance(m, AIMessage) else "user"
        content = m.content if isinstance(m.content, str) else [_block(p) for p in m.content]
        if out and out[-1]["role"] == role:                # merge same-role neighbours
            prev = out[-1]["content"]
            prev = [{"type": "text", "text": prev}] if isinstance(prev, str) else prev
            out[-1]["content"] = prev + ([{"type": "text", "text": content}] if isinstance(content, str) else content)
        else:
            out.append({"role": role, "content": content})
    p = {"model": config.SLEEP_MODEL, "max_tokens": max(max_tokens, 4096), "messages": out,
         "output_config": {"effort": config.LLM_BACKGROUND_EFFORT}}
    if system:
        p["system"] = "\n\n".join(system)
    return p


def _key(params: dict) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
    return "r" + hashlib.sha256(raw).hexdigest()[:40]       # custom_id: [A-Za-z0-9_-]{1,64}


# ------------------------------------------------------------------- the model facade
class _BatchModel:
    """Just enough of a LangChain chat model for the sleep jobs: ainvoke(messages)."""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens

    async def ainvoke(self, messages, **_ignored) -> AIMessage:
        params = _params(list(messages), self.max_tokens)
        key = _key(params)
        with _LOCK:
            d = _load()
            r = d["results"].get(key)
            if r is not None:
                d["results"].pop(key)                      # answers are used once
                _save(d)
                if "error" in r:
                    raise RuntimeError(f"batch request failed: {r['error']}")
                return AIMessage(content=r["text"])
            waiting = key in _queue or any(key in b["keys"] for b in d["inflight"].values())
        if not waiting:
            _queue[key] = params
            _arm_flush()
        raise Pending(key)

    def bind(self, **_ignored):     # json_model() / bind(response_format=...) are no-ops here
        return self


def model(max_tokens: int = 900, direct=None):
    """The model a sleep job should use: batch-backed on Claude (SLEEP_BATCH), otherwise
    `direct` (the job's normal chat model)."""
    if config.SLEEP_BATCH:
        return _BatchModel(max_tokens)
    return direct


def enabled() -> bool:
    return config.SLEEP_BATCH


# ------------------------------------------------------------------- submit and collect
def _arm_flush() -> None:
    global _flush_timer
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _flush_timer is not None:
        _flush_timer.cancel()
    _flush_timer = loop.call_later(_FLUSH_IDLE_S, lambda: loop.create_task(flush()))


async def flush() -> int:
    """Send everything queued as one batch (or a few, if very many). Returns how many."""
    global _flush_timer
    if _flush_timer is not None:
        _flush_timer.cancel()
        _flush_timer = None
    if not _queue:
        return 0
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    items = list(_queue.items())
    _queue.clear()
    sent = 0
    for i in range(0, len(items), _MAX_PER_BATCH):
        chunk = items[i:i + _MAX_PER_BATCH]
        try:
            b = await _anthropic().messages.batches.create(requests=[
                Request(custom_id=k, params=MessageCreateParamsNonStreaming(**p)) for k, p in chunk])
        except Exception as e:
            log.warning("sleepbatch: couldn't submit %d request(s): %s", len(chunk), e)
            continue                                       # dropped: the job asks again next session
        with _LOCK:
            d = _load()
            d["inflight"][b.id] = {"keys": [k for k, _ in chunk], "created": time.time()}
            _save(d)
        sent += len(chunk)
        log.info("sleepbatch: submitted %s with %d request(s) on %s", b.id, len(chunk), config.SLEEP_MODEL)
    _ensure_poller()
    return sent


async def _collect(batch_id: str) -> bool:
    """True once the batch has ended and its results are stored."""
    c = _anthropic()
    b = await c.messages.batches.retrieve(batch_id)
    if b.processing_status != "ended":
        return False
    got: dict[str, dict] = {}
    async for r in await c.messages.batches.results(batch_id):
        t = r.result.type
        if t == "succeeded":
            msg = r.result.message
            text = "".join(blk.text for blk in msg.content if blk.type == "text")
            if msg.stop_reason == "refusal":
                got[r.custom_id] = {"error": "refused", "t": time.time()}
            else:
                got[r.custom_id] = {"text": text, "t": time.time()}
        elif t == "errored":
            got[r.custom_id] = {"error": f"{r.result.error.type}", "t": time.time()}
        # canceled / expired: nothing stored, the job simply asks again
    with _LOCK:
        d = _load()
        d["results"].update(got)
        d["inflight"].pop(batch_id, None)
        _save(d)
    log.info("sleepbatch: %s ended: %d answer(s) stored", batch_id, len(got))
    return True


def _prune(d: dict) -> None:
    cutoff = time.time() - config.SLEEP_BATCH_MAX_AGE_H * 3600
    for bid in [k for k, v in d["inflight"].items() if v["created"] < cutoff]:
        d["inflight"].pop(bid)                             # batches end within 24 h
    for k in [k for k, v in d["results"].items() if v.get("t", 0) < cutoff - 24 * 3600]:
        d["results"].pop(k)                                # unclaimed answers (prompt changed)


async def _poll_forever() -> None:
    while True:
        try:
            with _LOCK:
                d = _load()
                _prune(d)
                _save(d)
                ids = list(d["inflight"])
            if not ids and not _queue:
                return
            for bid in ids:
                try:
                    await _collect(bid)
                except Exception as e:
                    log.warning("sleepbatch: checking %s failed: %s", bid, e)
        except Exception:
            log.exception("sleepbatch: poller error")
        await asyncio.sleep(_POLL_S)


def _ensure_poller() -> None:
    global _poller
    if _poller is None or _poller.done():
        _poller = asyncio.get_running_loop().create_task(_poll_forever())


def start() -> None:
    """From the FastAPI lifespan: resume polling batches submitted before a restart."""
    if config.SLEEP_BATCH and _load()["inflight"]:
        _ensure_poller()


def status_line() -> str:
    if not config.SLEEP_BATCH:
        return ""
    d = _load()
    n = sum(len(b["keys"]) for b in d["inflight"].values())
    return f"{n} sleep request(s) waiting in batches, {len(d['results'])} answer(s) ready, {len(_queue)} queued"
