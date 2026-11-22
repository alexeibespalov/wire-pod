"""Who is Vector looking at? Live face presence from the robot's own face recognition.

A background thread keeps a listen-only Vector SDK connection (wirepod_vector_sdk, no
behaviour control) and records `robot_observed_face` events: Vector's on-device
recognition (WireOS: YuNet + MobileFaceNet) tells us each face's id and, if enrolled,
the person's name. The agent's system prompt gets a "who's here" line every turn.

Enrolment / deletion go through wire-pod's /api-sdk (add_face -> meet-Victor behaviour).
"""
import json
import logging
import threading
import time
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import config, robot

log = logging.getLogger("vector-agent")

PRESENT_SECONDS = 20       # a face seen within this window counts as "here now"
KNOWN_REFRESH_SECONDS = 60

_lock = threading.Lock()
_seen: dict[int, dict] = {}      # face_id -> {"name": str, "last": float}
_known: list[str] = []           # enrolled names
_known_at = 0.0
_status = "not started"
_subscribers: list = []          # fn(robot) called on every (re)connect, in the SDK thread


def register_subscriber(fn) -> None:
    """Let other modules hook the shared listen-only SDK connection (e.g. subscribe to
    events, start feeds). Called with the connected anki_vector.Robot on every
    (re)connect, from the face-watcher thread; keep it quick and don't take control."""
    _subscribers.append(fn)


# ---------------------------------------------------------------- background watcher
def _watch_forever() -> None:
    global _status
    import anki_vector
    from anki_vector.events import Events

    def on_face(_robot, _type, event, *_a, **_k):
        with _lock:
            _seen[int(event.face_id)] = {"name": event.name or "", "last": time.time()}

    def on_id_changed(_robot, _type, event, *_a, **_k):  # face merged into a known id
        with _lock:
            old = _seen.pop(int(event.old_id), None)
            if old:
                _seen[int(event.new_id)] = old

    backoff = 5
    while True:
        try:
            with anki_vector.Robot(config.VECTOR_SERIAL, behavior_control_level=None,
                                   enable_face_detection=True, cache_animation_lists=False) as r:
                r.events.subscribe(on_face, Events.robot_observed_face)
                r.events.subscribe(on_id_changed, Events.robot_changed_observed_face_id)
                for fn in list(_subscribers):
                    try:
                        fn(r)
                    except Exception as e:
                        log.warning("faces: subscriber %s failed: %s", getattr(fn, "__name__", fn), e)
                _status = "watching"
                log.info("faces: listening for Vector's face recognition events")
                backoff = 5
                while True:
                    _refresh_known_sync(r)
                    time.sleep(KNOWN_REFRESH_SECONDS)
        except Exception as e:
            _status = f"disconnected ({type(e).__name__})"
            log.warning("faces: SDK connection lost (%s); retrying in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


def _refresh_known_sync(r) -> None:
    global _known, _known_at
    try:
        names = r.faces.request_enrolled_names()
        with _lock:
            _known = sorted({f.name for f in names.faces if f.name})
            _known_at = time.time()
    except Exception as e:
        log.warning("faces: couldn't read enrolled names (%s)", e)


def start() -> None:
    threading.Thread(target=_watch_forever, name="face-watcher", daemon=True).start()


# ---------------------------------------------------------------------- for the prompt
def present() -> tuple[list[str], int]:
    """(names of recognised people here now, number of unrecognised faces here now)"""
    cutoff = time.time() - PRESENT_SECONDS
    with _lock:
        recent = [v for v in _seen.values() if v["last"] >= cutoff]
    names = sorted({v["name"] for v in recent if v["name"]})
    unknown = sum(1 for v in recent if not v["name"])
    return names, unknown


def prompt_section() -> str:
    names, unknown = present()
    with _lock:
        known = list(_known)
    if _status != "watching":
        here = "Your face recognition isn't connected right now, so you can't tell who's there."
    elif names and unknown:
        here = (f"You can see {', '.join(names)} right now, plus {unknown} face(s) you don't know.")
    elif names:
        here = (f"You can see {', '.join(names)} right now (your own face recognition). "
                f"Assume you're talking to {names[0] if len(names) == 1 else 'one of them'}.")
    elif unknown:
        here = "You can see someone right now, but you don't know their face yet."
    else:
        here = "You can't see anyone's face at the moment."
    people = ", ".join(known) if known else "nobody yet"
    return f"WHO'S HERE\n{here}\nPeople you know by face: {people}.\n"


# ------------------------------------------------------------------------------ tools
@tool
async def learn_face(name: Annotated[str, Field(description="The person's first name, e.g. 'Alexei'")]) -> str:
    """Start Vector's own face enrolment for the person in front of him: he looks at them,
    scans their face and remembers it with this name. Load the 'faces' skill first.
    The person must be facing him, in decent light. It takes about 10 seconds and he
    speaks during it, so keep your own reply very short."""
    name = " ".join(name.split()).strip().title()
    if not name or len(name) > 30:
        return "error: give a short first name"
    await robot.sdk("add_face", name=name)
    return f"started face enrolment for {name}: he'll scan their face now"


@tool
async def forget_face(name: Annotated[str, Field(description="Name of the person to forget")]) -> str:
    """Delete a person's enrolled face from Vector's face recognition."""
    faces = json.loads(await robot.sdk("get_faces") or "null") or []
    matches = [f for f in faces if (f.get("name") or "").lower() == name.strip().lower()]
    if not matches:
        known = ", ".join(f.get("name", "") for f in faces) or "nobody"
        return f"error: no enrolled face called {name!r} (known: {known})"
    for f in matches:
        await robot.sdk("delete_face", id=f.get("face_id") or f.get("faceId") or f.get("id"))
    with _lock:
        _known[:] = [k for k in _known if k.lower() != name.strip().lower()]
    return f"forgot {name}'s face"
