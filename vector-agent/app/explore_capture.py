"""Exploration photos: while Vector runs his own built-in behaviours (exploring, driving
around) take a camera photo when he meets an obstacle or recognises an object.

Listen-only: hooks the shared SDK connection from app/faces.py (no behaviour control) and
grabs the picture from wire-pod's /cam-stream (app/robot.py camera_frame), so his own
behaviour is never interrupted. Faces are deliberately NOT a trigger (privacy).

Triggers (SDK thread; cheap, no I/O):
  * proximity: robot_state.prox_data, rising edge of "object close"
        found_object and distance_mm < EXPLORE_PROX_MM and signal_quality >= EXPLORE_PROX_MIN_QUALITY
    (on the charger the sensor reads ~85 mm at quality 0.01, hence the quality gate)
  * objects: Events.robot_observed_object (raw RobotObservedObject: object_type, object_id)
        CHARGER_BASIC -> "charger", BLOCK_LIGHTCUBE1 -> "cube", >= FIRST_CUSTOM_OBJECT_TYPE -> "custom"
Only while he is moving around on his own: off the charger, not held/picked up, not in calm
power mode (= sleeping), and pathing or wheels moving within EXPLORE_MOVING_GRACE_S.

Storage (shared contract, read by the map skill), under config.EXPLORE_DIR:
  photos/<YYYYmmdd-HHMMSS>-<trigger>-<id8>.jpg
  captures.jsonl  {"id","ts","trigger","object","prox_mm","pose":{x_mm,y_mm,angle_deg,origin_id},"file"}
Poses are only comparable within one origin_id (it resets on reboot / pickup / delocalisation).
Disk cap: only the newest EXPLORE_MAX_PHOTOS photos are kept (older jpgs are deleted; their
captures.jsonl lines stay, so readers must tolerate a missing file).
"""
import asyncio
import json
import logging
import math
import threading
import time
import uuid
from datetime import datetime
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import config, faces, robot

log = logging.getLogger("vector-agent")

# RobotState.status bits (anki_vector.status / clad RobotStatusFlag)
ST_MOVING = 0x1
ST_PICKED_UP = 0x8
ST_PATHING = 0x80
ST_CALM = 0x400
ST_ON_CHARGER = 0x1000
ST_CLIFF = 0x4000
ST_WHEELS = 0x8000
ST_HELD = 0x10000

OBJ_CUBE, OBJ_CHARGER, OBJ_FIRST_CUSTOM = 2, 6, 15      # protocol.ObjectType values

PHOTO_DIR = config.EXPLORE_DIR / "photos"
CAPTURES = config.EXPLORE_DIR / "captures.jsonl"
_STATE_FILE = config.EXPLORE_DIR / "state.json"

_loop: asyncio.AbstractEventLoop | None = None
_enabled = config.EXPLORE_CAPTURE
_write_lock: asyncio.Lock | None = None       # one writer for captures.jsonl
_inflight = 0
_stats = {"taken": 0, "failed": 0, "skipped_rate": 0, "last": None}

# state used only from the SDK event thread
_s = {
    "pose": None,          # (x, y, angle_deg, origin_id) from the latest robot_state
    "status": 0,
    "prox_close": False,   # latched until the obstacle clears (rising-edge detection)
    "last_motion": 0.0,
    "last_prox": 0.0,
    "last_any": 0.0,
    "last_obj": {},        # (type, id) -> time
    "hour": [],            # capture timestamps in the last hour
}


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Call once from the asyncio side (server lifespan); SDK-thread triggers hop onto it."""
    global _loop
    _loop = loop


def start() -> None:
    """Load the persisted on/off switch and hook the SDK connection."""
    global _enabled
    try:
        _enabled = bool(json.loads(_STATE_FILE.read_text())["enabled"])
    except (OSError, ValueError, KeyError):
        _enabled = config.EXPLORE_CAPTURE
    faces.register_subscriber(_subscribe)


def _subscribe(r) -> None:
    from anki_vector.events import Events
    r.events.subscribe(_on_state, Events.robot_state)
    r.events.subscribe(_on_object, Events.robot_observed_object)
    log.info("explore: capture hooks installed (enabled=%s)", _enabled)


# ------------------------------------------------------------------ SDK-thread callbacks
def _exploring(now: float) -> bool:
    st = _s["status"]
    if st & (ST_ON_CHARGER | ST_PICKED_UP | ST_HELD | ST_CALM):
        return False
    return now - _s["last_motion"] <= config.EXPLORE_MOVING_GRACE_S


def _on_state(_robot, _type, msg, *_a, **_k) -> None:
    now = time.time()
    st = int(msg.status)
    _s["status"] = st
    p = msg.pose
    _s["pose"] = (float(p.x), float(p.y), math.degrees(msg.pose_angle_rad), int(p.origin_id))
    if st & (ST_PATHING | ST_WHEELS):
        _s["last_motion"] = now
    if not _enabled:
        return
    pd = msg.prox_data
    close = (pd.found_object and not pd.is_lift_in_fov and 0 < pd.distance_mm < config.EXPLORE_PROX_MM
             and pd.signal_quality >= config.EXPLORE_PROX_MIN_QUALITY)
    if not close:
        # re-arm only once clearly away (hysteresis) so jitter around the threshold is one event
        if not pd.found_object or pd.distance_mm > config.EXPLORE_PROX_MM + 30:
            _s["prox_close"] = False
        return
    if _s["prox_close"]:
        return                      # same obstacle, already handled
    _s["prox_close"] = True
    if _exploring(now) and now - _s["last_prox"] >= config.EXPLORE_PROX_COOLDOWN_S:
        _s["last_prox"] = now
        _trigger("prox", None, int(pd.distance_mm), now)


def _on_object(_robot, _type, msg, *_a, **_k) -> None:
    if not _enabled:
        return
    t = int(msg.object_type)
    name = ("charger" if t == OBJ_CHARGER else "cube" if t == OBJ_CUBE
            else "custom" if t >= OBJ_FIRST_CUSTOM else None)
    if name is None:
        return
    now = time.time()
    key = (t, int(msg.object_id))
    if not _exploring(now) or now - _s["last_obj"].get(key, 0.0) < config.EXPLORE_OBJECT_COOLDOWN_S:
        return
    _s["last_obj"][key] = now
    _trigger("object", name, None, now)


def _trigger(kind: str, obj: str | None, prox_mm: int | None, now: float) -> None:
    """Global debounce + hourly cap, then hand over to the asyncio loop."""
    if now - _s["last_any"] < config.EXPLORE_MIN_INTERVAL_S:
        return
    _s["hour"] = [t for t in _s["hour"] if now - t < 3600]
    if len(_s["hour"]) >= config.EXPLORE_MAX_PER_HOUR:
        _stats["skipped_rate"] += 1
        return
    if _loop is None or _s["pose"] is None:
        return
    _s["last_any"] = now
    _s["hour"].append(now)
    x, y, ang, origin = _s["pose"]
    pose = {"x_mm": round(x, 1), "y_mm": round(y, 1), "angle_deg": round(ang, 1), "origin_id": origin}
    _loop.call_soon_threadsafe(lambda: _loop.create_task(_capture(kind, obj, prox_mm, pose)))


# ------------------------------------------------------------------ asyncio side
async def _capture(kind: str, obj: str | None, prox_mm: int | None, pose: dict) -> None:
    global _inflight, _write_lock
    if _inflight >= 2:
        return
    _inflight += 1
    try:
        jpg = await robot.camera_frame()
        cid = uuid.uuid4().hex
        stamp = datetime.now(config.TIMEZONE)
        rel = f"photos/{stamp:%Y%m%d-%H%M%S}-{kind}-{cid[:8]}.jpg"
        rec = {"id": cid, "ts": stamp.isoformat(timespec="seconds"), "trigger": kind, "object": obj,
               "prox_mm": prox_mm, "pose": pose, "file": rel}
        if _write_lock is None:
            _write_lock = asyncio.Lock()
        async with _write_lock:
            await asyncio.to_thread(_store, rel, jpg, rec)
        _stats["taken"] += 1
        _stats["last"] = rec
        log.info("explore: photo %s (%s%s) at x=%.0f y=%.0f origin=%d", cid[:8], kind,
                 f" {obj}" if obj else f" {prox_mm}mm", pose["x_mm"], pose["y_mm"], pose["origin_id"])
    except Exception as e:
        _stats["failed"] += 1
        log.warning("explore: capture failed (%s: %s)", type(e).__name__, e)
    finally:
        _inflight -= 1


def _store(rel: str, jpg: bytes, rec: dict) -> None:
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    (config.EXPLORE_DIR / rel).write_bytes(jpg)               # photo first, so lines never dangle
    with open(CAPTURES, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")   # single write + flush per line
        f.flush()
    _prune()


def _prune() -> None:
    files = sorted(PHOTO_DIR.glob("*.jpg"))      # names start with a timestamp: oldest first
    for old in files[:max(0, len(files) - config.EXPLORE_MAX_PHOTOS)]:
        try:
            old.unlink()
        except OSError:
            pass


def read_captures(limit: int | None = None) -> list[dict]:
    """All capture records (newest last), tolerant of a half-written last line."""
    out = []
    try:
        with open(CAPTURES, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


# ------------------------------------------------------------------------------ tools
@tool
def recent_exploration_finds(limit: Annotated[int, Field(description="How many, 1-20", ge=1, le=20)] = 8) -> str:
    """List the photos Vector took on his own while exploring (obstacle or object spotted):
    when, what triggered it, where (his own x/y position in mm) and, if the night-time
    analysis has run, a caption and the objects seen. Load the 'exploration' skill first."""
    caps = read_captures(200)
    if not caps:
        return "no exploration photos yet"
    from . import sleep_routine
    notes = sleep_routine.analyses_by_id()
    lines = []
    for c in caps[-limit:][::-1]:
        a = notes.get(c["id"])
        what = c["object"] or (f"obstacle at {c['prox_mm']} mm" if c.get("prox_mm") else c["trigger"])
        desc = f" | {a['caption']} (setting: {a['setting']}; objects: {', '.join(a['objects'])})" if a else " | not analysed yet"
        pz = c["pose"]
        lines.append(f"{c['ts']} {what} at ({pz['x_mm']:.0f},{pz['y_mm']:.0f}) mm, map {pz['origin_id']}{desc}")
    return "\n".join(lines)


@tool
def set_exploration_capture(enabled: Annotated[bool, Field(description="true to take photos while exploring, false to stop")]) -> str:
    """Turn Vector's automatic exploration photos on or off (survives restarts)."""
    global _enabled
    _enabled = enabled
    try:
        config.EXPLORE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"enabled": enabled}))
    except OSError as e:
        return f"switched {'on' if enabled else 'off'} until restart (couldn't save: {e})"
    return f"exploration photos are now {'on' if enabled else 'off'}"
