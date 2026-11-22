"""Practice: Vector learns physical skills by trying, measuring and adjusting, on his own.

* start_practice(goal): a background practice SESSION. A small agent loop (same model,
  body tools only: move, sense, look, maneuvers, tune, record_attempt) runs the
  body-learning loop for up to N minutes / tries, quietly. It pauses the moment someone
  says "Hey Vector" or a voice turn is running, and each motion holds the conversation
  lock so it never collides with a reply. The session's notes show up in his prompt.
* Idle practice (BODY_IDLE_PRACTICE=1, off by default): when he's awake, off the charger
  and nobody has talked to him for a while, he sometimes practises his least reliable
  maneuver for a few minutes.
* Sleep review (sleep job "review_learning"): never moves. Reads the day's attempts and
  practice notes, asks the model for up to 3 improved variants, keeps only those that pass
  the kinematic dry-run, and saves them as DRAFT maneuvers (they become "tested" only after
  a real successful run).
"""
import asyncio
import contextvars
import json
import logging
import time
from datetime import datetime
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import Field

from . import config, faces, routines, skills, sleep_routine, turn

log = logging.getLogger("vector-agent")

PRACTICE_TOOLS = {"move", "sense", "look", "drive", "save_maneuver", "run_maneuver", "record_attempt",
                  "tune", "list_maneuvers", "show_maneuver"}
MOTION_TOOLS = {"move", "run_maneuver", "tune", "drive"}
MAX_MINUTES = 30
MAX_TRIES = 40

_llm = ChatOpenAI(model=config.LLM_MODEL, base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY,
                  temperature=0.4, extra_body=config.LLM_EXTRA_BODY, timeout=60, max_retries=1,
                  max_tokens=900)

_session: dict | None = None
_task: asyncio.Task | None = None


def _store():
    # NOT in the maneuvers folder: the catalogue treats every *.json there as a maneuver
    return config.DATA_DIR / "practice" / "sessions.json"


def _history() -> list[dict]:
    try:
        return json.loads(_store().read_text())
    except (FileNotFoundError, ValueError):
        return []


def _remember(sess: dict) -> None:
    h = _history() + [{k: sess[k] for k in ("goal", "by", "started", "ended", "tries", "summary", "status")}]
    p = _store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(h[-20:], indent=1, ensure_ascii=False))
    tmp.replace(p)


def active() -> bool:
    return _task is not None and not _task.done()


# ------------------------------------------------------------------- the session loop
_SYSTEM = """\
You are Vector, a small robot, PRACTISING A PHYSICAL SKILL ON YOUR OWN in the background. \
Nobody is talking to you; don't chat, just practise. Goal: {goal}

Work in small, safe tries: each turn, make ONE try (tool calls), read the measured result \
(and look again if the result is visual), then write one short line: what happened and \
what you'll change next. Save what works with save_maneuver (name by intent), run the saved \
version, and record_attempt honestly. Use tune when one number needs searching. You have \
{tries} tries and {minutes} minutes at most. When the goal is met (works twice in a row) or \
you're stuck, reply with "DONE: <two sentences: what you learned and which maneuver to use>".

=== movement skill ===
{movement}

=== body-learning skill ===
{learning}"""


def _trim(msgs: list) -> list:
    """Keep the system + goal messages and the recent tail, cut at a clean boundary (an AI
    message that starts a round), so tool calls stay paired with their results."""
    head, tail = msgs[:2], msgs[2:]
    if len(tail) <= 24:
        return msgs
    tail = tail[-24:]
    while tail and not isinstance(tail[0], AIMessage):
        tail = tail[1:]
    return head + tail


async def _wait_until_free(sess: dict) -> None:
    while turn.lock().locked() or time.time() < routines._quiet_until:
        if sess["status"] != "paused":
            sess["status"] = "paused"
            log.info("practice: paused (someone is talking to me)")
        await asyncio.sleep(1)
    if sess["status"] == "paused":
        sess["status"] = "practising"
        log.info("practice: resumed")


async def _call(name: str, args: dict, sess: dict) -> str:
    from .tools import TOOLS_BY_NAME
    t = TOOLS_BY_NAME.get(name)
    if t is None or name not in PRACTICE_TOOLS:
        return f"error: {name} isn't available while practising"
    if name in MOTION_TOOLS:
        if sess["tries"] >= sess["max_tries"]:
            return "error: no tries left; reply DONE with what you learned"
        await _wait_until_free(sess)
        lk = await routines._control_slot(wait=30)     # own the robot for this motion
        if lk is None:
            return "skipped: someone is talking to me"
        sess["tries"] += 1
        try:
            return str(await t.ainvoke(args))[:900]
        except Exception as e:
            return f"error: {e}"
        finally:
            lk.release()
    try:
        return str(await t.ainvoke(args))[:900]
    except Exception as e:
        return f"error: {e}"


async def _run(sess: dict) -> None:
    from .tools import TOOLS_BY_NAME
    from .maneuvers import known_maneuvers_prompt
    tools = [TOOLS_BY_NAME[n] for n in PRACTICE_TOOLS if n in TOOLS_BY_NAME]
    llm = _llm.bind_tools(tools)
    sk = skills.SKILLS
    msgs: list = [
        SystemMessage(_SYSTEM.format(goal=sess["goal"], tries=sess["max_tries"], minutes=sess["minutes"],
                                     movement=sk.get("movement", {}).get("body", ""),
                                     learning=sk.get("body-learning", {}).get("body", ""))),
        HumanMessage(f"{known_maneuvers_prompt()}\nStart practising: {sess['goal']}"),
    ]
    summary = ""
    try:
        while time.time() < sess["until"] and sess["tries"] <= sess["max_tries"]:
            await _wait_until_free(sess)
            res: AIMessage = await llm.ainvoke(_trim(msgs))
            msgs.append(res)
            text = str(res.content).strip()
            if text:
                sess["log"].append(text[:200])
                sess["log"] = sess["log"][-12:]
            if "DONE" in text and not res.tool_calls:
                summary = text.split("DONE", 1)[1].lstrip(": ").strip()
                break
            if not res.tool_calls:
                msgs.append(HumanMessage("Next try (tool calls), or DONE: <what you learned>."))
                continue
            for tc in res.tool_calls:
                out = await _call(tc["name"], tc["args"], sess)
                msgs.append(ToolMessage(content=out, tool_call_id=tc["id"], name=tc["name"]))
                if tc["name"] in MOTION_TOOLS:
                    sess["log"].append(f"{tc['name']}: {out[:160]}")
                    sess["log"] = sess["log"][-12:]
                if out.startswith("error: stopped:") and ("picked up" in out or "held" in out):
                    summary = "Someone picked me up, so I stopped practising."
                    raise asyncio.CancelledError
        sess["status"] = "done"
    except asyncio.CancelledError:
        sess["status"] = "stopped"
    except Exception as e:
        log.exception("practice failed")
        sess["status"] = "failed"
        summary = summary or f"Practice broke off ({e})."
    if not summary:
        try:
            res = await _llm.ainvoke(_trim(msgs) + [HumanMessage(
                "Practice time is over. In two sentences: what did you learn, and which maneuver "
                "should you use next time?")])
            summary = str(res.content).strip()[:400]
        except Exception:
            summary = "; ".join(sess["log"][-3:])[:400]
    sess["summary"] = summary
    sess["ended"] = time.time()
    _remember(sess)
    log.info("practice %s after %d tries: %s", sess["status"], sess["tries"], summary)


def _start(goal: str, minutes: float, max_tries: int, by: str) -> dict:
    global _session, _task
    now = time.time()
    _session = {"goal": goal, "by": by, "started": now, "ended": None, "until": now + minutes * 60,
                "minutes": minutes, "max_tries": max_tries, "tries": 0, "status": "practising",
                "log": [], "summary": ""}
    _task = asyncio.get_running_loop().create_task(_run(_session), context=contextvars.Context())
    log.info("practice started: %s (%g min, %d tries)", goal, minutes, max_tries)
    return _session


def prompt_section() -> str:
    if active() and _session:
        s = _session
        lines = [f"YOU'RE PRACTISING IN THE BACKGROUND: {s['goal']} ({s['status']}, try {s['tries']}"
                 f"/{s['max_tries']}, until {datetime.fromtimestamp(s['until'], config.TIMEZONE):%-I:%M %p}; "
                 f"stop_practice ends it)"]
        lines += [f"    {x}" for x in s["log"][-3:]]
        return "\n".join(lines) + "\n"
    h = _history()
    if h and h[-1].get("ended") and time.time() - h[-1]["ended"] < 3 * 3600:
        last = h[-1]
        return f"YOU PRACTISED EARLIER: {last['goal']} ({last['tries']} tries). {last['summary']}\n"
    return ""


# ------------------------------------------------------------------------------ tools
@tool
async def start_practice(
        goal: Annotated[str, Field(description="What to practise, concretely, e.g. 'push the red ball "
                                               "about 10 cm forward without losing it (dribble)'")],
        minutes: Annotated[float, Field(description="How long, default 10, max 30")] = 10,
        max_tries: Annotated[int, Field(description="Max physical tries, default 20, max 40")] = 20) -> str:
    """Practise a PHYSICAL skill on your own in the background: try, measure, adjust, save
    maneuvers, repeat. Use when someone says "practise ...", "learn to ...", "get better at
    ...", or after a move didn't work well. You pause when someone talks to you. Load the
    'body-learning' skill first."""
    if active():
        return f"error: I'm already practising: {_session['goal']} (stop_practice first)"
    goal = " ".join(goal.split())[:300]
    if len(goal) < 5:
        return "error: say what to practise"
    s = _start(goal, max(1.0, min(minutes, MAX_MINUTES)), max(1, min(max_tries, MAX_TRIES)), faces.front_name())
    return (f"started practising '{goal}' for up to {s['minutes']:g} minutes / {s['max_tries']} tries. "
            f"Tell them briefly; you'll practise quietly.")


@tool
def practice_status() -> str:
    """How your background practice is going."""
    if not (active() and _session):
        h = _history()
        return f"not practising. Last time: {h[-1]['goal']}: {h[-1]['summary']}" if h else "not practising"
    s = _session
    return (f"practising '{s['goal']}' ({s['status']}), {s['tries']}/{s['max_tries']} tries, "
            f"{max(0, (s['until'] - time.time()) / 60):.0f} min left. Recent: " + " | ".join(s["log"][-4:]))


@tool
async def stop_practice() -> str:
    """Stop practising (e.g. "stop practising", "that's enough")."""
    if not active():
        return "I wasn't practising"
    _task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(_task), 15)
    except Exception:
        pass
    return f"stopped practising. {(_session or {}).get('summary', '')}"


PRACTICE_TOOL_LIST = [start_practice, practice_status, stop_practice]


# ----------------------------------------------------------------------- idle practice
async def _idle_loop() -> None:
    from . import body
    from .maneuvers import catalogue
    await asyncio.sleep(120)
    while True:
        try:
            s = body.FEED.snapshot()
            st = s.get("status", 0)
            idle = time.time() - sleep_routine._last_activity > config.BODY_IDLE_AFTER_MIN * 60
            calm_or_docked = st & (body.ST_ON_CHARGER | 0x400)
            if (config.BODY_IDLE_PRACTICE and idle and not calm_or_docked and not active()
                    and not turn.lock().locked() and (s.get("age_s") or 99) < 2):
                docs = [d for d in catalogue().all() if d["stats"].get("runs", 0) > 0]
                if docs:
                    d = min(docs, key=lambda d: d["stats"].get("competency") or 0)
                    if (d["stats"].get("competency") or 0) < 0.8:
                        _start(f"get more reliable at your maneuver '{d['name']}' ({d['description']})",
                               config.BODY_IDLE_MINUTES, 10, "")
        except Exception:
            log.exception("practice: idle loop error")
        await asyncio.sleep(config.BODY_IDLE_CHECK_MIN * 60)


def start() -> None:
    asyncio.get_running_loop().create_task(_idle_loop())


# ------------------------------------------------------------------------ sleep review
_REVIEW_PROMPT = """\
You review a small home robot's movement practice while it sleeps. Below: its saved \
maneuvers (with stats) and its recent attempts and practice notes. Propose at most 3 \
improved DRAFT maneuvers where the data shows a clear, specific fix (e.g. a push that kept \
stopping short -> longer drive; a turn that overshot -> smaller angle). Use only these step \
types: drive{{mm,speed,stop_when}}, turn{{deg}}, wheels{{left,right,ms}}, head{{deg}}, \
lift{{height}}, wait{{ms}}, maneuver{{name,values}}, repeat{{times,steps}}, \
if{{sensor,then,else}} (each step is an object with a "step" key). Keep moves small (desk).
Reply as JSON: {{"notes": "<one paragraph: what went well/badly>", "drafts": [{{"base": "<existing name>", \
"name": "<new name, by intent>", "description": "...", "steps": [...], "params": {{}}}}]}}. \
No drafts if nothing is clearly worth changing.

MANEUVERS:
{maneuvers}

RECENT ATTEMPTS (newest last):
{attempts}

PRACTICE NOTES:
{practice}"""


def _review_state() -> dict:
    try:
        return json.loads((_store().parent / "review.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _has_review_work() -> bool:
    from .maneuvers import attempts
    rows = attempts().recent(None, 1)
    return bool(rows) and rows[-1].get("time", "") > _review_state().get("last_attempt", "")


@sleep_routine.job("review_learning", order=60, budget_s=240, has_work=_has_review_work)
async def review_learning(ctx) -> dict:
    from .maneuvers import attempts, catalogue, dry_run
    cat = catalogue()
    docs = cat.all()
    rows = attempts().recent(None, 60)
    if not rows:
        return {"drafts": 0}
    man = "\n".join(f"- {d['name']} ({d.get('status')}): {d['description']}; stats {json.dumps(d.get('stats', {}))}; "
                    f"params {json.dumps(d.get('params', {}))}; steps {json.dumps(d['steps'])[:600]}" for d in docs[:30])
    att = "\n".join(f"- {r['time']} {r['maneuver']} args={json.dumps(r.get('args'))} outcome={r.get('outcome')} "
                    f"note={r.get('note')} result={json.dumps(r.get('result'))[:240]}" for r in rows)
    prac = "\n".join(f"- {p['goal']}: {p['summary']}" for p in _history()[-5:]) or "(none)"
    res = await _llm.ainvoke([HumanMessage(_REVIEW_PROMPT.format(maneuvers=man or "(none)", attempts=att,
                                                                 practice=prac))],
                             response_format={"type": "json_object"})
    try:
        plan = json.loads(str(res.content))
    except ValueError:
        return {"error": "review reply wasn't JSON"}
    saved, rejected = [], []
    for dft in (plan.get("drafts") or [])[:3]:
        if not ctx.alive():
            break
        try:
            sim = dry_run(dft["steps"], dft.get("params"), cat=cat)
            if not sim["ok"]:
                rejected.append(f"{dft.get('name')}: {'; '.join(sim['violations'])[:160]}")
                continue
            if cat.get(dft["name"]) and cat.get(dft["name"]).get("status") == "tested":
                rejected.append(f"{dft['name']}: would overwrite a tested maneuver")
                continue
            cat.save(dft["name"], dft.get("description", f"improved {dft.get('base', '')}"), dft["steps"],
                     dft.get("params"), tags=["sleep-draft"], author="vector (sleep review)")
            saved.append(dft["name"])
        except Exception as e:
            rejected.append(f"{dft.get('name')}: {e}"[:160])
    st = {"last_attempt": rows[-1].get("time", ""), "at": datetime.now(config.TIMEZONE).isoformat(timespec="seconds"),
          "notes": str(plan.get("notes", ""))[:800], "saved": saved, "rejected": rejected}
    p = _store().parent / "review.json"
    p.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    ctx.log.info("sleep: movement review: saved drafts %s, rejected %s", saved, rejected)
    return {"drafts": saved, "rejected": rejected}
