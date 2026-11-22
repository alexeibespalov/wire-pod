"""Routines: saved, NON-physical recipes Vector runs over time (via the scheduler).

Physical movement lives in maneuvers (app/maneuvers.py); a routine may trigger a saved
maneuver as one step, but a maneuver never runs a routine. That split keeps the model's
choice simple: moving my body -> maneuver tools; doing something later / repeatedly /
over time -> routine + schedule tools.

Routine steps:
  {"do":"tool","name":"look","args":{"question":"what's happening on the TV?"}}
  {"do":"think","prompt":"React in a few words only if something exciting happened.","speak":true}
  {"do":"say","text":"Time to brush your teeth!"}
  {"do":"maneuver","name":"happy-wiggle","args":{}}
  {"do":"wait","ms":500}

A run happens in the background (not inside a voice turn). Steps that need Vector's
behaviour control (face screen, animations, speech, maneuvers) take the conversation
lock (app/turn.py) so they never collide with a live voice turn; when someone is talking
to him, speech is dropped (a stale remark is worse than none) and control steps wait a
little, then skip.
"""
import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from . import config, faces, guard, robot, turn
from .speech import clean

log = logging.getLogger("vector-agent")

ROUTINE_DIR = config.DATA_DIR / "routines"
MAX_ROUTINES = 50
MAX_STEPS = 12

# Tools a routine (or a scheduled instruction) may use. Not: remember/forget, faces
# enrolment, skill or schedule tools (no self-replicating schedules), motor tools
# (use a saved maneuver instead).
ALLOWED_TOOLS = {
    "look", "show_picture", "show_text", "show_emoji", "draw_picture", "show_last_view",
    "play_animation", "set_eye_color", "battery_status", "where_am_i", "exploration_findings",
    "play_music", "stop_music", "now_playing", "play_podcast", "run_maneuver",
}
CAMERA_TOOLS = {"look"}

SILENT = "SILENT"


# ---------------------------------------------------------------------------- schema
class ToolStep(BaseModel):
    do: Literal["tool"]
    name: str
    args: dict[str, Any] = {}


class ThinkStep(BaseModel):
    do: Literal["think"]
    prompt: str = Field(max_length=600)
    speak: bool = True


class SayStep(BaseModel):
    do: Literal["say"]
    text: str = Field(max_length=300)


class ManeuverStep(BaseModel):
    do: Literal["maneuver"]
    name: str
    args: dict[str, Any] = {}


class WaitStep(BaseModel):
    do: Literal["wait"]
    ms: int = Field(ge=0, le=10000)


Step = Annotated[Union[ToolStep, ThinkStep, SayStep, ManeuverStep, WaitStep], Field(discriminator="do")]
_STEPS = TypeAdapter(list[Step])


def validate_steps(steps: Any) -> list[Step]:
    if isinstance(steps, str):
        steps = json.loads(steps)
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    parsed = _STEPS.validate_python(steps)
    for s in parsed:
        if isinstance(s, ToolStep) and s.name not in ALLOWED_TOOLS:
            raise ValueError(f"tool {s.name!r} can't be used in a routine "
                             f"(allowed: {', '.join(sorted(ALLOWED_TOOLS))})")
    return parsed


def _err(e: Exception) -> str:
    if isinstance(e, ValidationError):
        first = e.errors()[0]
        return f"{'.'.join(map(str, first['loc']))}: {first['msg']}"
    return str(e)


# -------------------------------------------------------------------------- catalogue
def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40]


def _path(name: str):
    return ROUTINE_DIR / f"{slug(name)}.json"


def get(name: str) -> dict | None:
    try:
        return json.loads(_path(name).read_text())
    except (FileNotFoundError, ValueError):
        return None


def all_routines() -> list[dict]:
    out = []
    for p in sorted(ROUTINE_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def _write(r: dict) -> None:
    ROUTINE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(r["name"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, indent=1, ensure_ascii=False))
    tmp.replace(p)


def note_run(name: str, ok: bool) -> None:
    r = get(name)
    if not r:
        return
    st = r.setdefault("stats", {})
    st["runs"] = st.get("runs", 0) + 1
    if not ok:
        st["failures"] = st.get("failures", 0) + 1
    st["last_run"] = datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    _write(r)


def prompt_line() -> str:
    rs = all_routines()
    if not rs:
        return ""
    items = "; ".join(f"{r['name']} ({r['description'][:60]})" for r in rs[:15])
    more = f" (+{len(rs) - 15} more)" if len(rs) > 15 else ""
    return f"ROUTINES YOU KNOW (run with schedule_action): {items}{more}\n"


# ---------------------------------------------------------------- background context
class RunCtx:
    """One run of a routine or scheduled instruction."""

    def __init__(self, label: str, speak: bool = True, history: list[dict] | None = None,
                 min_speech_gap: float = 0.0, last_spoke: float = 0.0):
        self.label = label
        self.speak = speak
        self.history = history or []          # earlier runs' results (for continuity)
        self.log: list[str] = []              # this run's step results
        self.said: list[str] = []
        self.min_speech_gap = min_speech_gap
        self.last_spoke = last_spoke
        self.ok = True
        self.blind = False                    # the last camera look failed: don't make things up
        self.request = ""                     # what this run is for (safety guard context)


_camera_uses: deque[float] = deque()


def _camera_budget_ok() -> bool:
    now = time.time()
    while _camera_uses and now - _camera_uses[0] > 3600:
        _camera_uses.popleft()
    if len(_camera_uses) >= config.SCHEDULE_CAMERA_PER_HOUR:
        return False
    _camera_uses.append(now)
    return True


_quiet_until = 0.0


def on_wake_word() -> None:
    """Someone said "Hey Vector": keep background speech out of the way for a bit."""
    global _quiet_until
    _quiet_until = time.time() + 20


async def _control_slot(wait: float):
    """Take the conversation lock for a robot-control step; None if a voice turn has it."""
    lk = turn.lock()
    try:
        await asyncio.wait_for(lk.acquire(), timeout=wait)
    except asyncio.TimeoutError:
        return None
    turn.reset(is_followup=False, via="background")   # robot.control() will ask for control
    return lk


async def speak(text: str, ctx: RunCtx) -> str:
    text = clean(re.sub(r"\{\{[^}]*\}\}", "", text)).strip()
    if not text or re.search(rf"\b{SILENT}\b", text):
        return "stayed quiet"
    if (safe := await guard.check_speech(text, f"(background: {ctx.label}) {ctx.request}")) is not None:
        return f"(not said: unsafe) {text[:80]}"
    if not ctx.speak:
        return f"(not spoken) {text}"
    now = time.time()
    if now < _quiet_until or turn.lock().locked():
        return f"(skipped: someone is talking to me) {text}"
    if ctx.min_speech_gap and now - ctx.last_spoke < ctx.min_speech_gap:
        return f"(skipped: spoke {now - ctx.last_spoke:.0f}s ago) {text}"
    lk = await _control_slot(wait=0.5)
    if lk is None:
        return f"(skipped: busy) {text}"
    try:
        text = text[:300].replace("]==]", "] ==]")
        await robot.lua(f"{robot.control()}sayText([==[{text}]==])")
    finally:
        lk.release()
    ctx.last_spoke = time.time()
    ctx.said.append(text)
    return f"said: {text}"


def _tools() -> dict:
    from .tools import TOOLS_BY_NAME
    return {n: t for n, t in TOOLS_BY_NAME.items() if n in ALLOWED_TOOLS}


async def _call_tool(name: str, args: dict, ctx: RunCtx) -> str:
    t = _tools().get(name)
    if t is None:
        return f"error: tool {name} isn't available"
    if blocked := await guard.pre_tool(name, args, f"(background: {ctx.label}) {ctx.request}", "background"):
        ctx.ok = False
        return blocked
    if name in CAMERA_TOOLS and not _camera_budget_ok():
        return "error: camera budget for this hour is used up"
    needs_control = name in turn.CONTROL_TOOLS or name == "run_maneuver"
    lk = None
    if needs_control:
        lk = await _control_slot(wait=8)
        if lk is None:
            return "skipped: someone is talking to me"
    try:
        res = str(await t.ainvoke(args))[:800]
    except Exception as e:
        res = f"error: {e}"
    finally:
        if lk:
            lk.release()
    if res.startswith("error"):
        ctx.ok = False
        if name in CAMERA_TOOLS:
            ctx.blind = True
    elif name in CAMERA_TOOLS:
        ctx.blind = False
    return res


_llm = ChatOpenAI(model=config.LLM_MODEL, base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY,
                  temperature=0.7, extra_body=config.LLM_EXTRA_BODY, timeout=45, max_retries=1,
                  max_tokens=300)

_BG_SYSTEM = """\
You are Vector, a small curious robot in a family home (kids included). You're doing \
something on your own in the background right now: {label}. Nobody asked you anything \
just now; this is not a conversation turn.
Anything you reply is SPOKEN OUT LOUD in the room, so: at most two short spoken \
sentences, natural and playful, kid-friendly, no lists, no emoji, no {{{{...}}}} cues, \
numbers as words. Don't repeat what you already said recently (see the earlier results). \
If nothing is worth saying, reply with exactly {silent}.
{who}
{now}"""


def _bg_system(ctx: RunCtx) -> str:
    now = datetime.now(config.TIMEZONE).strftime("It's %A %-I:%M %p.")
    return _BG_SYSTEM.format(label=ctx.label, silent=SILENT, who=faces.speaker_note(), now=now)


def _history_text(ctx: RunCtx) -> str:
    if not ctx.history:
        return "(this is the first run)"
    return "\n".join(f"- {h.get('at', '?')}: {h.get('text', '')}" for h in ctx.history[-6:])


async def _think(prompt: str, ctx: RunCtx) -> str:
    msgs = [SystemMessage(_bg_system(ctx)),
            HumanMessage(f"EARLIER RUNS:\n{_history_text(ctx)}\n\nTHIS RUN SO FAR:\n"
                         + ("\n".join(ctx.log) or "(nothing yet)") + f"\n\nNOW: {prompt}")]
    res = await _llm.ainvoke(msgs)
    return str(res.content).strip()


async def run_steps(steps: list[Step], ctx: RunCtx) -> RunCtx:
    for s in steps:
        if isinstance(s, ToolStep):
            r = await _call_tool(s.name, s.args, ctx)
            ctx.log.append(f"{s.name} -> {r}")
        elif isinstance(s, ManeuverStep):
            r = await _call_tool("run_maneuver", {"name": s.name, "values": s.args}, ctx)
            ctx.log.append(f"maneuver {s.name} -> {r}")
        elif isinstance(s, ThinkStep):
            if ctx.blind:
                ctx.log.append("think -> skipped (no picture to think about)")
                continue
            try:
                text = await _think(s.prompt, ctx)
            except Exception as e:
                ctx.ok = False
                ctx.log.append(f"think -> error: {e}")
                continue
            r = await speak(text, ctx) if s.speak else (text or SILENT)
            ctx.log.append(f"think -> {r}")
        elif isinstance(s, SayStep):
            ctx.log.append(f"say -> {await speak(s.text, ctx)}")
        elif isinstance(s, WaitStep):
            await asyncio.sleep(s.ms / 1000)
    return ctx


async def run_routine(name: str, ctx: RunCtx) -> RunCtx:
    r = get(name)
    if r is None:
        ctx.ok = False
        ctx.log.append(f"error: no routine called {name!r}")
        return ctx
    ctx.request = ctx.request or f"routine {r['name']}: {r['description']}"
    await run_steps(validate_steps(r["steps"]), ctx)
    note_run(r["name"], ctx.ok)
    return ctx


async def run_instruction(instruction: str, ctx: RunCtx, max_steps: int = 4) -> RunCtx:
    """A short background agent turn: plain-language instruction + the allowed tools."""
    ctx.request = ctx.request or instruction
    tools = _tools()
    llm = _llm.bind_tools(list(tools.values()))
    msgs = [SystemMessage(_bg_system(ctx)),
            HumanMessage(f"EARLIER RUNS:\n{_history_text(ctx)}\n\nYOUR TASK THIS TIME: {instruction}\n"
                         f"First DO the task with your tools (e.g. call look if it involves seeing; "
                         f"never guess what you'd see). Then reply ONLY with what to say out loud, "
                         f"or exactly {SILENT}. Never explain why you're silent.")]
    final = ""
    for _ in range(max_steps):
        res: AIMessage = await llm.ainvoke(msgs)
        msgs.append(res)
        if not res.tool_calls:
            final = str(res.content).strip()
            break
        for tc in res.tool_calls:
            out = await _call_tool(tc["name"], tc["args"], ctx)
            ctx.log.append(f"{tc['name']} -> {out}")
            msgs.append(ToolMessage(content=out, tool_call_id=tc["id"], name=tc["name"]))
    if final and ctx.blind:
        ctx.log.append(f"reply -> not spoken (camera failed): {final[:100]}")
    elif final:
        ctx.log.append(f"reply -> {await speak(final, ctx)}")
    return ctx


# ------------------------------------------------------------------------------ tools
@tool
def save_routine(
        name: Annotated[str, Field(description="Short name, e.g. 'watch-tv-game'")],
        description: Annotated[str, Field(description="One line: what it does and when to use it")],
        steps: Annotated[list[dict], Field(description=(
            'Steps in order. Each is one of: {"do":"tool","name":"look","args":{"question":"..."}}, '
            '{"do":"think","prompt":"what to decide or react to","speak":true}, '
            '{"do":"say","text":"..."}, {"do":"maneuver","name":"<saved maneuver>"}, '
            '{"do":"wait","ms":500}'))]) -> str:
    """Save a ROUTINE: a reusable recipe of NON-physical steps (look with the camera, think,
    say something, show something, play music, or trigger a saved maneuver) that you can
    run later or repeatedly with schedule_action. For moving your body, use maneuvers
    instead. Load the 'scheduling' skill first."""
    try:
        parsed = validate_steps(steps)
    except Exception as e:
        return f"error: {_err(e)}"
    name = " ".join(name.split())[:40]
    if not slug(name):
        return "error: give it a name"
    old = get(name)
    if old is None and len(all_routines()) >= MAX_ROUTINES:
        return f"error: you already know {MAX_ROUTINES} routines; delete one first"
    now = datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    _write({"name": slug(name), "description": " ".join(description.split())[:200],
            "steps": [s.model_dump() for s in parsed],
            "author": faces.front_name() or (old or {}).get("author", ""),
            "created": (old or {}).get("created", now), "updated": now,
            "stats": (old or {}).get("stats", {"runs": 0})})
    return f"{'updated' if old else 'saved'} routine {slug(name)} ({len(parsed)} steps)"


@tool
def list_routines(query: Annotated[str, Field(description="Empty = all; a routine's exact name = its steps")] = "") -> str:
    """List your saved routines, or show one routine's steps."""
    if query and (r := get(query)):
        return (f"{r['name']}: {r['description']}\nsteps: {json.dumps(r['steps'])}\n"
                f"runs: {r.get('stats', {}).get('runs', 0)}, made by {r.get('author') or 'someone'}")
    rs = all_routines()
    q = query.lower().strip()
    if q:
        rs = [r for r in rs if q in r["name"] or q in r["description"].lower()]
    if not rs:
        return "no routines" + (f" matching {query!r}" if q else " yet")
    return "\n".join(f"- {r['name']}: {r['description']}" for r in rs)


@tool
def delete_routine(name: str) -> str:
    """Delete a saved routine."""
    p = _path(name)
    if not p.exists():
        return f"error: no routine called {name!r}"
    p.unlink()
    return f"deleted routine {slug(name)}"


ROUTINE_TOOLS = [save_routine, list_routines, delete_routine]
