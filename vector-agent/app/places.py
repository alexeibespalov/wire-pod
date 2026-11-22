"""Known places: is Vector on a surface he's been on before (office desk, kitchen table,
the floor...) or somewhere new?

Vector's firmware forgets everything spatial on reboot or pick-up (his pose origin
resets), so we recognise places at the conversation layer, from evidence collected
anyway: exploration photos analysed by the VLM during sleep (app/sleep_routine.py) and
the nav-map summary (app/navmap.py). Different surfaces in the same room are different
places.

* A "placement" = consecutive captures sharing one pose origin_id (split on a long gap).
* Sleep job `identify_places` (after the VLM job): for each placement with enough analysed
  photos, an LLM compares the evidence with known places and decides match / new.
* Anti-flood: >= PLACE_MATCH_CONF = match; PLACE_PROBABLE_CONF..MATCH = "probably that
  place" (never creates one); below = a PROVISIONAL place. Provisional places are confirmed
  by a second sighting or by a human naming them; unconfirmed one-offs expire after
  PLACE_PROVISIONAL_DAYS; total capped at PLACES_MAX.
* Firmware navigation is unaffected: this is memory and context for conversation only.
"""
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import Field

from . import llm as llms
from . import config, explore, navmap
from .sleep_routine import Ctx, job

log = logging.getLogger("vector-agent")
_FILE = config.DATA_DIR / "places" / "places.json"
_lock = threading.Lock()

_llm = llms.chat(temperature=0.1, timeout=60, max_retries=1, max_tokens=700, background=True)


# ------------------------------------------------------------------------- storage
def _load() -> dict:
    try:
        d = json.loads(_FILE.read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("places", [])
    d.setdefault("groups", {})      # placement key -> decision
    return d


def _save(d: dict) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(_FILE)


def _now() -> str:
    return datetime.now(config.TIMEZONE).isoformat(timespec="seconds")


def _by_id(d: dict, pid: str | None) -> dict | None:
    return next((p for p in d["places"] if p["id"] == pid), None)


# ---------------------------------------------------------------------- placements
def placements() -> list[dict]:
    """Group captures into placements (same origin_id, no gap > PLACE_GROUP_GAP_H), oldest first."""
    caps = sorted(explore.load(), key=lambda c: c["when"])
    groups, cur = [], None
    for c in caps:
        origin = (c.get("pose") or {}).get("origin_id")
        if (cur is None or origin != cur["origin"]
                or (c["when"] - cur["last"]).total_seconds() > config.PLACE_GROUP_GAP_H * 3600):
            cur = {"key": f"{origin}-{str(c['id'])[:8]}", "origin": origin, "first": c["when"],
                   "last": c["when"], "caps": []}
            groups.append(cur)
        cur["caps"].append(c)
        cur["last"] = c["when"]
    for g in groups:
        g["analysed"] = [c for c in g["caps"] if c.get("analysis")]
    return groups


def _pending_groups() -> list[dict]:
    with _lock:
        done = _load()["groups"]
    out = []
    for g in placements():
        n = len(g["analysed"])
        prev = done.get(g["key"])
        if n >= config.PLACE_MIN_PHOTOS and (prev is None or n - prev.get("photos", 0) >= 3):
            out.append(g)
    return out


def _current_group() -> dict | None:
    pose = navmap.current_pose()
    if pose is None:
        return None
    groups = [g for g in placements() if g["origin"] == pose[3]]
    if not groups:
        return None
    g = groups[-1]
    if (datetime.now(timezone.utc) - g["last"]).total_seconds() > 3 * 86400:
        return None      # origin ids restart after reboots: don't trust a stale match
    return g


# -------------------------------------------------------------------------- deciding
_SYSTEM = """You help Vector, a palm-sized home robot, recognise WHERE he is. People carry \
him between surfaces: an office desk, another desk, the kitchen table, the floor, etc. Each \
SURFACE is its own place, even within the same room. Vector's own map resets whenever he is \
moved, so his start position and coordinates mean nothing across visits; judge by what was \
seen: the surface (wood desk, glass table, carpet), nearby things and how they are arranged \
relative to each other (monitor + keyboard + mug near an edge), the room around, and the \
map layout notes (size of the surface, where edges are) when given.

You get the known places and the evidence from one stay. Decide whether this stay is one of \
the known places or a new one. A known place can look a bit different (objects moved, Vector \
started elsewhere on it); that is still the same place. Be conservative: thin or ambiguous \
evidence = low confidence.

Reply with ONLY a JSON object with these keys:
"match_id": id of the matching known place, or null
"confidence": 0.0-1.0, how sure you are about your choice (match or new)
"reasoning": one sentence
"name": 2-4 word everyday name for this place, e.g. "office desk", "kitchen table", \
"living room floor" (for a match, the known name unless a clearly better one fits)
"description": one sentence describing the place
"setting": room/area, e.g. "home office"
"landmarks": up to 8 short things that identify it
"layout": one short sentence about the surface, edges and arrangement
"changes": what looks different from the matched place, or "" """


def _evidence(g: dict) -> str:
    lines = [f"Stay from {g['first']:%a %d %b %H:%M} to {g['last']:%H:%M} UTC, "
             f"{len(g['caps'])} photos, {len(g['analysed'])} described:"]
    base = None
    for c in g["analysed"][:30]:
        a, p = c["analysis"], c.get("pose") or {}
        if base is None:
            base = p
        dx = (p.get("x_mm", 0) - base.get("x_mm", 0)) / 10
        dy = (p.get("y_mm", 0) - base.get("y_mm", 0)) / 10
        trig = c.get("object") or (f"obstacle {c.get('prox_mm')} mm ahead" if c.get("prox_mm") else c.get("trigger"))
        lines.append(f"- [{trig}; at {dx:+.0f},{dy:+.0f} cm from the first photo] {a.get('caption')} "
                     f"(setting: {a.get('setting')}; objects: {', '.join(a.get('objects') or [])})")
    pose = navmap.current_pose()
    if pose is not None and pose[3] == g["origin"]:
        lines.append("Map of this stay (Vector's own nav map):\n" + navmap.summarise())
    return "\n".join(lines)


def _known_text(d: dict) -> str:
    if not d["places"]:
        return "(no known places yet)"
    out = []
    for p in d["places"]:
        out.append(f"- id={p['id']} name={p['name']!r} status={p['status']} seen={p['sightings']}x "
                   f"last={p['last_seen'][:10]} setting={p.get('setting')!r}\n  description: {p.get('description')}"
                   f"\n  landmarks: {', '.join(p.get('landmarks') or [])}\n  layout: {p.get('layout')}")
    return "\n".join(out)


def _parse(text: str) -> dict | None:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0) if m else text)
    except ValueError:
        return None
    if not isinstance(d, dict) or not d.get("name"):
        return None
    try:
        d["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        d["confidence"] = 0.0
    d["landmarks"] = [str(x)[:40] for x in (d.get("landmarks") or [])][:8]
    return d


async def decide(known: str, evidence: str) -> dict | None:
    msgs = [SystemMessage(_SYSTEM), HumanMessage(f"KNOWN PLACES\n{known}\n\nEVIDENCE\n{evidence}")]
    try:
        resp = await llms.json_model(_llm).ainvoke(msgs)
    except Exception:
        resp = await _llm.ainvoke(msgs)
    return _parse(llms.text(resp))


def _apply(d: dict, g: dict, r: dict) -> str:
    """Update the store with one decision; returns a short description of what happened."""
    match = _by_id(d, r.get("match_id"))
    conf = r["confidence"]
    now = _now()
    if match and conf >= config.PLACE_PROBABLE_CONF:
        kind = "match" if conf >= config.PLACE_MATCH_CONF else "probable"
        match["sightings"] += 1
        match["last_seen"] = now
        match["landmarks"] = list(dict.fromkeys((r["landmarks"] or []) + match.get("landmarks", [])))[:15]
        if r.get("layout"):
            match["layout"] = r["layout"]
        if r.get("changes"):
            match.setdefault("changes", []).append(f"{now[:10]}: {r['changes']}")
            match["changes"] = match["changes"][-5:]
        if match["status"] == "provisional" and match["sightings"] >= config.PLACE_CONFIRM_SIGHTINGS:
            match["status"] = "confirmed"
            kind += "+confirmed"
        pid = match["id"]
    else:
        provisional = [p for p in d["places"] if p["status"] == "provisional"]
        if len(d["places"]) >= config.PLACES_MAX:
            if not provisional:
                d["groups"][g["key"]] = {"place_id": None, "kind": "full", "confidence": conf,
                                         "photos": len(g["analysed"]), "decided_at": now, "origin": g["origin"]}
                return "place list full; not adding"
            drop = min(provisional, key=lambda p: (p["sightings"], p["last_seen"]))
            d["places"].remove(drop)
        pid = uuid.uuid4().hex[:8]
        d["places"].append({"id": pid, "name": r["name"][:40], "status": "provisional", "sightings": 1,
                            "first_seen": now, "last_seen": now, "description": r.get("description", ""),
                            "setting": r.get("setting", ""), "landmarks": r["landmarks"],
                            "layout": r.get("layout", ""), "changes": []})
        kind = "new-provisional"
    d["groups"][g["key"]] = {"place_id": pid, "kind": kind, "confidence": conf, "reasoning": r.get("reasoning", ""),
                             "photos": len(g["analysed"]), "decided_at": now, "origin": g["origin"],
                             "last": g["last"].isoformat()}
    return f"{kind}: {_by_id(d, pid)['name']} ({conf:.2f})"


def _expire(d: dict) -> int:
    cutoff = (datetime.now(config.TIMEZONE) - timedelta(days=config.PLACE_PROVISIONAL_DAYS)).isoformat()
    old = [p for p in d["places"] if p["status"] == "provisional" and p["sightings"] <= 1 and p["last_seen"] < cutoff]
    for p in old:
        d["places"].remove(p)
    return len(old)


@job("identify_places", order=20, budget_s=300, has_work=lambda: bool(_pending_groups()))
async def identify_places(ctx: Ctx) -> dict:
    results = []
    for g in _pending_groups():
        if not ctx.alive():
            break
        with _lock:
            known = _known_text(_load())
        r = await decide(known, _evidence(g))
        if r is None:
            log.warning("places: unparseable decision for %s", g["key"])
            continue
        with _lock:
            d = _load()
            msg = _apply(d, g, r)
            _save(d)
        log.info("places: %s -> %s | %s", g["key"], msg, r.get("reasoning", ""))
        results.append(msg)
    with _lock:
        d = _load()
        n = _expire(d)
        if n:
            _save(d)
            log.info("places: expired %d unconfirmed one-off place(s)", n)
    return {"decided": results, "expired": n}


# ------------------------------------------------------------------- awake: context
def current() -> tuple[dict | None, dict | None]:
    """(place, decision) for where Vector is now, if this stay has been identified."""
    g = _current_group()
    with _lock:
        d = _load()
    dec = d["groups"].get(g["key"]) if g else None
    if not dec:
        # named by a human before any photos were taken here: match on the current origin
        pose = navmap.current_pose()
        if pose is None:
            return None, None
        cutoff = (datetime.now(config.TIMEZONE) - timedelta(days=3)).isoformat()
        cands = [v for v in d["groups"].values() if v.get("origin") == pose[3] and v.get("decided_at", "") >= cutoff]
        if not cands:
            return None, None
        dec = max(cands, key=lambda v: v["decided_at"])
    return _by_id(d, dec.get("place_id")), dec


def prompt_section() -> str:
    place, dec = current()
    with _lock:
        names = [f"{p['name']}{'' if p['status'] == 'confirmed' else ' (unconfirmed)'}" for p in _load()["places"]]
    known = f" Places you know: {', '.join(names)}." if names else ""
    if place:
        sure = ("you're fairly sure" if dec["confidence"] >= config.PLACE_MATCH_CONF else "you think, not sure")
        first = " This is a new place to you (unconfirmed)." if place["status"] == "provisional" else ""
        return f"WHERE YOU ARE\nYou're on the {place['name']} ({sure}).{first}{known}\n"
    return f"WHERE YOU ARE\nYou haven't worked out which place this is yet.{known}\n"


# ------------------------------------------------------------------------------ tools
@tool
def which_place() -> str:
    """Which known place Vector is on now (office desk, kitchen table...), how sure he is,
    and every place he knows. Load the 'places' skill first."""
    place, dec = current()
    with _lock:
        d = _load()
    lines = []
    if place:
        lines.append(f"NOW: {place['name']} ({dec['kind']}, confidence {dec['confidence']:.2f}, "
                     f"{place['status']}). {place.get('description', '')} Landmarks: "
                     f"{', '.join(place.get('landmarks') or [])}. Why: {dec.get('reasoning', '')}")
        if place.get("changes"):
            lines.append("Recent changes noticed: " + "; ".join(place["changes"][-2:]))
    else:
        g = _current_group()
        lines.append("NOW: not identified yet" + (f" ({len(g['analysed'])} described photos from this stay so far; "
                     "places are worked out while I sleep)" if g else " (no exploration photos from this stay yet)"))
    for p in d["places"]:
        lines.append(f"- {p['name']} [{p['status']}, seen {p['sightings']}x, last {p['last_seen'][:10]}]: "
                     f"{p.get('description', '')}")
    if not d["places"]:
        lines.append("(no known places yet)")
    return "\n".join(lines)


@tool
def name_place(name: Annotated[str, Field(description="What the human calls the place Vector is on now, e.g. 'kitchen table'")]) -> str:
    """The human tells Vector where he is ("this is the kitchen table", "you're on my office
    desk"). Confirms/renames the current place, or creates it. Load the 'places' skill first."""
    name = " ".join(name.split()).strip()[:40]
    if not name:
        return "error: give a name"
    g = _current_group()
    pose = navmap.current_pose()
    with _lock:
        d = _load()
        dec = d["groups"].get(g["key"]) if g else None
        place = _by_id(d, dec.get("place_id")) if dec else None
        if place is None:   # not identified yet: maybe an existing place with that name
            place = next((p for p in d["places"] if p["name"].lower() == name.lower()), None)
        if place is None:
            place = {"id": uuid.uuid4().hex[:8], "name": name, "status": "confirmed", "sightings": 1,
                     "first_seen": _now(), "last_seen": _now(), "description": "", "setting": "",
                     "landmarks": [], "layout": "", "changes": []}
            d["places"].append(place)
        old = place["name"]
        place["name"], place["status"], place["last_seen"] = name, "confirmed", _now()
        key = g["key"] if g else f"{pose[3] if pose else 'x'}-named-{int(time.time())}"
        d["groups"][key] = {"place_id": place["id"], "kind": "named", "confidence": 1.0,
                            "photos": len(g["analysed"]) if g else 0, "decided_at": _now(),
                            "origin": pose[3] if pose else None,
                            "last": (g["last"] if g else datetime.now(timezone.utc)).isoformat()}
        _save(d)
    return f"got it: this is the {name}" + (f" (was '{old}')" if old.lower() != name.lower() else "")


@tool
def forget_place(name: Annotated[str, Field(description="Name of the place to forget")]) -> str:
    """Delete a known place from Vector's memory."""
    with _lock:
        d = _load()
        hits = [p for p in d["places"] if p["name"].lower() == name.strip().lower()]
        if not hits:
            return f"error: no place called {name!r}; known: {', '.join(p['name'] for p in d['places']) or 'none'}"
        for p in hits:
            d["places"].remove(p)
            for k, v in list(d["groups"].items()):
                if v.get("place_id") == p["id"]:
                    del d["groups"][k]
        _save(d)
    return f"forgot the {name}"


PLACE_TOOLS = [which_place, name_place, forget_place]
