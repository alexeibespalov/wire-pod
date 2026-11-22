"""Body programs: Vector writes a small Python program that drives his body in a closed loop
("turn until the cube is centred, then creep up to 30 mm"), using Claude's programmatic
tool calling. Claude-only (LLM_PROVIDER=anthropic).

How it works (official `anthropic` SDK, Messages API):
* tools = the code execution tool + a FLAT set of body functions marked
  allowed_callers=["code_execution_20260120"] (the recursive `move` schema can't be used
  there). Claude writes one script; each `await drive({...})` pauses the script, comes back
  to us as a tool_use block (with a `caller`), we run it on the real robot and return a
  string; the script continues with it. Only the script's final output returns to Claude,
  so a 30-step loop costs one or two model calls instead of thirty.
* Results are JSON strings (the script parses them).
* Same rules as practice: every call goes through the safety guard (app/guard.py), motions
  wait until nobody is talking and hold the control slot, the body governor (app/body.py)
  still applies, and "Hey Vector" stops the program at once.
* It runs in the background: the voice turn answers straight away, the result is spoken
  when the program ends and written to the activity journal.
"""
import asyncio
import json
import logging
import time
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import activity, body, config, guard, routines, turn

log = logging.getLogger("vector-agent")

CODE_TOOL = "code_execution_20260120"
MAX_CALLS = 80            # body/sensor calls per program
MAX_ROUNDS = 40           # model round trips per program
DEFAULT_SECONDS, MAX_SECONDS = 90, 300

_task: asyncio.Task | None = None
_loop: asyncio.AbstractEventLoop | None = None
_last: dict = {}

_num = {"type": "number"}
_BODY_TOOLS = [
    {"name": "drive", "description": (
        "Drive straight. mm: + forward / - back (max 300 per call). speed: mm/s 10-120 (default 60). "
        "stop_below_mm: optional, stop early when the distance sensor reads closer than this. "
        "Returns JSON: what really happened, e.g. {\"moved_mm\": 39, \"turned_deg\": 0, "
        "\"prox_start_mm\": 316, \"prox_end_mm\": 120, \"stopped\": \"...\"}, or {\"error\": ...}."),
     "input_schema": {"type": "object", "properties": {"mm": _num, "speed": _num, "stop_below_mm": _num},
                      "required": ["mm"]}},
    {"name": "turn", "description": "Turn on the spot. deg: + left / - right. Returns JSON like drive.",
     "input_schema": {"type": "object", "properties": {"deg": _num}, "required": ["deg"]}},
    {"name": "wheels", "description": (
        "Run each wheel at its own speed for a time (curves, wiggles). left/right: mm/s (-120..120), "
        "ms: duration (max 3000). Returns JSON like drive."),
     "input_schema": {"type": "object", "properties": {"left": _num, "right": _num, "ms": _num},
                      "required": ["left", "right", "ms"]}},
    {"name": "head", "description": "Tilt the head. deg: -22 (down) to 45 (up). Returns JSON like drive.",
     "input_schema": {"type": "object", "properties": {"deg": _num}, "required": ["deg"]}},
    {"name": "lift", "description": "Move the lift (arms). height: 0 (down) to 1 (up). Returns JSON like drive.",
     "input_schema": {"type": "object", "properties": {"height": _num}, "required": ["height"]}},
    {"name": "sense", "description": (
        "Read the sensors now (nothing moves). Returns JSON: x_mm, y_mm, heading_deg (position "
        "since waking), head_deg, lift_mm, prox_mm (distance sensor, straight ahead), "
        "prox_quality (0..1, trust it above ~0.5), object_ahead, on_charger, picked_up, cliff, "
        "touched, cube_visible (camera sees the cube right now)."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "look", "description": (
        "Take a photo and ask a question about it (slow: ~3 s, use sparingly, at most 6 per "
        "program). head: 'as is', 'up', 'level' or 'down'. Returns the answer as text, e.g. "
        "'The green block is slightly left of centre, about 20 cm away.'"),
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string"},
         "head": {"type": "string", "enum": ["as is", "up", "level", "down"]}},
         "required": ["question"]}},
    {"name": "run_maneuver", "description": (
        "Run one of your saved maneuvers by name, optionally with values for its params. "
        "Returns JSON like drive."),
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "values": {"type": "object"}}, "required": ["name"]}},
]
for _t in _BODY_TOOLS:
    _t["allowed_callers"] = [CODE_TOOL]
MOTION = {"drive", "turn", "wheels", "head", "lift", "run_maneuver"}

_SYSTEM = """\
You control Vector, a palm-sized home robot (wheels, a head that tilts, a lift arm, a \
camera, a distance sensor that looks straight ahead), by writing ONE Python program with \
the code_execution tool. In that program, call your body functions with await: each takes \
one dict and returns a string (JSON for the body/sensor ones: use json.loads), e.g.
    r = json.loads(await turn({"deg": 15}))
    s = json.loads(await sense({}))
Rules:
- Work in a loop: sense or look, make a SMALL move, check the result, adjust. Keep loops \
bounded (at most 30 iterations) and stop as soon as the goal is reached.
- Units: mm and degrees (+ is forward / left). Prefer small moves near objects: 10-40 mm, \
5-20 degrees, speed 30-60.
- If a result has "error" or says it was aborted/stopped (picked up, cliff, bumped, too \
far), stop the program.
- look() is slow: use sense() (prox_mm, cube_visible) where it's enough.
- Never move toward people or pets, never push things off a table edge.
- At the end, print one short line saying what happened (with numbers), then reply to me \
in one or two plain spoken sentences, kid-friendly, no lists, no markdown."""


def _jsonable(r: dict) -> str:
    """The measured result of one motion (maneuvers.run_steps) as JSON for the script."""
    if "error" in r:
        return json.dumps({"error": str(r["error"])[:200]})
    out = {"moved_mm": round(r.get("disp_mm") or 0), "turned_deg": round(r.get("heading_deg") or 0),
           "prox_start_mm": r.get("prox_before"), "prox_end_mm": r.get("prox_after"),
           "duration_s": round(r.get("duration_s") or 0, 1)}
    if r.get("stop_reason"):
        out["stopped"] = r["stop_reason"]
    if r.get("notes"):
        out["notes"] = "; ".join(r["notes"])[:200]
    if r.get("cube_moved"):
        out["cube_moved"] = True
    return json.dumps(out)


def _sense_json() -> str:
    s = body.snapshot()
    if not s.get("ts"):
        return json.dumps({"error": "no sensor data yet"})
    st = s["status"]
    return json.dumps({
        "x_mm": round(s["x"]), "y_mm": round(s["y"]), "heading_deg": round(s["heading_deg"]),
        "head_deg": round(s["head_deg"]), "lift_mm": round(s["lift_mm"]),
        "prox_mm": s["prox_mm"], "prox_quality": round(s["prox_quality"], 2),
        "object_ahead": bool(s["prox_found"]) and not s["prox_lift"],
        "on_charger": bool(st & body.ST_ON_CHARGER), "picked_up": bool(st & body.ST_PICKED_UP),
        "cliff": bool(st & body.ST_CLIFF), "touched": bool(s["touched"]),
        "cube_visible": bool(s["cube_visible"]), "sensor_age_s": round(s["age_s"] or 0, 1)})


def _step(name: str, a: dict) -> dict:
    n = lambda k, d=None: a.get(k, d)  # noqa: E731
    if name == "drive":
        st = {"step": "drive", "mm": n("mm", 0), "speed": n("speed", 60)}
        if n("stop_below_mm"):
            st["stop_when"] = {"prox_below_mm": n("stop_below_mm")}
        return st
    if name == "turn":
        return {"step": "turn", "deg": n("deg", 0)}
    if name == "wheels":
        return {"step": "wheels", "left": n("left", 0), "right": n("right", 0), "ms": n("ms", 500)}
    if name == "head":
        return {"step": "head", "deg": n("deg", 0)}
    return {"step": "lift", "height": n("height", 0)}


async def _call(name: str, args: dict, run: dict) -> str:
    from .maneuvers import run_steps
    from .tools import TOOLS_BY_NAME
    run["calls"] += 1
    if run["calls"] > MAX_CALLS:
        return json.dumps({"error": f"call limit reached ({MAX_CALLS}); finish now"})
    if not isinstance(args, dict):
        args = {}
    if blocked := await guard.pre_tool(name, args,
                                       f"(body program) goal: {run['task']}", "program"):
        return json.dumps({"error": blocked})
    if name == "sense":
        return _sense_json()
    if name == "look":
        run["looks"] += 1
        if run["looks"] > 6:
            return "error: no more looks this program; use sense()"
        return str(await TOOLS_BY_NAME["look"].ainvoke(
            {"question": str(args.get("question", "What do you see?")), "head": args.get("head", "as is")}))[:600]
    if name not in MOTION:
        return json.dumps({"error": f"no function {name}"})
    while turn.lock().locked() or time.time() < routines._quiet_until:   # someone is talking
        await asyncio.sleep(0.5)
    lk = await routines._control_slot(wait=30)
    if lk is None:
        return json.dumps({"error": "someone is talking to me; stop"})
    try:
        if name == "run_maneuver":
            out = await TOOLS_BY_NAME["run_maneuver"].ainvoke(
                {"name": str(args.get("name", "")), "values": args.get("values") or {}})
            return json.dumps({"result": str(out)[:400]})
        r = await run_steps([_step(name, args)], label=f"program:{name}")
        return _jsonable(r)
    except Exception as e:
        return json.dumps({"error": str(e)[:200]})
    finally:
        lk.release()


def _text(content) -> str:
    return " ".join(b.text for b in content if getattr(b, "type", "") == "text").strip()


async def _run(run: dict) -> None:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=180, max_retries=1)
    tools = [{"type": CODE_TOOL, "name": "code_execution"}, *_BODY_TOOLS]
    messages: list[dict] = [{"role": "user", "content": f"Goal: {run['task']}"}]
    container = None
    try:
        for _ in range(MAX_ROUNDS):
            kw = {"container": container} if container else {}
            resp = await client.messages.create(
                model=config.PROGRAM_MODEL, max_tokens=8000, system=_SYSTEM, tools=tools,
                messages=messages, output_config={"effort": config.LLM_BACKGROUND_EFFORT}, **kw)
            if getattr(resp, "container", None):
                container = resp.container.id
            messages.append({"role": "assistant", "content": resp.content})   # unchanged (thinking too)
            if resp.stop_reason == "tool_use":
                results = []
                for b in resp.content:
                    if b.type == "tool_use":            # programmatic (or direct) call of a body function
                        out = await _call(b.name, b.input or {}, run)
                        run["log"].append(f"{b.name}({json.dumps(b.input)[:80]}) -> {out[:120]}")
                        results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
                messages.append({"role": "user", "content": results})    # only tool_result blocks
                continue
            if resp.stop_reason == "pause_turn":         # long server-side run: let it continue
                continue
            run["result"] = _text(resp.content) or ("(refused)" if resp.stop_reason == "refusal" else "")
            break
        else:
            run["result"] = "That took too many steps, so I stopped."
        run["status"] = "done"
    except asyncio.CancelledError:
        run["status"] = "stopped"
        run["result"] = run.get("result") or "Okay, I stopped."
        raise
    except Exception as e:
        log.exception("program failed")
        run["status"] = "failed"
        run["result"] = f"My program went wrong ({type(e).__name__})."
    finally:
        run["ended"] = time.time()
        activity.record("program", f"{run['status']}: {run['task'][:60]} ({run['calls']} body calls) -> "
                                   f"{run['result'][:160]}")
        log.info("program %s: %s -> %s", run["status"], run["task"], run["result"])
        if run["status"] != "stopped" and run["result"]:
            try:
                await routines.speak(run["result"], routines.RunCtx("body program"))
            except Exception as e:
                log.warning("program: couldn't say the result (%s)", e)


def active() -> bool:
    return _task is not None and not _task.done()


def on_wake_word() -> None:
    """Called from the face-watcher thread: "Hey Vector" stops a running program."""
    if active() and _loop is not None:
        log.info("program: stopped by the wake word")
        _loop.call_soon_threadsafe(_task.cancel)


def prompt_section() -> str:
    if active():
        return f"- Running a body program: {_last.get('task', '')[:80]} ({_last.get('calls', 0)} body calls so far)\n"
    if _last and time.time() - _last.get("ended", 0) < 600:
        return f"- Body program just {_last['status']}: {_last.get('result', '')[:120]}\n"
    return ""


@tool
async def run_program(
        task: Annotated[str, Field(description=(
            "What to achieve with your body, with the stopping condition, e.g. 'Turn until the "
            "green block is straight ahead, then drive up to it and stop 3 cm away.'"))],
        seconds: Annotated[int, Field(description="Time limit, default 90, max 300")] = DEFAULT_SECONDS) -> str:
    """Write and run a small program that moves your body in a loop using your sensors and
    camera (sense -> small move -> check -> adjust) until a goal is reached. Use it for
    things that need many small corrections: lining up with something, driving up to an
    object, following a line of objects, searching by turning. It runs in the background
    and you say the result when it's done. For a fixed gesture or a saved trick use
    move / run_maneuver instead."""
    global _task, _loop, _last
    if not config.ANTHROPIC:
        return "error: body programs need the Claude brain (LLM_PROVIDER=anthropic)"
    if active():
        return f"error: already running a program ({_last.get('task', '')[:60]}); wait or say stop"
    seconds = max(10, min(int(seconds or DEFAULT_SECONDS), MAX_SECONDS))
    _last = {"task": task.strip()[:400], "calls": 0, "looks": 0, "log": [], "status": "running",
             "started": time.time(), "result": ""}
    _loop = asyncio.get_running_loop()

    async def bounded():
        try:
            await asyncio.wait_for(_run(_last), timeout=seconds)
        except (TimeoutError, asyncio.TimeoutError):
            _last["status"], _last["result"] = "timed out", "I ran out of time on that one."

    _task = _loop.create_task(bounded())
    activity.record("program", f"started: {_last['task'][:100]}")
    return ("started the program; it runs right after you finish talking. Tell them in a few "
            "words what you're going to do. The result is said out loud when it's done.")


@tool
async def stop_program() -> str:
    """Stop the body program that's running."""
    if not active():
        return "no program is running"
    _task.cancel()
    return "stopped the program"


PROGRAM_TOOLS = [run_program, stop_program]
