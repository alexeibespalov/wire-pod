"""Read-only view of what Vector photographed while exploring.

Another component writes (never the agent, never in real time):
  DATA_DIR/explore/captures.jsonl  {"id","ts","trigger","object","prox_mm","pose":{"x_mm","y_mm",
                                    "angle_deg","origin_id"},"file"}
  DATA_DIR/explore/analyses.jsonl  {"id","analyzed_at","model","caption","objects":[...],"setting"}
Joined by id. Both files may be missing, empty or have partial/garbled lines.
"""
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import config

log = logging.getLogger("vector-agent")

_TAIL_BYTES = 4_000_000   # only ever read the end of a (growing) file


def _read_jsonl(path) -> list[dict]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue          # partial line being written, or garbage
        if isinstance(d, dict):
            out.append(d)
    return out


def _parse_ts(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=config.TIMEZONE)


def load(hours: float | None = None) -> list[dict]:
    """Joined findings, newest first: capture fields plus 'analysis' (dict or None) and 'when'."""
    d = config.EXPLORE_DIR
    caps = _read_jsonl(d / "captures.jsonl")
    ans = {}
    for a in _read_jsonl(d / "analyses.jsonl"):
        if "id" in a:
            ans[str(a["id"])] = a        # a later line for the same id wins
    now = datetime.now(timezone.utc)
    out = []
    for c in caps:
        when = _parse_ts(c.get("ts"))
        if when is None or "id" not in c:
            continue
        if hours is not None and (now - when).total_seconds() > hours * 3600:
            continue
        out.append({**c, "when": when, "analysis": ans.get(str(c["id"]))})
    out.sort(key=lambda c: c["when"], reverse=True)
    return out


def _ago(when: datetime) -> str:
    s = (datetime.now(timezone.utc) - when).total_seconds()
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 36 * 3600:
        return f"{s / 3600:.0f} h ago"
    return f"{s / 86400:.0f} days ago"


def _place(c: dict, pose) -> str:
    """Where the photo was taken relative to him now, if both are in the same map frame."""
    from . import navmap
    p = c.get("pose") or {}
    try:
        ox, oy, oid = float(p["x_mm"]), float(p["y_mm"]), int(p["origin_id"])
    except (KeyError, TypeError, ValueError):
        return "earlier (position unknown)"
    if pose is None or pose[3] != oid:
        return "earlier, from before my last reboot/re-placement (can't place it on my current map)"
    f, l = navmap._rel(pose[0], pose[1], pose[2], ox, oy)
    if math.hypot(f, l) < 80:
        return "right where I am now"
    return f"taken {navmap._where(f, l)} from where I am now"


def findings_text(query: str = "", pose=None, hours: float | None = None) -> str:
    hours = config.FINDINGS_HOURS if hours is None else hours
    items = load(hours)
    if not items:
        return (f"EXPLORATION: no exploration photos from the last {hours:g} h "
                f"(I may not have explored lately, or nothing was written yet).")
    described = [c for c in items if c["analysis"] and (c["analysis"].get("caption") or c["analysis"].get("setting"))]
    pending = len(items) - len(described)
    terms = [t for t in query.lower().replace(",", " ").split() if len(t) > 2]
    if terms:
        def hay(c):
            a = c["analysis"]
            return " ".join([str(c.get("object") or ""), str(a.get("caption", "")), str(a.get("setting", "")),
                             " ".join(map(str, a.get("objects") or []))]).lower()
        matched = [c for c in described if any(t in hay(c) for t in terms)]
    else:
        matched = described
    head = f"EXPLORATION (last {hours:g} h): {len(items)} photos, {len(described)} described"
    if pending:
        head += f", {pending} not described yet"
    if terms:
        head += f"; {len(matched)} match {query!r}"
    lines = [head + "."]
    if not matched:
        return "\n".join(lines)
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in matched[:config.FINDINGS_MAX]:
        groups[str(c["analysis"].get("setting") or "unknown setting").strip() or "unknown setting"].append(c)
    for setting, cs in groups.items():
        objs: list[str] = []
        for c in cs:
            for o in c["analysis"].get("objects") or []:
                if str(o) not in objs:
                    objs.append(str(o))
        lines.append(f"- {setting} ({len(cs)} photo{'s' if len(cs) != 1 else ''}); objects: "
                     f"{', '.join(objs[:8]) or 'none listed'}")
        for c in cs[:2]:
            cap = str(c["analysis"].get("caption") or "").strip().replace("\n", " ")[:160]
            trig = f", stopped for {c['object']}" if c.get("object") else (
                f", something {round(c['prox_mm'] / 10)} cm ahead" if isinstance(c.get("prox_mm"), (int, float)) else "")
            lines.append(f"    {_ago(c['when'])}{trig}: {cap} [{_place(c, pose)}]")
    if len(matched) > config.FINDINGS_MAX:
        lines.append(f"(+{len(matched) - config.FINDINGS_MAX} older matching photos not shown)")
    return "\n".join(lines)


@tool
def exploration_findings(
        query: Annotated[str, Field(description="Optional word(s) to look for, e.g. 'cup', 'kitchen'. "
                                                "Empty = a summary of everything recent.")] = "") -> str:
    """What Vector noticed and photographed while exploring (descriptions written earlier, not
    a live look): settings, objects and roughly where they were. Use for 'what did you find
    while exploring?', 'did you see a ...?'. Load the 'map' skill first."""
    from . import navmap
    return findings_text(query, navmap.current_pose())
