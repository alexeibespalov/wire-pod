"""One conversation turn: words in, spoken sentences out.

The agent's text streams in token by token; each sentence goes to the sink (the satellite's
Utterance, or a list for the text door) as soon as it's complete, so she starts talking
while the rest is still being written. Each spoken sentence passes the speech guard first.
"""
import asyncio
import contextvars
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime
from typing import Protocol

from langchain_core.messages import AIMessageChunk

from . import activity, config, guard, llm as llms, memory, turn
from .speech import SILENT, Sentences, clean

log = logging.getLogger("cortana-agent")

STATE: dict = {"agent": None}
_THREAD_FILE = config.DATA_DIR / "thread.json"
_SEARCH_FILLERS = ["One sec, let me check. ", "Let me look that up. ", "Hang on, checking. "]
_NEVER_MIND = re.compile(r"^\W*(?:stop|cancel|never ?mind|nothing|forget it|no thanks?|that's all|"
                         r"that's it|nope)\W*$", re.I)


class Sink(Protocol):
    async def say(self, sentence: str) -> None: ...


class TextSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    async def say(self, sentence: str) -> None:
        self.sentences.append(sentence)


class TeeSink:
    def __init__(self, *sinks: Sink) -> None:
        self.sinks = sinks

    async def say(self, sentence: str) -> None:
        for s in self.sinks:
            await s.say(sentence)


def is_never_mind(text: str) -> bool:
    return bool(_NEVER_MIND.match(text.strip()))


def thread_id() -> tuple[str, float]:
    """(thread id, seconds since the last turn). One continuous conversation by default;
    long histories are folded into a rolling summary by app/compaction.py."""
    now = time.time()
    try:
        t = json.loads(_THREAD_FILE.read_text())
    except (FileNotFoundError, ValueError):
        t = {}
    gap = now - t.get("last", now)
    idle = config.THREAD_IDLE_MINUTES * 60
    if not t.get("id") or (idle > 0 and gap > idle):
        t = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]}
        log.info("new conversation thread %s", t["id"])
    t["last"] = now
    _THREAD_FILE.parent.mkdir(parents=True, exist_ok=True)
    _THREAD_FILE.write_text(json.dumps(t))
    return t["id"], gap


def stamp(gap: float) -> str:
    """'(heard Sat 10:39 AM)' plus how long it's been quiet when that matters."""
    when = datetime.now(config.TIMEZONE).strftime("%a %-I:%M %p")
    if gap >= 36 * 3600:
        when += f"; first words in {gap / 86400:.0f} days"
    elif gap >= 2 * 3600:
        when += f"; first words in {gap / 3600:.0f} hours"
    elif gap >= 20 * 60:
        when += f"; first words in {gap / 60:.0f} minutes"
    return f"(heard {when})"


_bg_tasks: set = set()


def _bg(coro) -> None:
    """Run after the turn, in a fresh context (its model calls must not stream into a reply)."""
    t = asyncio.get_running_loop().create_task(coro, context=contextvars.Context())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def _memory_pass(said: str, reply: str) -> None:
    try:
        for change in await memory.reflect(said, reply):
            log.info("memory: %s", change)
            activity.record("memory", change)
    except Exception as e:
        log.warning("memory pass failed: %s", e)


async def respond(text: str, sink: Sink, channel: str = "voice") -> str:
    """Run one turn and return everything that was said (already sent to the sink)."""
    async with turn.hold(config.TURN_LOCK_WAIT_S):
        turn.channel = channel
        thread, gap = thread_id()
        note = stamp(gap)
        cfg = {"configurable": {"thread_id": thread}}
        log.info("[%s] user (%s): %s %s", thread, channel, note, text)
        t0 = time.monotonic()
        said: list[str] = []
        sentences = Sentences()
        step_tools: set[str] = set()
        step_said = False
        final = None

        async def out(s: str) -> None:
            nonlocal step_said
            s = s.strip()
            if not s or SILENT.match(s):
                return
            s = await guard.check_speech(s, f"{note}\n{text}") or s
            if not said:
                log.info("[%s] first words after %.2fs", thread, time.monotonic() - t0)
            said.append(s)
            step_said = True
            await sink.say(s)

        try:
            async with asyncio.timeout(config.TURN_HARD_LIMIT_S):
                async for mode, data in STATE["agent"].astream(f"{note}\n{text}", cfg,
                                                              stream_mode=["messages", "updates"]):
                    if mode == "messages":
                        msg, meta = data
                        # only the agent's own model call speaks (not models used inside tools)
                        if meta.get("langgraph_node", "call_model") != "call_model":
                            continue
                        if not isinstance(msg, AIMessageChunk):
                            continue
                        for tc in msg.tool_call_chunks or []:
                            if tc.get("name"):
                                step_tools.add(tc["name"])
                        if piece := llms.text(msg):
                            for s in sentences.feed(piece):
                                await out(s)
                    elif mode == "updates":
                        for node, val in data.items():
                            if node == "call_model":           # a model step finished
                                await out(sentences.flush())
                                if (not step_said and not said
                                        and any(n.startswith("tavily") for n in step_tools)):
                                    await out(random.choice(_SEARCH_FILLERS))
                                step_tools, step_said = set(), False
                            elif node == "call_tool" and config.DEBUG:
                                log.info("[%s] tool -> %s", thread, getattr(val, "content", val))
                            elif node == "cortana_agent":
                                final = val
        except TimeoutError:
            log.error("[%s] turn cancelled after %.0fs", thread, config.TURN_HARD_LIMIT_S)
            activity.record("conversation", f"gave up on a turn that ran over {config.TURN_HARD_LIMIT_S:.0f}s")
            await out("Sorry, that took too long. Could you ask me again?")
        except Exception:
            log.exception("[%s] agent error", thread)
            await out("Sorry, my brain glitched for a second. Try me again?")
        if not said and final and not SILENT.match(str(final)):
            await out(clean(str(final)))          # nothing streamed (e.g. a non-streaming model)
        spoken = " ".join(said)
        log.info("[%s] %s (%.2fs): %s", thread, config.ASSISTANT_NAME.lower(), time.monotonic() - t0, spoken)
        activity.record("conversation", f"{note} heard: \"{text[:120]}\" -> I said: \"{spoken[:160]}\"",
                        channel=channel)
        _bg(_memory_pass(f"{note}\n{text}", spoken))      # automatic memory, after the reply
        return spoken
