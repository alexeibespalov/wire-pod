"""OpenAI-compatible front door for wire-pod's "Custom" knowledge provider.

wire-pod sends its own system prompt + its own short chat history; we ignore both
and only take the latest user utterance. Conversation history, persona and tools
belong to the agent. Replies stream back as OpenAI chat.completion.chunk SSE so
wire-pod can start speaking at the first sentence.
"""
import asyncio
import contextvars
from datetime import datetime
import json
import logging
import random
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
from .mcp_tools import load_mcp_tools
from .speech import Sanitizer, clean
from . import llm as llms
from . import activity, beliefs, body, explore_capture, faces, guard, library, listen, memory, navmap, practice, scheduler, sleep_routine, turn
from .audio import PLAYER
from . import belief_sleep, compaction  # noqa: F401  (register the sleep jobs: beliefs, folding, pruning)

def _setup_file_log() -> None:
    """Keep the agent's own log (conversations, tools, presence, errors) on the data
    volume so it survives container recreation. Rotating: 5 MB x 5 files."""
    from logging.handlers import RotatingFileHandler
    path = config.DATA_DIR / "logs" / "agent.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(path, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUPS,
                                encoding="utf-8")
    except OSError as e:
        logging.getLogger("vector-agent").warning("file log disabled: %s", e)
        return
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("vector-agent").addHandler(h)


log = logging.getLogger("vector-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_setup_file_log()

STATE = {"agent": None}
_THREAD_FILE = config.DATA_DIR / "thread.json"
_CUE = re.compile(r"\{\{.*?\}\}")
_QUIET_STEP_TOOLS = {"play_music", "play_podcast", "play_midi", "stop_music", "load_skill",
                     "save_routine", "list_routines", "delete_routine", "schedule_action",
                     "list_schedules", "cancel_schedule", "create_skill", "update_skill",
                     "forget_skill", "recent_activity", "search_logs", "remember"}
_SEARCH_FILLERS = [
    "{{playAnimationWI||thinking}} Ooh, let me look that up. ",
    "{{playAnimationWI||thinking}} One sec, checking the internet. ",
    "{{playAnimationWI||dartingEyes}} Hang on, let me find out. ",
    "{{playAnimationWI||thinking}} Good one. Let me check. ",
]
_LEAD_INS = ["Here it comes!", "Watch this!", "Check this out!", "Ta-da, here you go!"]
_PICTURE_FILLERS = [
    "Ooh, let me find a picture. ",
    "Hang on, finding one for my screen. ",
    "One sec, let me show you. ",
]
_LOOK_FILLERS = [
    "Let me see. ",
    "Ooh, let me have a look. ",
    "Hold still, I'm looking. ",
]


def _thread_id() -> tuple[str, float]:
    """(thread id, seconds since the last turn). One continuous conversation by default
    (THREAD_IDLE_MINUTES=0): long histories are folded into a rolling summary by
    app/compaction.py. Set THREAD_IDLE_MINUTES to start fresh after a quiet spell."""
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
    _THREAD_FILE.write_text(json.dumps(t))
    return t["id"], gap


def _stamp(eyes: str, gap: float) -> str:
    """'(eyes: Leo is right in front of you)' -> '(eyes: ...; Sat 10:39 AM)', plus how long
    it's been quiet when that matters, so the history shows time passing."""
    when = datetime.now(config.TIMEZONE).strftime("%a %-I:%M %p")
    if gap >= 36 * 3600:
        when += f"; first words in {gap / 86400:.0f} days"
    elif gap >= 2 * 3600:
        when += f"; first words in {gap / 3600:.0f} hours"
    elif gap >= 20 * 60:
        when += f"; first words in {gap / 60:.0f} minutes"
    return f"{eyes[:-1]}; {when})"


_STOP = re.compile(r"^(?:please\s+)?(?:stop|pause|enough|quiet|be quiet|shush|turn it off|"
                   r"stop (?:the |that )?(?:music|song|podcast|playing|it)|"
                   r"music off|that's enough)(?:\s+please)?[.!]*$", re.I)


def _is_stop(text: str) -> bool:
    return bool(_STOP.match(text.strip()))


_NOISE_WORDS = {"", "the", "a", "an", "uh", "um", "hmm", "huh", "ah", "oh", "and", "it", "i"}


def _is_silence(text: str) -> bool:
    """VOSK turns silence/room noise into nothing or a lone filler word."""
    words = re.sub(r"[^a-z' ]", " ", text.lower()).split()
    return len(words) == 0 or (len(words) == 1 and words[0] in _NOISE_WORDS)


# Silence after he promised to do something ("hold it up and I'll take a closer look") is
# usually the person doing what he asked, not the end of the conversation.
_last_reply = {"text": "", "t": 0.0, "from_silence": False}
_PROMISE = re.compile(r"\b(I'?ll|I will|let me|I'?m going to|I'?ll have|hold it|show me|bring it|"
                      r"put it|point me|turn on a light|move it|try again)\b", re.I)
_SILENT_WORD = re.compile(r"^\W*SILENT\W*$", re.I)
SILENCE_TURN = ("(No words: they stayed quiet after your last reply, most likely doing what you "
                "asked, e.g. holding something up for you. If your last reply said you'd do "
                "something now (look again, check, try), do it straight away with your tools, then "
                "say what you found in a sentence or two. If not, reply with exactly SILENT.)")


def _promised_recently() -> bool:
    """His last reply (in the last 90 s) promised an action, and it wasn't itself the
    answer to a silence (one silence turn in a row at most: no looking forever)."""
    return (time.time() - _last_reply["t"] < 90 and not _last_reply["from_silence"]
            and bool(_PROMISE.search(_last_reply["text"])))


def _is_followup(body: dict) -> bool:
    """wire-pod's knowledge-graph path (follow-ups after newVoiceRequest, and "I have a
    question") holds Vector's behaviour control from the START of the request and loops
    a "searching" animation, so robot actions must not request control themselves.
    wire-pod marks that path in its system prompt."""
    for m in body.get("messages", []):
        if m.get("role") == "system" and "You are in 'conversation' mode" in str(m.get("content")):
            return True
    return False


# Known speech-to-text mishearings (VOSK's small English model lacks some words).
# Applied to the transcript before the agent sees it. Add new ones as they show up.
_STT_FIXES = [
    (re.compile(r"\b(?:a|an|in|and)\s+(?:mogi|moji|mo\s?gee|mojis|mogis)\b", re.I), "an emoji"),
    (re.compile(r"\b(?:e|him|em|ian|e-?)\s?(?:oji|moji|mogi|ogi|mo\s?gee)s?\b", re.I), "emoji"),
    (re.compile(r"\bemo\s?gee\b|\bmiji\b|\bimogi\b|\bemogi\b|\bemoje\b", re.I), "emoji"),
]


def _fix_transcript(text: str) -> str:
    fixed = text
    for pat, rep in _STT_FIXES:
        fixed = pat.sub(rep, fixed)
    if fixed != text:
        log.info("stt fix: %r -> %r", text, fixed)
    return fixed


def _user_text(body: dict) -> str:
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # multimodal parts
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return (c or "").strip()
    return ""


_bg_tasks: set = set()


def _bg(coro) -> None:
    """Run after the turn, in a fresh context (its model calls must not stream into the reply)."""
    t = asyncio.get_running_loop().create_task(coro, context=contextvars.Context())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _detached(agen, cid: str, model: str):
    """Run a reply generator to the end in its OWN task and hand its chunks to the client
    through a queue. The turn holds the turn lock; if wire-pod drops the stream mid-reply,
    a generator left suspended at a `yield` would keep that lock forever and every later
    "Hey Vector" would wait in silence. Here the turn always finishes (or is cancelled at
    TURN_HARD_LIMIT_S) and releases the lock, whatever the client does."""
    q: asyncio.Queue = asyncio.Queue()
    sent_stop = [False]

    async def pump():
        try:
            async with asyncio.timeout(config.TURN_HARD_LIMIT_S):
                async for item in agen:
                    if item.startswith("data: [DONE]"):
                        sent_stop[0] = True
                    q.put_nowait(item)
        except turn.Busy as e:
            log.error("turn skipped: %s (a previous turn is still running)", e)
            activity.record("conversation", f"couldn't answer: still busy with the previous turn ({e})")
            q.put_nowait(_chunk(cid, model, "{{playAnimationWI||confused}} Sorry, I'm still busy. Try me again in a moment?"))
        except TimeoutError:
            log.error("turn cancelled: still running after %.0fs", config.TURN_HARD_LIMIT_S)
            activity.record("conversation", f"gave up on a turn that ran over {config.TURN_HARD_LIMIT_S:.0f}s")
        except Exception:
            log.exception("turn failed")
        finally:
            if not sent_stop[0]:
                q.put_nowait(_chunk(cid, model, None, "stop"))
                q.put_nowait("data: [DONE]\n\n")
            q.put_nowait(None)

    t = asyncio.get_running_loop().create_task(pump())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

    async def reader():
        while (item := await q.get()) is not None:
            yield item
    return reader()


async def _memory_pass(said: str, reply: str) -> None:
    try:
        for change in await memory.reflect(said, reply):
            log.info("memory: %s", change)
            activity.record("memory", change)
    except Exception as e:
        log.warning("memory pass failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLAYER.bind_loop(asyncio.get_running_loop())
    explore_capture.bind_loop(asyncio.get_running_loop())
    beliefs.start()          # long-term memory on the NUC: cached current beliefs, refreshed in the background
    explore_capture.start()  # photos when his own exploring meets obstacles/objects (hooks faces' SDK link)
    sleep_routine.start()    # night-time chores while he sleeps (registers its hook before faces.start)
    navmap.register()  # nav map feed rides on the same SDK link (before it connects)
    body.register()    # body engine's state feed (fine movement / maneuvers) on the same SDK link
    faces.start()  # background listen-only SDK connection for face presence
    library.start()  # index the NAS music library (cached)
    scheduler.start()  # scheduled actions / routines in the background
    practice.start()   # idle practice loop (off unless BODY_IDLE_PRACTICE=1)
    mcp = await load_mcp_tools()
    async with AsyncSqliteSaver.from_conn_string(str(config.DATA_DIR / "checkpoints.sqlite")) as saver:
        STATE["agent"] = build_agent(saver, mcp)
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
    text = _fix_transcript(_user_text(body))
    sleep_routine.note_activity()
    followup = _is_followup(body)
    eyes = faces.speaker_note()  # who's in front of him as the words arrive
    stream = bool(body.get("stream"))
    model = config.LLM_MODEL
    cid = "chatcmpl-" + uuid.uuid4().hex
    async def silent_end():
        # Follow-up listening heard nothing (or just noise): end the conversation
        # quietly with a glance instead of answering thin air.
        log.info("silence/noise after follow-up (%r): ending conversation", text)
        yield _chunk(cid, model, "{{playAnimationWI||dartingEyes}}")
        yield _chunk(cid, model, None, "stop")
        yield "data: [DONE]\n\n"
        PLAYER.turn_ended(0.5)  # resume music a wake word paused

    async def run_stream():
        async with turn.hold(config.TURN_LOCK_WAIT_S):  # shared with A2A: one turn at a time
            thread, gap = _thread_id()
            note = _stamp(eyes, gap)
            cfg = {"configurable": {"thread_id": thread}}
            log.info("[%s] user: %s %s", thread, note, text)
            t0, said, final, tools_used = time.time(), [], None, set()
            PLAYER.turn_started()
            scheduler.on_turn_start()
            turn.reset(is_followup=followup, voice_turn=True)  # follow-up: wire-pod holds the robot from the start
            if followup:
                log.info("[%s] follow-up (knowledge-graph path): wire-pod holds control", thread)
            # Text from one model step is held until the step ends, because only then do we
            # know whether it calls robot tools: those act first, then he talks (an image
            # shown over his talking face comes out black; see app/turn.py).
            step_text: list[str] = []
            step_tools: set[str] = set()
            intent_sent = False  # a built-in behaviour answered the request: say nothing more
            overheard = False    # he judged it "not for me" (people talking nearby)

            def speak(piece: str):
                if intent_sent or _SILENT_WORD.match(piece or ""):
                    return None
                if piece:
                    if not said:
                        log.info("[%s] first words after %.2fs", thread, time.time() - t0)
                    said.append(piece)
                    turn.mark_wirepod_control()  # wire-pod takes control at the first words
                    turn.mark_speech(piece)      # his body is busy talking until about then
                    return _chunk(cid, model, piece)
                return None

            try:
                async for mode, data in STATE["agent"].astream(f"{note}\n{text}", cfg, stream_mode=["messages", "updates"]):
                    if mode == "messages":
                        msg, meta = data
                        # Only the agent's own model call speaks. Model calls made INSIDE
                        # tools (e.g. the vision model in `look`) must not reach wire-pod.
                        if meta.get("langgraph_node", "call_model") != "call_model":
                            continue
                        if not isinstance(msg, AIMessageChunk):
                            continue
                        for tc in msg.tool_call_chunks or []:
                            if tc.get("name"):
                                step_tools.add(tc["name"])
                        if piece := llms.text(msg):   # str (Qwen) or text blocks (Claude)
                            step_text.append(piece)
                    elif mode == "updates":
                        for node, val in data.items():
                            if node == "call_model":  # a model step finished: decide what to say
                                san = Sanitizer()
                                text_out = san.feed("".join(step_text)) + san.flush()
                                uses_control = bool(step_tools & turn.CONTROL_TOOLS)
                                # music: playback starts after the reply, so narration
                                # around these calls ("let me try again...") is just noise
                                quiet_step = uses_control or bool(step_tools & _QUIET_STEP_TOOLS)
                                if text_out.strip() and not quiet_step and not _SILENT_WORD.match(text_out):
                                    # safety laws, post-hook: check words before they're spoken
                                    safe = await guard.check_speech(text_out, f"{note}\n{text}")
                                    if safe is not None:
                                        text_out = safe
                                if not step_tools:
                                    out = speak(text_out)                      # the answer
                                elif uses_control and turn.followup and not said:
                                    # stop wire-pod's looping "searching" animation with a
                                    # short lead-in; the face is used once it's spoken
                                    out = speak(random.choice(_LEAD_INS))
                                    turn.mark_lead_in()
                                elif quiet_step:
                                    out = None                                 # act first, talk after
                                elif text_out.strip():
                                    out = speak(text_out)                      # preamble before a search
                                elif not said and any(n.startswith("tavily") for n in step_tools):
                                    out = speak(random.choice(_SEARCH_FILLERS))
                                elif not said and step_tools == {"look"}:
                                    out = speak(random.choice(_LOOK_FILLERS))
                                else:
                                    out = None
                                if out:
                                    yield out
                                step_text, step_tools = [], set()
                            elif node == "call_tool":
                                tools_used.add(getattr(val, "name", ""))
                                if turn.pending_intent and not intent_sent:
                                    # the whole answer: wire-pod sends this intent to Vector
                                    intent_sent = True
                                    cue = "{{doIntent||" + turn.pending_intent + "}}."
                                    log.info("[%s] answering with built-in behaviour %s", thread, turn.pending_intent)
                                    said.append(cue)
                                    yield _chunk(cid, model, cue)
                                if config.DEBUG:
                                    log.info("[%s] tool -> %s", thread, getattr(val, "content", val))
                            elif node == "vector_agent":
                                final = val
            except Exception as e:  # never leave wire-pod hanging mid-stream
                log.exception("agent error")
                fallback = "{{playAnimationWI||confused}} Hmm, my brain glitched for a second. Try me again?"
                yield _chunk(cid, model, fallback)
                said.append(fallback)
            turn.close_intent_slot()
            if final and _SILENT_WORD.match(str(final)):
                final = None                   # nothing worth saying (e.g. a silence turn)
                if text != SILENCE_TURN:
                    overheard = True
                    log.info("[%s] not for me (overheard?): staying quiet, not listening on", thread)
                if not said:
                    yield _chunk(cid, model, "{{playAnimationWI||dartingEyes}}")
            if not intent_sent and not _CUE.sub("", "".join(said)).strip() and final:  # nothing spoken by the LLM
                piece = clean(str(final))
                piece = await guard.check_speech(piece, f"{note}\n{text}") or piece
                yield _chunk(cid, model, piece)
                said.append(piece)
            # Deterministic conversation mode (see config.FOLLOW_UP_MODE): keep Vector
            # listening after he speaks so follow-ups don't need "Hey Vector".
            full = "".join(said)
            spoken = _CUE.sub("", full).strip()
            routine = (bool(tools_used & {"do_action", "learn_face"}) or PLAYER.has_pending()
                       or PLAYER.is_paused())  # music about to (re)start: don't keep listening  # don't listen while he dances etc.
            if config.FOLLOW_UP_MODE == "always":
                want = bool(spoken) and not routine
            elif config.FOLLOW_UP_MODE == "questions":
                want = spoken.endswith("?") and not routine
            else:
                want = False
            if want and "newVoiceRequest" not in full:
                cue = " {{newVoiceRequest||now}}"
                yield _chunk(cid, model, cue)
                said.append(cue)
            log.info("[%s] vector (%.2fs): %s", thread, time.time() - t0, "".join(said))
            _last_reply.update(text=spoken, t=time.time(), from_silence=text == SILENCE_TURN)
            activity.record("conversation", f"{note} heard: \"{text[:120]}\" -> I said: \"{spoken[:160]}\"")
            if text != SILENCE_TURN:                        # nobody said anything to remember
                heard = f"{note}\n{text}"
                _bg(_memory_pass(memory.tag_overheard(heard) if overheard else heard, spoken))
            yield _chunk(cid, model, None, "stop")
            yield "data: [DONE]\n\n"
            # Start music queued this turn, or resume music the wake word paused,
            # once he's said his (short) reply.
            PLAYER.turn_ended(2.0 + len(spoken) / 12)

    async def stop_fast():
        # "stop" while music plays: no LLM round-trip, just stop it
        await PLAYER.stop()
        log.info("fast stop: %r", text)
        yield _chunk(cid, model, "{{playAnimationWI||happy}} Okay, music off.")
        yield _chunk(cid, model, None, "stop")
        yield "data: [DONE]\n\n"

    async def capture_end():
        # an overhear capture (app/listen.py), not someone talking to him: no reply
        yield _chunk(cid, model, None, "stop")
        yield "data: [DONE]\n\n"

    if listen.is_capture(text):
        heard = listen.strip(text)
        scheduler.on_turn_start()        # wire-pod took behaviour control for the listen
        taken = listen.deliver(text)
        log.info("overheard%s: %s", "" if taken else " (nobody waiting, dropped)", heard[:200])
        if not (taken and listen.addressed_to_him(heard)):
            gen = capture_end()
        else:                            # "Hey Vector, ..." while he was already listening
            text = heard
            gen = run_stream()
    elif _is_stop(text) and PLAYER.active():
        gen = stop_fast()
    elif _is_silence(text) and followup and _promised_recently():
        log.info("silence after a promise (%r): letting him do what he said", _last_reply["text"][-80:])
        text = SILENCE_TURN
        gen = run_stream()
    else:
        gen = silent_end() if _is_silence(text) else run_stream()
    gen = _detached(gen, cid, model)
    if stream:
        return StreamingResponse(gen, media_type="text/event-stream")

    parts = []
    async for line in gen:
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

    servers = [uvicorn.Server(uvicorn.Config(app, host=config.LISTEN_HOST, port=config.LISTEN_PORT,
                                             log_level="warning"))]
    if config.A2A_TOKEN:
        from .a2a_server import build_app
        a2a_app = build_app(lambda: STATE["agent"])
        servers.append(uvicorn.Server(uvicorn.Config(a2a_app, host=config.A2A_HOST,
                                                     port=config.A2A_PORT, log_level="warning")))
        log.info("A2A server on %s:%s (card: %s/.well-known/agent-card.json)",
                 config.A2A_HOST, config.A2A_PORT, config.A2A_PUBLIC_URL)
    else:
        log.info("A2A disabled (no A2A_TOKEN)")

    async def serve_all():
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(serve_all())


if __name__ == "__main__":
    main()
