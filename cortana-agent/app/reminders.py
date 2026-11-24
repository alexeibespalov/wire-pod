"""Timers and reminders: "set a ten minute pasta timer", "remind me at seven to call Grandma".

Kept in DATA_DIR/reminders.json so they survive restarts. A background task sleeps until
the next one is due, then rings the satellite's timer chime and says it out loud
(app/satellite.py). If the satellite is offline it keeps trying for REMINDER_RETRY_MIN,
then gives up and journals it as missed. Ones that fell due while the agent was down are
still said if they're less than REMINDER_RETRY_MIN late.
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Awaitable, Callable

from langchain_core.tools import tool
from pydantic import Field

from . import activity, config

log = logging.getLogger("cortana-agent")

_PATH = config.DATA_DIR / "reminders.json"
_LOCK = threading.Lock()
_changed: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
MAX_TIMER_MIN = 24 * 60
MAX_REMINDER_DAYS = 60

# set by the server: async (item) -> None; raises if it couldn't be said
Announcer = Callable[[dict], Awaitable[None]]
_announce: Announcer | None = None


def _load() -> list[dict]:
    try:
        return json.loads(_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    tmp.replace(_PATH)


def _poke() -> None:
    if _changed is not None and _loop is not None:
        _loop.call_soon_threadsafe(_changed.set)


def _clock(ts: float) -> str:
    d = datetime.fromtimestamp(ts, config.TIMEZONE)
    now = datetime.now(config.TIMEZONE)
    t = d.strftime("%-I:%M %p")
    if d.date() == now.date():
        return t
    if d.date() == (now + timedelta(days=1)).date():
        return f"tomorrow {t}"
    return d.strftime("%A %-d %B ") + t


def _left(ts: float) -> str:
    s = max(0, int(ts - time.time()))
    if s < 90:
        return f"{s} seconds"
    m = round(s / 60)
    if m < 120:
        return f"{m} minutes"
    return f"{m // 60} hours {m % 60} minutes"


def _add(item: dict) -> str | None:
    with _LOCK:
        items = _load()
        if len(items) >= config.REMINDERS_MAX:
            return f"error: there are already {len(items)} timers and reminders; cancel one first"
        items.append(item)
        items.sort(key=lambda i: i["due"])
        _save(items)
    _poke()
    return None


def parse_when(when: str, now: datetime | None = None) -> datetime:
    """'2026-11-23 07:00', '2026-11-23T07:00', '19:30' or '7:30 PM' (next time it's that
    o'clock) in local time."""
    now = now or datetime.now(config.TIMEZONE)
    w = " ".join(when.strip().upper().replace(".", "").split())
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
        try:
            t = datetime.strptime(w, fmt)
        except ValueError:
            continue
        d = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return d if d > now else d + timedelta(days=1)
    d = datetime.fromisoformat(when.strip().replace(" ", "T", 1))
    return d.replace(tzinfo=config.TIMEZONE) if d.tzinfo is None else d


@tool
def set_timer(minutes: Annotated[float, Field(description="Length in minutes (0.5 = thirty seconds)")],
              label: Annotated[str, Field(description="What it's for, e.g. 'pasta'; empty if not said")] = "") -> str:
    """Start a countdown timer. When it ends, the chime rings and you say it's done."""
    if not 0 < minutes <= MAX_TIMER_MIN:
        return f"error: timers can be up to {MAX_TIMER_MIN // 60} hours"
    due = time.time() + minutes * 60
    label = " ".join(label.split())[:60]
    err = _add({"id": uuid.uuid4().hex[:6], "kind": "timer", "label": label, "due": due,
                "minutes": minutes, "created": time.time()})
    if err:
        return err
    activity.record("reminder", f"timer set: {label or 'timer'} {minutes:g} min")
    return f"timer set: {label or 'timer'}, {minutes:g} minutes, ends at {_clock(due)}"


@tool
def set_reminder(when: Annotated[str, Field(description=(
                     "Local time: 'HH:MM' (24h, the next time it's that o'clock) or 'YYYY-MM-DD HH:MM' "
                     "for another day. Work the date out from RIGHT NOW."))],
                 text: Annotated[str, Field(description=(
                     "What to say then, spoken to the person, e.g. 'Time to call Grandma'"))]) -> str:
    """Say something out loud at a set time ("remind me at seven to call Grandma",
    "remind us tomorrow at eight that it's bin day")."""
    try:
        due = parse_when(when)
    except ValueError:
        return "error: give the time as HH:MM or YYYY-MM-DD HH:MM"
    now = datetime.now(config.TIMEZONE)
    if due <= now:
        return "error: that time has already passed"
    if due - now > timedelta(days=MAX_REMINDER_DAYS):
        return f"error: reminders can be up to {MAX_REMINDER_DAYS} days ahead"
    text = " ".join(text.split())[:200]
    if not text:
        return "error: say what the reminder is"
    err = _add({"id": uuid.uuid4().hex[:6], "kind": "reminder", "label": text, "due": due.timestamp(),
                "created": time.time()})
    if err:
        return err
    activity.record("reminder", f"reminder set for {_clock(due.timestamp())}: {text}")
    return f"reminder set for {_clock(due.timestamp())}: {text}"


@tool
def list_timers() -> str:
    """The timers and reminders that are running, with time left."""
    items = _load()
    if not items:
        return "no timers or reminders"
    return "\n".join(
        f"- timer {i['label'] or ''} ({_left(i['due'])} left)".replace("timer  (", "timer (")
        if i["kind"] == "timer" else f"- reminder at {_clock(i['due'])}: {i['label']}"
        for i in items)


@tool
def cancel_timer(which: Annotated[str, Field(description=(
        "Words from the timer's label or reminder text, e.g. 'pasta'; 'timer' for the only timer; "
        "'all' for everything"))]) -> str:
    """Cancel timers or reminders."""
    w = which.lower().strip()
    with _LOCK:
        items = _load()
        if w == "all":
            gone = items
        else:
            gone = [i for i in items if w and w in (i["label"] or "").lower()]
            if not gone and w in {"timer", "the timer", "it"}:
                timers = [i for i in items if i["kind"] == "timer"]
                gone = timers if len(timers) == 1 else []
        if not gone:
            return f"error: nothing matches {which!r}; " + (list_timers.invoke({}) if items else "none running")
        ids = {i["id"] for i in gone}
        _save([i for i in items if i["id"] not in ids])
    _poke()
    activity.record("reminder", f"cancelled {len(gone)}: {', '.join(i['label'] or i['kind'] for i in gone)}")
    return f"cancelled {len(gone)}: " + ", ".join(i["label"] or i["kind"] for i in gone)


REMINDER_TOOLS = [set_timer, set_reminder, list_timers, cancel_timer]


def prompt_section() -> str:
    items = _load()
    if not items:
        return ""
    parts = [f"{i['label'] or 'a'} timer, {_left(i['due'])} left" if i["kind"] == "timer"
             else f"reminder at {_clock(i['due'])} ({i['label']})" for i in items[:6]]
    return "Running: " + "; ".join(parts) + "."


def spoken(item: dict) -> str:
    """What she says when it goes off."""
    if item["kind"] == "timer":
        what = f"The {item['label']} timer" if item.get("label") else "Your timer"
        return f"{what} is done."
    late = time.time() - item["due"]
    pre = "Sorry, this is a bit late. " if late > 120 else ""
    return pre + item["label"].rstrip(".") + "."


async def _fire(item: dict) -> None:
    deadline = item["due"] + config.REMINDER_RETRY_MIN * 60
    while True:
        try:
            if _announce is None:
                raise RuntimeError("no announcer")
            await _announce(item)
            activity.record("reminder", f"{item['kind']} went off: {item['label'] or item['kind']}")
            return
        except Exception as e:
            if time.time() > deadline:
                log.warning("reminder %s missed: %s", item["id"], e)
                activity.record("reminder", f"missed {item['kind']} ({item['label']}): couldn't say it ({e})")
                return
            log.info("reminder %s: couldn't say it yet (%s); retrying", item["id"], e)
            await asyncio.sleep(20)


async def _run() -> None:
    assert _changed is not None
    tasks: set[asyncio.Task] = set()
    while True:
        _changed.clear()
        now = time.time()
        with _LOCK:
            items = _load()
            due = [i for i in items if i["due"] <= now]
            if due:
                _save([i for i in items if i["due"] > now])
        for i in due:
            if now - i["due"] > config.REMINDER_RETRY_MIN * 60:
                activity.record("reminder", f"missed {i['kind']} ({i['label']}): I was offline")
                continue
            t = asyncio.create_task(_fire(i))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        nxt = min((i["due"] for i in items if i["due"] > now), default=None)
        wait = 3600 if nxt is None else max(0.2, min(nxt - time.time(), 3600))
        try:
            await asyncio.wait_for(_changed.wait(), wait)
        except (TimeoutError, asyncio.TimeoutError):
            pass


def start(announce: Announcer) -> asyncio.Task:
    global _changed, _loop, _announce
    _announce = announce
    _loop = asyncio.get_running_loop()
    _changed = asyncio.Event()
    return _loop.create_task(_run())
