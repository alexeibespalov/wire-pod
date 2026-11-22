"""Scheduled actions: run a routine or a plain-language instruction later, at a time,
every day, or every N seconds for a while ("watch me play" -> look at the TV every 5 s
for half an hour and react).

Schedules persist in DATA_DIR/schedules.json and survive restarts. One background loop
(started from the server lifespan) checks once a second and starts due runs, each in
its own task; a run that's still going when the next one is due is skipped, not stacked.

Limits (config): at most SCHEDULE_MAX_ACTIVE active schedules, intervals >= SCHEDULE_MIN_EVERY_S,
interval schedules last SCHEDULE_DEFAULT_MINUTES by default and SCHEDULE_MAX_MINUTES at most,
camera looks are budgeted per hour (app/routines.py), and a schedule that keeps failing
is stopped. Background speech: at most one remark per SCHEDULE_SPEECH_GAP_S per schedule,
never while someone is talking to him.

hold_still: keeps Vector's behaviour control (wire-pod /api-sdk) between runs so he doesn't
wander off what he's watching. Dropped on the wake word / a voice turn, re-taken at the
next run, released when the schedule ends.
"""
import asyncio
import contextvars
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import config, faces, robot, routines, turn

log = logging.getLogger("vector-agent")

_FILE = config.DATA_DIR / "schedules.json"
_schedules: list[dict] = []
_running: dict[str, asyncio.Task] = {}
_loop: asyncio.AbstractEventLoop | None = None
_held = False                 # we (still) hold behaviour control for a hold_still schedule
_took_hold = False            # we took it at some point and haven't released it since
_last_spoke: dict[str, float] = {}


# ------------------------------------------------------------------------- storage
def _load() -> None:
    global _schedules
    try:
        _schedules = json.loads(_FILE.read_text())
    except (FileNotFoundError, ValueError):
        _schedules = []


def _save() -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_schedules, indent=1, ensure_ascii=False))
    tmp.replace(_FILE)


def active() -> list[dict]:
    return [s for s in _schedules if s.get("status") == "active"]


# --------------------------------------------------------------------------- timing
_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", re.I)


def _parse_clock(text: str) -> tuple[int, int] | None:
    m = _TIME.match(text or "")
    if not m:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm.startswith("p") and h < 12:
        h += 12
    if ampm.startswith("a") and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return h, mi


def _next_clock(h: int, mi: int, after: datetime | None = None) -> float:
    now = after or datetime.now(config.TIMEZONE)
    t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.TIMEZONE).strftime("%-I:%M %p").lower()


def _describe(s: dict) -> str:
    what = f"routine {s['routine']}" if s.get("routine") else f"\"{s['instruction'][:80]}\""
    if s.get("every_s"):
        when = f"every {s['every_s']:g}s" + (f" until {_clock(s['until'])}" if s.get("until") else "")
    elif s.get("daily_at"):
        when = f"every day at {s['daily_at']}"
    else:
        when = f"once at {_clock(s['next_at'])}"
    by = f", for {s['by']}" if s.get("by") else ""
    return f"#{s['id']} {s['title']}: {what}, {when}{by} ({s.get('runs', 0)} runs so far)"


# ---------------------------------------------------------------------------- runs
async def _ensure_hold() -> None:
    global _held, _took_hold
    if _held or turn.lock().locked():
        return
    try:
        await robot.sdk("assume_behavior_control", priority="default")
        _held = _took_hold = True
        log.info("schedule: holding still (behaviour control)")
    except Exception as e:
        log.warning("schedule: couldn't hold still: %s", e)


async def _release_hold() -> None:
    """Release once no hold_still schedule is left (even if a voice turn took it meanwhile:
    wire-pod's hold may still be registered)."""
    global _held, _took_hold
    if not _took_hold or any(s.get("hold_still") for s in active()):
        return
    _held = _took_hold = False
    try:
        await robot.sdk("release_behavior_control")
        log.info("schedule: released behaviour control")
    except Exception as e:
        log.warning("schedule: release failed: %s", e)


def on_wake_word() -> None:
    """From the SDK thread: wire-pod is about to talk, our hold is gone anyway."""
    global _held
    _held = False


def on_turn_start() -> None:
    global _held
    _held = False


async def _run(s: dict) -> None:
    sid = s["id"]
    if s.get("hold_still"):
        await _ensure_hold()
    ctx = routines.RunCtx(label=s["title"], speak=s.get("speak", True),
                          history=s.get("results", []),
                          min_speech_gap=config.SCHEDULE_SPEECH_GAP_S if s.get("every_s") else 0,
                          last_spoke=_last_spoke.get(sid, 0.0))
    t0 = time.time()
    try:
        if s.get("routine"):
            await routines.run_routine(s["routine"], ctx)
        else:
            await routines.run_instruction(s["instruction"], ctx)
    except Exception as e:
        ctx.ok = False
        ctx.log.append(f"error: {e}")
        log.exception("schedule #%s failed", sid)
    _last_spoke[sid] = ctx.last_spoke
    summary = " | ".join(ctx.log)[-400:] or "(nothing happened)"
    log.info("schedule #%s run %d (%.1fs): %s", sid, s.get("runs", 0) + 1, time.time() - t0, summary)
    s["runs"] = s.get("runs", 0) + 1
    s["last_run"] = t0
    s["errors"] = 0 if ctx.ok else s.get("errors", 0) + 1
    s.setdefault("results", []).append({"at": _clock(t0), "text": summary[-240:]})
    s["results"] = s["results"][-20:]
    if s["errors"] >= 5:
        s["status"] = "failed"
        log.warning("schedule #%s stopped after 5 failures in a row", sid)
        if s.get("speak", True):
            why = "my camera isn't giving me pictures" if ctx.blind else "it kept going wrong"
            await routines.speak(f"I stopped {s['title']}, because {why}.",
                                 routines.RunCtx(label=s["title"]))
    _reschedule(s, t0)
    _save()
    if s["status"] != "active" and s.get("hold_still"):
        await _release_hold()


def _reschedule(s: dict, started: float) -> None:
    now = time.time()
    if s.get("max_runs") and s["runs"] >= s["max_runs"]:
        s["status"] = "done"
    elif s.get("every_s"):
        s["next_at"] = max(started + s["every_s"], now + 1)
        if s.get("until") and s["next_at"] > s["until"]:
            s["status"] = "done"
    elif s.get("daily_at"):
        h, mi = _parse_clock(s["daily_at"])
        s["next_at"] = _next_clock(h, mi)
    else:
        s["status"] = "done"


async def _tick_forever() -> None:
    while True:
        try:
            now = time.time()
            for s in active():
                if s.get("until") and now > s["until"] and s["id"] not in _running:
                    s["status"] = "done"
                    _save()
                    if s.get("hold_still"):
                        await _release_hold()
                    continue
                if now >= s.get("next_at", 0) and s["id"] not in _running:
                    t = asyncio.get_running_loop().create_task(_run(s), context=contextvars.Context())
                    _running[s["id"]] = t
                    t.add_done_callback(lambda _t, sid=s["id"]: _running.pop(sid, None))
        except Exception:
            log.exception("schedule: loop error")
        await asyncio.sleep(1)


def start() -> None:
    global _loop
    _load()
    _loop = asyncio.get_running_loop()
    n = len(active())
    # interval schedules that ended while we were down: close them
    for s in active():
        if s.get("until") and time.time() > s["until"]:
            s["status"] = "done"
    _save()
    _loop.create_task(_tick_forever())
    log.info("schedule: %d active schedule(s) restored", n)


def prompt_section() -> str:
    acts = active()
    if not acts:
        return ""
    lines = ["WHAT YOU'RE DOING RIGHT NOW (scheduled actions running in the background; "
             "cancel_schedule stops one)"]
    for s in acts:
        lines.append("- " + _describe(s))
        for r in s.get("results", [])[-3:]:
            lines.append(f"    {r['at']}: {r['text'][-200:]}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- tools
@tool
async def schedule_action(
        routine: Annotated[str, Field(description="Name of a saved routine to run. Use this OR instruction.")] = "",
        instruction: Annotated[str, Field(description=(
            "OR plain words for what to do each time, e.g. \"Look at the TV. If something exciting "
            "happened in the game since last time, react in a few words, otherwise stay quiet.\""))] = "",
        every_seconds: Annotated[float, Field(description="Repeat every N seconds (min 5). 0 = not repeating.")] = 0,
        for_minutes: Annotated[float, Field(description="How long a repeating action lasts (default 30, max 180)")] = 0,
        in_seconds: Annotated[float, Field(description="Run once after this many seconds")] = 0,
        at_time: Annotated[str, Field(description="Run once at a clock time today/tomorrow, e.g. '7:30 pm'")] = "",
        daily_at: Annotated[str, Field(description="Run every day at this time, e.g. '8:00 am'")] = "",
        speak: Annotated[bool, Field(description="Say results out loud (false = just keep notes)")] = True,
        hold_still: Annotated[bool, Field(description="Stay put between runs (e.g. keep watching the TV)")] = False,
        title: Annotated[str, Field(description="Short label, e.g. 'watching Leo play Mario'")] = "") -> str:
    """Schedule something to happen LATER, REPEATEDLY or OVER TIME: a saved routine or a
    plain-language instruction. Examples: watch a video game (every 5 s for 30 min), remind
    someone at 7 pm, say good morning every day at 8. With no timing it runs once right now
    in the background. For moving your body right now, use maneuvers instead. Load the
    'scheduling' skill first."""
    routine, instruction = routine.strip(), " ".join(instruction.split())[:600]
    if bool(routine) == bool(instruction):
        return "error: give either a routine name or an instruction (one of them)"
    if routine and routines.get(routine) is None:
        return f"error: no routine called {routine!r} (list_routines shows them)"
    if len(active()) >= config.SCHEDULE_MAX_ACTIVE:
        return (f"error: {config.SCHEDULE_MAX_ACTIVE} things are already scheduled; cancel one first "
                f"(list_schedules)")
    now = time.time()
    s = {"id": uuid.uuid4().hex[:4], "routine": routines.slug(routine) if routine else "",
         "instruction": instruction, "speak": bool(speak), "hold_still": bool(hold_still),
         "by": faces.front_name(), "created": now, "status": "active", "runs": 0, "results": []}
    if every_seconds:
        if every_seconds < config.SCHEDULE_MIN_EVERY_S:
            return f"error: every_seconds must be at least {config.SCHEDULE_MIN_EVERY_S:g}"
        mins = min(for_minutes or config.SCHEDULE_DEFAULT_MINUTES, config.SCHEDULE_MAX_MINUTES)
        s.update(every_s=float(every_seconds), next_at=now + max(in_seconds, 0), until=now + mins * 60)
    elif daily_at:
        hm = _parse_clock(daily_at)
        if hm is None:
            return "error: daily_at must look like '8:00 am' or '20:15'"
        s.update(daily_at=f"{hm[0]:02d}:{hm[1]:02d}", next_at=_next_clock(*hm))
    elif at_time:
        hm = _parse_clock(at_time)
        if hm is None:
            return "error: at_time must look like '7:30 pm' or '19:30'"
        s.update(next_at=_next_clock(*hm), max_runs=1)
    else:
        s.update(next_at=now + max(in_seconds, 0), max_runs=1)
    s["title"] = " ".join(title.split())[:60] or (routine or instruction[:40])
    _schedules.append(s)
    _schedules[:] = [x for x in _schedules if x["status"] == "active"] + \
        [x for x in _schedules if x["status"] != "active"][-30:]   # keep a little history
    _save()
    first = s["next_at"] - now
    starts = "starting now" if first < 2 else f"first run at {_clock(s['next_at'])}"
    return f"scheduled {_describe(s)}; {starts}"


@tool
def list_schedules() -> str:
    """List what's scheduled or running in the background right now."""
    acts = active()
    if not acts:
        return "nothing is scheduled"
    return "\n".join(_describe(s) + (f"; next {_clock(s['next_at'])}" if not s.get("every_s") else "")
                     for s in acts)


@tool
async def cancel_schedule(which: Annotated[str, Field(description=(
        "The schedule's id (e.g. '3fa2'), words from its title (e.g. 'watching'), or 'all'"))]) -> str:
    """Stop a scheduled action ("stop watching", "cancel the reminder")."""
    w = which.strip().lower().lstrip("#")
    hits = [s for s in active() if w == "all" or w == s["id"] or (w and w in s["title"].lower())
            or (w and w in (s.get("routine") or "")) or (w and w in s.get("instruction", "").lower())]
    if not hits:
        acts = active()
        if len(acts) == 1 and w in ("it", "that", "this", "watching", "the schedule", ""):
            hits = acts
        else:
            return f"error: nothing scheduled matches {which!r}" + (
                f"; scheduled: {', '.join('#' + s['id'] + ' ' + s['title'] for s in acts)}" if acts else "")
    for s in hits:
        s["status"] = "cancelled"
        t = _running.get(s["id"])
        if t and not t.done():
            t.cancel()
    _save()
    if any(s.get("hold_still") for s in hits):
        await _release_hold()
    return "cancelled " + ", ".join(f"#{s['id']} {s['title']}" for s in hits)


SCHEDULE_TOOLS = [schedule_action, list_schedules, cancel_schedule]
