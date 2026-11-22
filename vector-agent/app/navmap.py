"""Vector's navigation map as something the LLM can reason about.

WireOS builds a quad-tree "memory map" while Vector moves (in memory on the robot, reset on
reboot). The listen-only SDK connection owned by faces.py hands us the robot on every
(re)connect (faces.register_subscriber); here we subscribe to the nav map feed and the
robot state, keep the latest of each cheaply in memory, and decode/summarise lazily when a
tool is called. Nothing here controls the robot.

SDK API used (wirepod_vector_sdk == anki_vector):
  robot.nav_map.init_nav_map_feed(frequency)       -> streams NavMapFeedResponse; `frequency` is a
                          minimum period in SECONDS; the robot only sends when the map changed
  Events.nav_map_update   handler(robot, type, msg): msg.origin_id, msg.map_info.{root_depth,
                          root_size_mm, root_center_x/y}, msg.quad_infos[].{content, depth}
                          (quad-tree leaves in depth-first order, children +x+y, +x-y, -x+y, -x-y)
  Events.robot_state      handler(robot, type, msg): msg.pose.{x,y,origin_id} (mm),
                          msg.pose_angle_rad, msg.status (bit ROBOT_STATUS_IS_ON_CHARGER)
  robot.world.charger / .light_cube                -> objects with .pose (mm, origin_id)

Everything is in mm in the map frame; text for the LLM is egocentric (ahead/left/...) in cm.
"""
import logging
import math
import threading
import time
from typing import Annotated

from langchain_core.tools import tool
from PIL import Image, ImageDraw
from pydantic import Field

from . import config

log = logging.getLogger("vector-agent")

# NavNodeContentType (nav_map.proto)
UNKNOWN, CLEAR_OBSTACLE, CLEAR_CLIFF, OBJ, PROX, PROX_EXPLORED, UNRECOGNIZED, CLIFF, EDGE, NOT_EDGE = range(10)
_FREE = {CLEAR_OBSTACLE, CLEAR_CLIFF, EDGE, NOT_EDGE}   # edges are visual only: not blocking
_CLEAR = {CLEAR_OBSTACLE, CLEAR_CLIFF}
_OBSTACLE = {PROX, PROX_EXPLORED, UNRECOGNIZED}

_lock = threading.Lock()
_state: dict = {
    "msg": None, "msg_at": 0.0, "grid": None,        # latest raw map msg, when, decoded cache
    "pose": None, "pose_at": 0.0,                    # (x_mm, y_mm, angle_rad, origin_id)
    "on_charger": False,
    "charger": None, "cube": None,                   # (x_mm, y_mm, origin_id)
    "charger_hint": None,                            # robot pose when last seen docked
    "connected_at": 0.0,
}
_last_state_at = 0.0


# ---------------------------------------------------------------- SDK thread (no blocking)
def on_connect(robot) -> None:
    """faces.register_subscriber hook: runs in the SDK thread on every (re)connect."""
    from anki_vector.events import Events
    with _lock:
        _state["connected_at"] = time.time()

    def on_map(_r, _t, msg, *_a, **_k):
        with _lock:           # just keep the raw message; decoded lazily on demand
            _state["msg"], _state["msg_at"], _state["grid"] = msg, time.time(), None

    def on_state(r, _t, msg, *_a, **_k):
        global _last_state_at
        now = time.time()
        if now - _last_state_at < 1.0:       # robot_state is frequent; 1 Hz is plenty
            return
        _last_state_at = now
        from anki_vector import status
        p = (float(msg.pose.x), float(msg.pose.y), float(msg.pose_angle_rad), int(msg.pose.origin_id))
        docked = bool(msg.status & status.ROBOT_STATUS_IS_ON_CHARGER)
        ch = cube = None
        try:
            w = r.world
            if w.charger is not None and w.charger.pose is not None:
                q = w.charger.pose
                ch = (float(q.position.x), float(q.position.y), int(q.origin_id))
            if w.light_cube is not None and w.light_cube.pose is not None:
                q = w.light_cube.pose
                cube = (float(q.position.x), float(q.position.y), int(q.origin_id))
        except Exception:
            pass
        with _lock:
            _state["pose"], _state["pose_at"], _state["on_charger"] = p, now, docked
            if docked:
                _state["charger_hint"] = (p[0], p[1], p[3])
            if ch:
                _state["charger"] = ch
            if cube:
                _state["cube"] = cube

    robot.events.subscribe(on_map, Events.nav_map_update)
    robot.events.subscribe(on_state, Events.robot_state)
    nm = robot.nav_map
    _orig_close = nm.close_nav_map_feed

    def _safe_close():
        # SDK bug on py3.8+: closing the feed awaits the cancelled task and raises
        # CancelledError, which would abort Robot.disconnect() and leak the connection.
        try:
            _orig_close()
        except BaseException as e:      # noqa: BLE001
            log.debug("navmap: close_nav_map_feed raised %s (ignored)", type(e).__name__)
    nm.close_nav_map_feed = _safe_close
    # NB: the "frequency" is really a minimum period in SECONDS, and the robot only pushes
    # a (full) map when it has changed since the last push.
    nm.init_nav_map_feed(frequency=config.NAVMAP_FEED_SEC)
    log.info("navmap: subscribed to nav map feed (every >= %.1f s when changed)", config.NAVMAP_FEED_SEC)


def register() -> None:
    from . import faces
    faces.register_subscriber(on_connect)


# ------------------------------------------------------------------------- decode
class Grid:
    """Decoded quad-tree: .leaves = [(cx, cy, size, content)] (mm), point lookup."""

    def __init__(self, origin_id: int, cx: float, cy: float, size: float, depth: int, quads: list):
        self.origin_id = origin_id
        self.leaves: list[tuple] = []
        self._i = 0
        self._quads = quads
        self._root = self._build(cx, cy, size, depth)
        self._quads = None

    def _build(self, cx, cy, size, depth):
        q = self._quads
        if self._i >= len(q) or depth <= 0 or q[self._i][1] >= depth:
            content = q[self._i][0] if self._i < len(q) else UNKNOWN
            self._i += 1
            self.leaves.append((cx, cy, size, content))
            return [cx, cy, size, content, None]
        h, o = size / 2, size / 4
        kids = [self._build(cx + dx * o, cy + dy * o, h, depth - 1)
                for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1))]
        return [cx, cy, size, None, kids]

    def content(self, x: float, y: float) -> int:
        n = self._root
        if abs(x - n[0]) > n[2] / 2 or abs(y - n[1]) > n[2] / 2:
            return UNKNOWN
        while n[4] is not None:
            n = n[4][(2 if x < n[0] else 0) + (1 if y < n[1] else 0)]
        return n[3]


def decode(msg) -> Grid:
    quads = [(int(q.content), int(q.depth)) for q in msg.quad_infos]
    mi = msg.map_info
    return Grid(int(msg.origin_id), float(mi.root_center_x), float(mi.root_center_y),
                float(mi.root_size_mm), int(mi.root_depth), quads)


def _snapshot() -> dict:
    with _lock:
        s = dict(_state)
    if s["msg"] is not None and s["grid"] is None:
        try:
            s["grid"] = decode(s["msg"])
            with _lock:
                if _state["msg"] is s["msg"]:
                    _state["grid"] = s["grid"]
        except Exception as e:
            log.warning("navmap: couldn't decode map (%s)", e)
    return s


# ------------------------------------------------------------------ egocentric helpers
def _rel(px, py, ang, x, y) -> tuple[float, float]:
    """(forward_mm, left_mm) of map point (x, y) as seen from pose (px, py, ang)."""
    dx, dy = x - px, y - py
    return dx * math.cos(ang) + dy * math.sin(ang), -dx * math.sin(ang) + dy * math.cos(ang)


_SECTORS = ["straight ahead", "ahead-left", "to the left", "behind-left", "behind",
            "behind-right", "to the right", "ahead-right"]


def sector(fwd: float, left: float) -> str:
    b = math.degrees(math.atan2(left, fwd)) % 360     # 0 = ahead, counter-clockwise = left
    return _SECTORS[int(((b + 22.5) % 360) // 45)]


def _where(fwd: float, left: float) -> str:
    return f"{sector(fwd, left)} ~{round(math.hypot(fwd, left) / 10)} cm"


def _dist_to_leaf(leaf, px, py) -> tuple[float, float, float]:
    """(distance, nearest_x, nearest_y) from point to the leaf square."""
    cx, cy, s, _ = leaf
    h = s / 2
    nx, ny = min(max(px, cx - h), cx + h), min(max(py, cy - h), cy + h)
    return math.hypot(nx - px, ny - py), nx, ny


def _ray(grid: Grid, px, py, ang, max_mm=1500, step=20) -> tuple[int, str]:
    """March from the robot along bearing `ang`: (clear mm, what ended it)."""
    d = 40
    while d <= max_mm:
        c = grid.content(px + d * math.cos(ang), py + d * math.sin(ang))
        if c not in _FREE:
            why = {CLIFF: "a drop (cliff/edge)", OBJ: "a recognised object", UNKNOWN: "unexplored space"}.get(
                c, "an obstacle")
            return d - step, why
        d += step
    return max_mm, "nothing within 1.5 m"


def _cluster(leaves: list, gap: float = 30.0) -> list[list]:
    """Greedy grouping of touching/near leaves of the same kind."""
    groups: list[list] = []
    for lf in leaves:
        hit = [g for g in groups if any(
            abs(lf[0] - o[0]) <= (lf[2] + o[2]) / 2 + gap and abs(lf[1] - o[1]) <= (lf[2] + o[2]) / 2 + gap
            for o in g)]
        merged = [lf]
        for g in hit:
            merged.extend(g)
            groups.remove(g)
        groups.append(merged)
    return groups


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    return f"{seconds / 3600:.1f} h ago"


# ---------------------------------------------------------------------------- summary
def current_pose() -> tuple | None:
    """(x_mm, y_mm, angle_rad, origin_id) or None. Used by the exploration module too."""
    with _lock:
        return _state["pose"]


def summarise(s: dict | None = None) -> str:
    s = s or _snapshot()
    now = time.time()
    if s["pose"] is None and s["grid"] is None:
        return ("MAP: I have no map data right now (my map link isn't connected or hasn't "
                "reported yet). I can't say where I am.")
    lines: list[str] = []
    pose, grid = s["pose"], s["grid"]
    px = py = ang = None
    origin = None
    if pose:
        px, py, ang, origin = pose
    pose_ok = pose is not None and (grid is None or grid.origin_id == origin)

    # --- header: freshness + honesty
    if grid is not None:
        lines.append(f"MAP (updated {_ago(now - s['msg_at'])}; short-term memory: empty after a reboot or "
                     f"re-placement, covers only what I've seen since; positions drift a little).")
    else:
        lines.append("MAP: no map has reached me since my link connected (the robot only sends the map "
                     "when it changes), so I can't describe my surroundings yet, only my position.")

    # --- own position vs charger
    ch = None
    if s["charger"] and (origin is None or s["charger"][2] == origin):
        ch = s["charger"][:2]
    elif s["charger_hint"] and (origin is None or s["charger_hint"][2] == origin):
        ch = s["charger_hint"][:2]
    if s["on_charger"]:
        lines.append("You: sitting on my charger right now.")
    elif pose and ch:
        f, l = _rel(px, py, ang, *ch)
        # his heading relative to the straight line to the charger
        lines.append(f"You: my charger is {_where(f, l)} from me.")
    elif pose:
        lines.append("You: I don't know where my charger is on this map (haven't seen it since the map started).")

    if grid is None or not pose_ok:
        if grid is not None and pose is None:
            lines.append("I haven't received my own position yet, so I can't place myself on the map.")
        elif grid is not None and not pose_ok:
            lines.append("My map and my position are from different placements, so I can't relate them.")
        return "\n".join(lines)

    # --- coverage
    leaves = grid.leaves
    known = [lf for lf in leaves if lf[3] != UNKNOWN]
    if not known:
        lines.append("Seen so far: nothing yet; the map is empty (I probably just woke up or was placed down).")
        return "\n".join(lines)
    x0 = min(l[0] - l[2] / 2 for l in known)
    x1 = max(l[0] + l[2] / 2 for l in known)
    y0 = min(l[1] - l[2] / 2 for l in known)
    y1 = max(l[1] + l[2] / 2 for l in known)
    clear_m2 = sum(l[2] ** 2 for l in known if l[3] in _CLEAR) / 1e6
    lines.append(f"Seen so far: an area about {round((x1 - x0) / 10)} x {round((y1 - y0) / 10)} cm "
                 f"(bounding box, so the real shape is smaller), with ~{clear_m2:.2f} m2 checked as clear; "
                 f"everything outside is unknown, NOT empty.")

    # --- clear space in the four directions
    parts = []
    for name, off in (("ahead", 0), ("to the left", 90), ("to the right", -90), ("behind", 180)):
        mm, why = _ray(grid, px, py, ang + math.radians(off))
        parts.append(f"{name} {round(mm / 10)} cm then {why}")
    lines.append("Around you (clear distance I've seen, then what stops it): " + "; ".join(parts) + ".")

    # --- drops / obstacles / objects relative to him
    def nearest_groups(kinds, limit=3):
        """Group touching cells; for each group: nearest point + rough size."""
        out = []
        for g in _cluster([lf for lf in known if lf[3] in kinds], gap=40):
            d, nx, ny = min(_dist_to_leaf(lf, px, py) for lf in g)
            f, l = _rel(px, py, ang, nx, ny)
            span = max(max(lf[0] + lf[2] / 2 for lf in g) - min(lf[0] - lf[2] / 2 for lf in g),
                       max(lf[1] + lf[2] / 2 for lf in g) - min(lf[1] - lf[2] / 2 for lf in g))
            out.append((d, f"nearest {_where(f, l)}, roughly {max(1, round(span / 10))} cm across"))
        return "; ".join(t for _, t in sorted(out)[:limit])

    drops = nearest_groups({CLIFF})
    lines.append("Drops (desk edge/cliff): "
                 + (drops + "." if drops else "none detected so far (unknown beyond what I've explored)."))
    obs = nearest_groups(_OBSTACLE)
    if obs:
        lines.append("Obstacles (found with my distance sensor, may be small): " + obs + ".")

    objs = _cluster([lf for lf in known if lf[3] == OBJ], gap=40)
    notes = []
    for g in objs[:4]:
        cx = sum(l[0] for l in g) / len(g)
        cy = sum(l[1] for l in g) / len(g)
        label = "recognised object"
        if ch and math.hypot(cx - ch[0], cy - ch[1]) < 150:
            label = "charger"
        elif s["cube"] and s["cube"][2] == origin and math.hypot(cx - s["cube"][0], cy - s["cube"][1]) < 150:
            label = "cube"
        f, l = _rel(px, py, ang, cx, cy)
        notes.append(f"{label} {_where(f, l)}")
    if s["cube"] and s["cube"][2] == origin and not any(n.startswith("cube") for n in notes):
        f, l = _rel(px, py, ang, *s["cube"][:2])
        notes.append(f"cube {_where(f, l)}")
    if notes:
        lines.append("Objects: " + "; ".join(notes) + ".")

    edges = sum(1 for lf in known if lf[3] == EDGE)
    if edges:
        lines.append(f"Visual edges (changes in colour/texture, low reliability): {edges} spots.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- picture
_COL = {CLEAR_OBSTACLE: (0, 110, 90), CLEAR_CLIFF: (0, 170, 120), OBJ: (80, 160, 255),
        PROX: (255, 140, 0), PROX_EXPLORED: (255, 140, 0), UNRECOGNIZED: (255, 170, 40),
        CLIFF: (255, 40, 40), EDGE: (170, 90, 255), NOT_EDGE: (60, 40, 90)}


def render(s: dict | None = None) -> Image.Image | None:
    """Top-down map on the face screen, rotated so 'up' is the way he's facing."""
    s = s or _snapshot()
    grid, pose = s["grid"], s["pose"]
    if grid is None or pose is None or grid.origin_id != pose[3]:
        return None
    px, py, ang, origin = pose
    W, H = config.FACE_W, config.FACE_H
    known = [l for l in grid.leaves if l[3] != UNKNOWN]
    ch = s["charger"] if s["charger"] and s["charger"][2] == origin else (
        s["charger_hint"] if s["charger_hint"] and s["charger_hint"][2] == origin else None)

    def tr(x, y):  # map mm -> robot-relative (right, up) mm, i.e. screen axes
        f, l = _rel(px, py, ang, x, y)
        return -l, f

    pts = [(0.0, 0.0)]
    for cx, cy, sz, _ in known:
        h = sz / 2
        pts += [tr(cx + dx * h, cy + dy * h) for dx in (-1, 1) for dy in (-1, 1)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pad = 40.0
    minx, maxx, miny, maxy = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    scale = min((W - 4) / (maxx - minx), (H - 4) / (maxy - miny), 0.3)  # px/mm, cap 3 px/cm
    ox = (W - (maxx - minx) * scale) / 2 - minx * scale
    oy = (H + (maxy - miny) * scale) / 2 + miny * scale

    def px_(x, y):
        return ox + x * scale, oy - y * scale

    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for cx, cy, sz, c in sorted(known, key=lambda l: l[3] in _CLEAR, reverse=True):
        h = sz / 2
        quad = [px_(*tr(cx + dx * h, cy + dy * h)) for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        d.polygon(quad, fill=_COL.get(c, (90, 90, 90)))
    if ch:
        cx, cy = px_(*tr(*ch[:2]))
        d.rectangle([cx - 3, cy - 3, cx + 3, cy + 3], outline=(60, 255, 90), width=1)
    rx, ry = px_(0, 0)   # robot: white arrow pointing up
    d.polygon([(rx, ry - 6), (rx - 4, ry + 4), (rx + 4, ry + 4)], fill=(255, 255, 255))
    bar = 100 * scale   # 10 cm scale bar
    d.line([(3, H - 4), (3 + bar, H - 4)], fill=(255, 255, 255))
    return img


@tool
async def show_map() -> str:
    """Show Vector's navigation map as a tiny top-down picture on his face screen (up is the
    way he's facing; white arrow = him, green square = charger, red = drops/desk edge,
    orange = obstacles, blue = objects, teal = space he has checked). Load the 'map' skill
    first. Shown BEFORE you speak."""
    from . import face
    img = render()
    if img is None:
        return "error: no usable map yet (" + summarise().splitlines()[0] + ")"
    await face._display(face._save(img), 6)
    return "showed my map on my face (up = the way I'm facing)"


@tool
def where_am_i() -> str:
    """Describe where Vector is and what's around him, from his own navigation map (built
    while he moves; resets on reboot), plus what he photographed and noticed while
    exploring. Use for 'where are you?', 'what's around you?', 'how big is your desk?',
    'where's your charger?', 'are you near the edge?'. Load the 'map' skill first."""
    from . import explore
    return summarise() + "\n" + explore.findings_text("", current_pose())
