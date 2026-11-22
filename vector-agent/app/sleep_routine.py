"""Sleep routine: a small framework for chores Vector does while he is asleep.

Never wakes him: jobs run on the agent's CPU/network only. They must not take behaviour
control, play audio, animate or move him (the only robot link used here is the listen-only
SDK connection from app/faces.py, to read his state).

Detecting sleep (see `asleep()`):
  Primary: RobotState.status has CALM_POWER_MODE (0x400) for SLEEP_CALM_SECONDS, he is not
    held/picked up, and nobody has talked to him for SLEEP_QUIET_MINUTES.
    Why it works: in WireOS calm power mode is part of "power save", requested via
    SmartRequestPowerSaveMode() by BehaviorSleeping (engine/.../sleeping/behaviorSleeping.cpp,
    the state the SleepCycle behaviour ends in); only quiet mode, ask-for-help and onboarding
    request it as well. Observed on the real robot: asleep on the charger = status 1056512
    (calm | on charger | head/lift in place | ...).
  Fallback (SLEEP_FALLBACK): on the charger, still, no conversation for
    SLEEP_FALLBACK_IDLE_MINUTES and (if SLEEP_FALLBACK_NIGHT_ONLY) inside the night hours.
While a session runs it is re-checked every 2 s; the moment he is no longer asleep the
running job is cancelled (jobs must therefore be cancellation-safe and idempotent).

Framework:
    @job("name", order=10, budget_s=600, has_work=callable)   # lower order runs first
    async def my_job(ctx): ...            # ctx.log, ctx.state (persisted dict), ctx.alive(), ctx.sleep(s)
Jobs share one session budget (SLEEP_SESSION_BUDGET_S) and each has its own time budget.
Per-job results and bookkeeping persist in DATA_DIR/sleep/state.json.

First job `analyze_explore_photos`: send each unanalysed exploration photo (app/explore_capture)
to the vision model and append the result to DATA_DIR/explore/analyses.jsonl:
  {"id": <capture id>, "analyzed_at", "model", "caption", "objects": [str], "setting"}
"""
import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Awaitable, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import Field

from . import llm as llms
from . import config, faces

log = logging.getLogger("vector-agent")

ST_PICKED_UP = 0x8
ST_PATHING = 0x80
ST_CALM = 0x400
ST_ON_CHARGER = 0x1000
ST_WHEELS = 0x8000
ST_HELD = 0x10000

SLEEP_DIR = config.DATA_DIR / "sleep"
_STATE_FILE = SLEEP_DIR / "state.json"
CAPTURES = config.EXPLORE_DIR / "captures.jsonl"
ANALYSES = config.EXPLORE_DIR / "analyses.jsonl"
_THREAD_FILE = config.DATA_DIR / "thread.json"


# ============================================================ sleep detection
class _Robot:
    """Latest robot state, written by the SDK thread (plain attribute stores), read anywhere."""
    status = 0
    t = 0.0               # when the last robot_state arrived
    calm_since = 0.0      # since when status has had CALM (0 = not calm)
    charger_since = 0.0
    last_motion = 0.0


_r = _Robot()
_last_activity = 0.0       # last conversation/interaction with the agent


def note_activity() -> None:
    """Call whenever someone talks to Vector (voice or A2A): resets the quiet timers."""
    global _last_activity
    _last_activity = time.time()


def _subscribe(r) -> None:
    from anki_vector.events import Events
    r.events.subscribe(_on_state, Events.robot_state)


def _on_state(_robot, _type, msg, *_a, **_k) -> None:
    now = time.time()
    st = int(msg.status)
    if now - _r.t > 15:                      # reconnect: forget old timers
        _r.calm_since = _r.charger_since = 0.0
    _r.t = now
    _r.status = st
    if st & ST_CALM:
        _r.calm_since = _r.calm_since or now
    else:
        _r.calm_since = 0.0
    if st & ST_ON_CHARGER:
        _r.charger_since = _r.charger_since or now
    else:
        _r.charger_since = 0.0
    if st & (ST_PATHING | ST_WHEELS):
        _r.last_motion = now


def _in_night(now_dt: datetime) -> bool:
    h, a, b = now_dt.hour, config.SLEEP_NIGHT_START, config.SLEEP_NIGHT_END
    return (h >= a or h < b) if a > b else (a <= h < b)


def asleep(now: float | None = None) -> tuple[bool, str]:
    """(is he asleep right now, why/why not)"""
    now = now or time.time()
    if now - _r.t > 15:
        return False, "no live robot state (SDK not connected)"
    st = _r.status
    if st & (ST_HELD | ST_PICKED_UP):
        return False, "being held / picked up"
    quiet_for = now - _last_activity
    if st & ST_CALM:
        if now - _r.calm_since < config.SLEEP_CALM_SECONDS:
            return False, f"calm power mode only {now - _r.calm_since:.0f}s so far"
        if quiet_for < config.SLEEP_QUIET_MINUTES * 60:
            return False, f"talked to {quiet_for / 60:.0f} min ago"
        return True, f"calm power mode for {now - _r.calm_since:.0f}s (WireOS sleep/power-save)"
    if config.SLEEP_FALLBACK and st & ST_ON_CHARGER:
        idle = config.SLEEP_FALLBACK_IDLE_MINUTES * 60
        if quiet_for >= idle and now - _r.charger_since >= idle and now - _r.last_motion >= idle:
            if not config.SLEEP_FALLBACK_NIGHT_ONLY or _in_night(datetime.now(config.TIMEZONE)):
                return True, f"fallback: on charger and idle {idle / 60:.0f}+ min"
            return False, "on charger and idle, but not night time"
    return False, "awake (not in calm power mode)"


# ============================================================ job framework
@dataclass
class Job:
    name: str
    order: int
    budget_s: float
    fn: Callable[["Ctx"], Awaitable[dict | None]]
    has_work: Callable[[], bool] | None = None


JOBS: dict[str, Job] = {}


def job(name: str, order: int = 100, budget_s: float = 600, has_work: Callable[[], bool] | None = None):
    """Register an async sleep job. `has_work` (cheap, sync) lets the scheduler skip a
    session when there is nothing to do. The job gets a Ctx and returns an optional
    summary dict (stored as its last result)."""
    def deco(fn):
        JOBS[name] = Job(name, order, budget_s, fn, has_work)
        return fn
    return deco


@dataclass
class Ctx:
    job: Job
    deadline: float
    force: bool
    state: dict = field(default_factory=dict)       # persisted per-job scratch (JSON-able)

    @property
    def log(self) -> logging.Logger:
        return log

    def remaining(self) -> float:
        return self.deadline - time.time()

    def alive(self) -> bool:
        """Still within budget (and, unless forced, still asleep)."""
        return self.remaining() > 0 and (self.force or asleep()[0])

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(min(s, max(0.0, self.remaining())))

    def save(self) -> None:
        _state["jobs"].setdefault(self.job.name, {})["state"] = self.state
        _save_state()


_state: dict = {"jobs": {}}
_run_lock: asyncio.Lock | None = None
_session_task: asyncio.Task | None = None
_current = {"job": None, "since": 0.0}


def _load_state() -> None:
    global _state
    try:
        _state = json.loads(_STATE_FILE.read_text())
        _state.setdefault("jobs", {})
    except (OSError, ValueError):
        _state = {"jobs": {}}


def _save_state() -> None:
    try:
        SLEEP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, indent=1))
        tmp.replace(_STATE_FILE)
    except OSError as e:
        log.warning("sleep: couldn't save state (%s)", e)


async def run_session(force: bool = False, budget_s: float | None = None) -> dict:
    """Run all jobs in order within the session budget. Stops as soon as he wakes
    (unless `force`, which is for testing: it skips the sleep check but still never
    touches the robot)."""
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    if _run_lock.locked():
        return {"error": "a session is already running"}
    results: dict = {}
    async with _run_lock:
        session_end = time.time() + (budget_s or config.SLEEP_SESSION_BUDGET_S)
        log.info("sleep: session starts (%s)%s", asleep()[1], " [forced]" if force else "")
        for j in sorted(JOBS.values(), key=lambda j: (j.order, j.name)):
            if not force and not asleep()[0]:
                results[j.name] = "skipped: woke up"
                break
            if session_end - time.time() < 5:
                results[j.name] = "skipped: session budget used up"
                continue
            if j.has_work is not None and not j.has_work():
                results[j.name] = "nothing to do"
                continue
            ctx = Ctx(j, min(session_end, time.time() + j.budget_s), force,
                      _state["jobs"].get(j.name, {}).get("state", {}))
            t0 = time.time()
            _current.update(job=j.name, since=t0)
            task = asyncio.ensure_future(j.fn(ctx))
            outcome = "done"
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=2)
                    if task.done():
                        break
                    if time.time() > ctx.deadline + 10:
                        task.cancel(); outcome = "cancelled: time budget"; break
                    if not force and not asleep()[0]:
                        task.cancel(); outcome = "cancelled: he woke up"; break
                if outcome == "done":
                    summary = task.result()
                else:
                    summary = None
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            except Exception as e:
                outcome, summary = f"failed: {type(e).__name__}: {e}", None
                log.exception("sleep: job %s failed", j.name)
            finally:
                _current.update(job=None, since=0.0)
            results[j.name] = {"outcome": outcome, "seconds": round(time.time() - t0, 1), **(summary or {})}
            _state["jobs"].setdefault(j.name, {}).update(
                last_run=datetime.now(config.TIMEZONE).isoformat(timespec="seconds"), last_result=results[j.name])
            _state["jobs"][j.name]["state"] = ctx.state
            _save_state()
            log.info("sleep: job %s -> %s", j.name, results[j.name])
            try:
                from . import activity
                activity.record("sleep", f"while asleep: {j.name} -> {json.dumps(results[j.name])[:200]}")
            except Exception:
                pass
            if outcome.startswith("cancelled: he woke"):
                break
        _state["last_session_end"] = time.time()
        _save_state()
    return results


async def _scheduler() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            ok, _why = asleep()
            gap = time.time() - _state.get("last_session_end", 0)
            if ok and gap >= config.SLEEP_MIN_INTERVAL_MIN * 60 and any(
                    j.has_work is None or j.has_work() for j in JOBS.values()):
                await run_session()
        except Exception:
            log.exception("sleep: scheduler error")
        await asyncio.sleep(30)


def start() -> None:
    """Start from the FastAPI lifespan (inside the running event loop)."""
    global _last_activity
    try:
        _last_activity = float(json.loads(_THREAD_FILE.read_text()).get("last", 0))
    except (OSError, ValueError):
        pass
    _load_state()
    faces.register_subscriber(_subscribe)
    if config.SLEEP_ROUTINE:
        asyncio.get_running_loop().create_task(_scheduler())
        log.info("sleep: routine enabled, jobs: %s", ", ".join(sorted(JOBS, key=lambda n: JOBS[n].order)))


def status_text() -> str:
    ok, why = asleep()
    lines = [f"asleep: {'yes' if ok else 'no'} ({why})",
             f"routine {'enabled' if config.SLEEP_ROUTINE else 'disabled'}; "
             f"running now: {_current['job'] or 'nothing'}"]
    for j in sorted(JOBS.values(), key=lambda j: j.order):
        info = _state["jobs"].get(j.name, {})
        pend = ", work pending" if j.has_work is None or j.has_work() else ", no work pending"
        lines.append(f"- {j.name}: last run {info.get('last_run', 'never')}{pend}; "
                     f"last result {json.dumps(info.get('last_result', {}))}")
    return "\n".join(lines)


# ============================================================ job 1: analyse exploration photos
_vlm = llms.chat(model=config.VISION_MODEL, temperature=0.1, timeout=60, background=True,
                 max_retries=0,        # retries are ours: failures are re-queued for a later session
                 max_tokens=400)
_json_mode = True         # flips to False if the endpoint rejects response_format

_VLM_SYSTEM = (
    "You label photos taken by Vector, a palm-sized robot exploring a home. The camera is "
    "very low (a few cm above the floor or desk) and wide, so things look big and tilted. "
    "Reply with ONLY a JSON object, no prose, with exactly these keys: "
    '"caption" (one factual sentence, max 25 words), '
    '"objects" (array of up to 8 short noun phrases for the notable things, e.g. "wall", '
    '"shoe", "table leg", "cable"; empty if nothing is clear), '
    '"setting" (a rough place, e.g. "desk", "living room floor", "kitchen", "hallway"; '
    '"unknown" if unclear). Describe only what is visible. Never identify or name people '
    "(just \"a person\"); do not guess ages or other personal traits. If the image is dark "
    'or blurry, say so in the caption and use "unknown" for the setting.'
)


def _read_jsonl(path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def analyses_by_id() -> dict[str, dict]:
    return {a["id"]: a for a in _read_jsonl(ANALYSES) if "id" in a}


def _failures() -> dict:
    return _state["jobs"].setdefault("analyze_explore_photos", {}).setdefault("state", {}).setdefault("failures", {})


def _pending() -> list[dict]:
    """Captures with a photo on disk, no analysis yet, and attempts left (newest first)."""
    done = analyses_by_id()
    fails = _failures()
    out = []
    for c in reversed(_read_jsonl(CAPTURES)):
        if c.get("id") in done or fails.get(c.get("id"), {}).get("n", 0) >= config.SLEEP_VLM_MAX_ATTEMPTS:
            continue
        if (config.EXPLORE_DIR / c.get("file", "")).is_file():
            out.append(c)
    return out


def parse_vlm(text: str) -> dict | None:
    """Defensively turn the model's reply into {caption, objects, setting}."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    data = None
    m = re.search(r"\{.*\}", text, re.S)
    for cand in (text, m.group(0) if m else ""):
        try:
            data = json.loads(cand)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        return {"caption": " ".join(text.split())[:300], "objects": [], "setting": "unknown"} if text else None
    objs = data.get("objects", [])
    if isinstance(objs, str):
        objs = [o.strip() for o in objs.split(",")]
    objs = [" ".join(str(o).split())[:40] for o in objs if str(o).strip()][:8] if isinstance(objs, list) else []
    caption = " ".join(str(data.get("caption", "")).split())[:300]
    if not caption:
        return None
    return {"caption": caption, "objects": objs, "setting": " ".join(str(data.get("setting") or "unknown").split())[:80]}


async def _ask_vlm(jpg: bytes) -> dict:
    global _json_mode
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}},
        {"type": "text", "text": "Label this photo as JSON."},
    ])
    model = llms.json_model(_vlm) if _json_mode else _vlm
    try:
        resp = await model.ainvoke([SystemMessage(_VLM_SYSTEM), msg])
    except Exception as e:
        if _json_mode and "response_format" in str(e).lower():
            log.info("sleep: endpoint rejects response_format; parsing free text from now on")
            _json_mode = False
            resp = await _vlm.ainvoke([SystemMessage(_VLM_SYSTEM), msg])
        else:
            raise
    parsed = parse_vlm(llms.text(resp))
    if parsed is None:
        raise ValueError("unparseable/empty model reply")
    return parsed


def _append_analysis(rec: dict) -> None:
    ANALYSES.parent.mkdir(parents=True, exist_ok=True)
    with open(ANALYSES, "a", encoding="utf-8") as f:       # only this job writes this file
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        f.flush()


@job("analyze_explore_photos", order=10, budget_s=900, has_work=lambda: bool(_pending()))
async def analyze_explore_photos(ctx: Ctx) -> dict:
    """Idempotent: a photo counts as done once its line is in analyses.jsonl; failures are
    counted (max SLEEP_VLM_MAX_ATTEMPTS) and retried in later sessions."""
    todo = _pending()[:config.SLEEP_VLM_BATCH]
    ok = failed = streak = 0
    for i, c in enumerate(todo):
        if not ctx.alive():
            break
        try:
            jpg = await asyncio.to_thread((config.EXPLORE_DIR / c["file"]).read_bytes)
            res = await _ask_vlm(jpg)
            _append_analysis({"id": c["id"], "analyzed_at": datetime.now(config.TIMEZONE).isoformat(timespec="seconds"),
                              "model": config.VISION_MODEL, **res})
            _failures().pop(c["id"], None)
            ok += 1
            streak = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failed += 1
            streak += 1
            f = _failures().setdefault(c["id"], {"n": 0})
            f["n"] += 1
            f["last"] = f"{type(e).__name__}: {str(e)[:120]}"
            log.warning("sleep: analysing %s failed (%s)", c["id"][:8], f["last"])
            if streak >= 3:        # endpoint probably down: stop spending the night on it
                break
        ctx.save()
        if i < len(todo) - 1:
            await ctx.sleep(config.SLEEP_VLM_DELAY_S)
    return {"analysed": ok, "failed": failed, "still_pending": len(_pending())}


# ============================================================ tools
@tool
def sleep_routine_status() -> str:
    """Is Vector asleep right now (and why), and what his night-time chores (analysing his
    exploration photos) last did / have pending."""
    return status_text()


@tool
async def run_sleep_routine_now() -> str:
    """Start the sleep chores right now in the background even though he is awake (for
    testing). It only uses the network/CPU: it never moves, speaks to or wakes him.
    Check progress with sleep_routine_status."""
    global _session_task
    if _run_lock is not None and _run_lock.locked():
        return "a sleep session is already running"
    _session_task = asyncio.get_running_loop().create_task(run_session(force=True, budget_s=300))
    return "started the sleep chores in the background (max 5 minutes); ask sleep_routine_status for progress"
