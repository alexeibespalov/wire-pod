"""Physical maneuvers: validated step lists, a persistent catalogue, a runner on the body
engine (app/body.py), a kinematic dry-run, and the agent tools that use them.

A maneuver is a list of steps. Numbers may be "$param" strings, resolved from the maneuver's
declared params (default/min/max, clamped). Maneuvers may call other maneuvers (depth <= 4,
no cycles) and repeat/branch; at most 200 steps are ever expanded. Everything physical runs
through ONE body-engine batch (one control acquisition, one governor).

Catalogue: DATA_DIR/maneuvers/<slug>.json   (<= 100, last 5 versions kept, atomic writes)
Log:       DATA_DIR/maneuvers/attempts.jsonl (one line per run; outcome filled by record_attempt)
"""
import asyncio
import contextlib
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from langchain_core.tools import tool
from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field,
                      TypeAdapter, ValidationError, model_validator)

from . import body

MAX_MANEUVERS = 100
MAX_EXPANDED = 200
MAX_DEPTH = 4            # maneuver-calls-maneuver nesting
MAX_BLOCK_DEPTH = 4      # repeat/if nesting inside one maneuver
MAX_REPEAT = 20
KEEP_VERSIONS = 5
EMA_ALPHA = 0.2
MAX_ATTEMPT_LINES = 500
TRACK_MM = body.BODY_TRACK_MM
HEAD_LIFT_S = 0.5        # assumed time of a head/lift move in the dry run
_OUTCOMES = {"success": 1.0, "partial": 0.5, "failure": 0.0}


class ManeuverError(ValueError):
    """Bad maneuver (message is plain words for the model)."""


# ===================================================================== schema
def _num(v):
    if isinstance(v, bool):
        raise ValueError("number or '$param'")
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", s):
            return s
        try:
            return float(s)
        except ValueError:
            raise ValueError("number or '$param'") from None
    return v


Num = Annotated[Union[float, str], BeforeValidator(_num)]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StopWhen(_M):
    prox_below_mm: Num | None = None
    touch: bool | None = None
    cube_moved: bool | None = None


class Drive(_M):
    step: Literal["drive"]
    mm: Num
    speed: Num = 60
    fast: bool = False
    stop_when: StopWhen | None = None


class Turn(_M):
    step: Literal["turn"]
    deg: Num


class Wheels(_M):
    step: Literal["wheels"]
    left: Num
    right: Num
    ms: Num


class Head(_M):
    step: Literal["head"]
    deg: Num


class Lift(_M):
    step: Literal["lift"]
    height: Num


class Wait(_M):
    step: Literal["wait"]
    ms: Num


class Call(_M):
    step: Literal["maneuver"]
    name: str
    args: dict[str, Num] = Field(default_factory=dict)


class Sensor(_M):
    prox_below_mm: Num | None = None
    touch: bool | None = None
    cube_visible: bool | None = None

    @model_validator(mode="after")
    def _one(self):
        if sum(v is not None for v in (self.prox_below_mm, self.touch, self.cube_visible)) != 1:
            raise ValueError("sensor needs exactly one of prox_below_mm, touch, cube_visible")
        return self


class Repeat(_M):
    step: Literal["repeat"]
    times: Num
    steps: list["Step"]


class If(_M):
    step: Literal["if"]
    sensor: Sensor
    then: list["Step"]
    else_: list["Step"] = Field(default_factory=list, alias="else")


Step = Annotated[Union[Drive, Turn, Wheels, Head, Lift, Wait, Call, Repeat, If], Field(discriminator="step")]
Repeat.model_rebuild()
If.model_rebuild()
_STEPS = TypeAdapter(list[Step])


class Param(_M):
    default: float
    min: float | None = None
    max: float | None = None
    description: str = ""

    @model_validator(mode="after")
    def _ok(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min > max")
        if self.min is not None:
            self.default = max(self.min, self.default)
        if self.max is not None:
            self.default = min(self.max, self.default)
        return self


_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,23}")


def parse_params(raw: dict | None) -> dict[str, Param]:
    out: dict[str, Param] = {}
    for k, v in (raw or {}).items():
        if not _PARAM_NAME.fullmatch(str(k)):
            raise ManeuverError(f"bad param name {k!r} (letters/digits/_ only)")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = {"default": v}
        try:
            out[k] = Param.model_validate(v)
        except ValidationError as e:
            raise ManeuverError(f"param {k}: {_fmt_err(e)}") from None
    if len(out) > 8:
        raise ManeuverError("at most 8 params")
    return out


def _fmt_err(e: ValidationError) -> str:
    bits = []
    for err in e.errors()[:3]:
        loc = ".".join(str(x) for x in err["loc"] if not str(x).startswith(("function-", "list[")))
        bits.append(f"{loc}: {err['msg']}")
    return "; ".join(bits)


def parse_steps(raw: Any) -> list:
    raw = _coerce_list(raw)
    try:
        return _STEPS.validate_python(raw)
    except ValidationError as e:
        raise ManeuverError("bad steps - " + _fmt_err(e)) from None


def _coerce_list(x: Any) -> list:
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            raise ManeuverError("steps must be a JSON list") from None
    if isinstance(x, dict):
        x = [x]
    if not isinstance(x, list) or not x:
        raise ManeuverError("steps must be a non-empty list")
    return x


def _dump(steps: list) -> list[dict]:
    return [s.model_dump(by_alias=True, exclude_none=True) for s in steps]


def _refs(step) -> list[str]:
    """All $param references directly on this step (not its children)."""
    out = []
    vals = []
    if isinstance(step, (Drive, Turn, Wheels, Head, Lift, Wait)):
        vals = [getattr(step, f) for f in type(step).model_fields if f not in ("step", "stop_when")]
        if isinstance(step, Drive) and step.stop_when:
            vals.append(step.stop_when.prox_below_mm)
    elif isinstance(step, Call):
        vals = list(step.args.values())
    elif isinstance(step, Repeat):
        vals = [step.times]
    elif isinstance(step, If):
        vals = [step.sensor.prox_below_mm]
    return [v for v in vals if isinstance(v, str)]


# ================================================================== catalogue
def catalogue_dir() -> Path:
    from . import config
    return Path(config.DATA_DIR) / "maneuvers"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:40]
    if not s:
        raise ManeuverError("give the maneuver a short name (letters/digits)")
    return s


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Catalogue:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, slug: str) -> Path:
        return self.root / f"{slug}.json"

    def _write(self, path: Path, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json")) if self.root.exists() else []

    def get(self, name: str) -> dict | None:
        try:
            slug = slugify(name)
        except ManeuverError:
            return None
        p = self._path(slug)
        if not p.exists():
            hits = [n for n in self.names() if n.startswith(slug)]
            if len(hits) != 1:
                return None
            p = self._path(hits[0])
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and "steps" in d else None   # ignore stray files

    def all(self) -> list[dict]:
        out = []
        for n in self.names():
            d = self.get(n)
            if d:
                out.append(d)
        return out

    def put(self, doc: dict) -> None:
        with self._lock:
            self._write(self._path(doc["slug"]), json.dumps(doc, indent=1))

    def delete(self, name: str) -> bool:
        d = self.get(name)
        if not d:
            return False
        with self._lock:
            with contextlib.suppress(FileNotFoundError):
                self._path(d["slug"]).unlink()
        return True

    # ------------------------------------------------------------------ save
    def save(self, name: str, description: str, steps: Any, params: dict | None = None,
             tags: list[str] | None = None, author: str = "vector") -> dict:
        slug = slugify(name)
        pdecl = parse_params(params)
        parsed = self.validate(steps, pdecl, self_slug=slug)
        with self._lock:
            old = self.get(slug)
            if old is None and len(self.names()) >= MAX_MANEUVERS:
                raise ManeuverError(f"I already know {MAX_MANEUVERS} maneuvers; delete one first")
            new_steps = _dump(parsed)
            new_params = {k: v.model_dump(exclude_none=True) for k, v in pdecl.items()}
            now = _iso()
            if old:
                changed = old["steps"] != new_steps or old["params"] != new_params
                hist = list(old.get("history", []))
                if changed:
                    hist.append({"version": old["version"], "updated": old["updated"],
                                 "description": old["description"], "params": old["params"],
                                 "steps": old["steps"]})
                doc = dict(old, description=description.strip()[:200] or old["description"],
                           params=new_params, steps=new_steps, updated=now,
                           tags=_tags(tags, old.get("tags")),
                           history=hist[-KEEP_VERSIONS:],
                           version=old["version"] + (1 if changed else 0),
                           status="draft" if changed else old["status"])
            else:
                doc = {"name": name.strip()[:60], "slug": slug, "description": description.strip()[:200],
                       "params": new_params, "steps": new_steps, "tags": _tags(tags, None),
                       "status": "draft", "author": author, "created": now, "updated": now,
                       "version": 1, "history": [],
                       "stats": {"runs": 0, "successes": 0, "partials": 0, "failures": 0,
                                 "competency": None, "last_used": None}}
            self.put(doc)
            return doc

    # -------------------------------------------------------------- validation
    def validate(self, steps: Any, pdecl: dict[str, Param], self_slug: str | None = None) -> list:
        parsed = parse_steps(steps)
        cache: dict[str, tuple[list, dict[str, Param]]] = {}

        def callee(name: str):
            d = self.get(name)
            if not d:
                raise ManeuverError(f"unknown maneuver {name!r}")
            if d["slug"] not in cache:
                cache[d["slug"]] = (parse_steps(d["steps"]), parse_params(d["params"]))
            return d["slug"], cache[d["slug"]]

        def walk(sts: list, decl: dict[str, Param], stack: tuple, mdepth: int, bdepth: int) -> int:
            if bdepth > MAX_BLOCK_DEPTH:
                raise ManeuverError(f"repeat/if nested deeper than {MAX_BLOCK_DEPTH}")
            n = 0
            for s in sts:
                for ref in _refs(s):
                    if ref[1:] not in decl:
                        raise ManeuverError(f"'{ref}' is not a declared param")
                if isinstance(s, Repeat):
                    t = s.times
                    if isinstance(t, str):
                        p = decl[t[1:]]
                        times = int(p.max if p.max is not None else p.default)
                    else:
                        times = int(t)
                        if not 1 <= times <= MAX_REPEAT:
                            raise ManeuverError(f"repeat times must be 1..{MAX_REPEAT}")
                    n += max(1, times) * walk(s.steps, decl, stack, mdepth, bdepth + 1)
                elif isinstance(s, If):
                    n += 1 + max(walk(s.then, decl, stack, mdepth, bdepth + 1),
                                 walk(s.else_, decl, stack, mdepth, bdepth + 1) if s.else_ else 0)
                elif isinstance(s, Call):
                    slug = slugify(s.name)
                    if slug in stack:
                        raise ManeuverError("maneuvers call each other in a loop: " +
                                            " -> ".join(stack + (slug,)))
                    if mdepth + 1 > MAX_DEPTH:
                        raise ManeuverError(f"maneuvers nested deeper than {MAX_DEPTH}")
                    real, (csteps, cdecl) = callee(s.name)
                    if real in stack:
                        raise ManeuverError("maneuvers call each other in a loop: " +
                                            " -> ".join(stack + (real,)))
                    for k in s.args:
                        if k not in cdecl:
                            raise ManeuverError(f"maneuver {s.name!r} has no param {k!r}")
                    n += walk(csteps, cdecl, stack + (real,), mdepth + 1, 1)
                else:
                    n += 1
                if n > MAX_EXPANDED:
                    raise ManeuverError(f"too long: more than {MAX_EXPANDED} steps once expanded")
            return n

        walk(parsed, pdecl, (self_slug,) if self_slug else (), 0, 1)
        return parsed

    # ------------------------------------------------------------------ stats
    def mark_run(self, name: str) -> None:
        with self._lock:
            d = self.get(name)
            if d:
                d["stats"]["runs"] += 1
                d["stats"]["last_used"] = _iso()
                self.put(d)

    def record_outcome(self, name: str, outcome: str) -> dict:
        if outcome not in _OUTCOMES:
            raise ManeuverError("outcome must be success, partial or failure")
        with self._lock:
            d = self.get(name)
            if not d:
                raise ManeuverError(f"no saved maneuver called {name!r}")
            st = d["stats"]
            key = {"success": "successes", "partial": "partials", "failure": "failures"}[outcome]
            st[key] += 1
            v = _OUTCOMES[outcome]
            c = st.get("competency")
            st["competency"] = round(v if c is None else (1 - EMA_ALPHA) * c + EMA_ALPHA * v, 4)
            st["last_used"] = _iso()
            if outcome == "success" and d["status"] == "draft":
                d["status"] = "tested"
            self.put(d)
            return d

    def set_param_default(self, name: str, param: str, value: float) -> dict:
        with self._lock:
            d = self.get(name)
            if not d or param not in d["params"]:
                raise ManeuverError("no such maneuver/param")
            p = d["params"][param]
            lo, hi = p.get("min"), p.get("max")
            v = value if lo is None else max(lo, value)
            p["default"] = v if hi is None else min(hi, v)
            d["updated"] = _iso()
            self.put(d)
            return d


def _tags(new: list[str] | None, old: list[str] | None) -> list[str]:
    src = new if new is not None else (old or [])
    return sorted({str(t).strip().lower()[:20] for t in src if str(t).strip()})[:8]


def catalogue() -> Catalogue:
    return Catalogue(catalogue_dir())


# ================================================================= attempts log
class AttemptLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows[-MAX_ATTEMPT_LINES:]))
        os.replace(tmp, self.path)

    def append(self, maneuver: str, args: dict, result: dict) -> str:
        aid = uuid.uuid4().hex[:6]
        row = {"id": aid, "time": _iso(), "maneuver": maneuver, "args": args,
               "result": result, "outcome": None, "note": None}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            if self.path.stat().st_size > 400_000:
                self._write(self._read())
        return aid

    def fill(self, maneuver: str, outcome: str, note: str) -> str | None:
        """Fill the newest still-open attempt of this maneuver. Returns its id."""
        with self._lock:
            rows = self._read()
            for r in reversed(rows):
                if r.get("maneuver") == maneuver and r.get("outcome") is None:
                    r["outcome"], r["note"] = outcome, note[:200]
                    self._write(rows)
                    return r["id"]
        return None

    def recent(self, maneuver: str | None = None, n: int = 5) -> list[dict]:
        rows = [r for r in self._read() if maneuver is None or r.get("maneuver") == maneuver]
        return rows[-n:]


def attempts() -> AttemptLog:
    return AttemptLog(catalogue_dir() / "attempts.jsonl")


# ====================================================================== runner
def _clamp(v: float, p: Param) -> float:
    if p.min is not None:
        v = max(p.min, v)
    if p.max is not None:
        v = min(p.max, v)
    return v


def bind_args(decl: dict[str, Param], args: dict | None) -> dict[str, float]:
    args = dict(args or {})
    unknown = [k for k in args if k not in decl]
    if unknown:
        raise ManeuverError(f"unknown arg(s) {unknown}; params are {sorted(decl) or 'none'}")
    vals = {}
    for k, p in decl.items():
        try:
            v = float(args[k]) if k in args else p.default
        except (TypeError, ValueError):
            raise ManeuverError(f"arg {k} must be a number") from None
        vals[k] = _clamp(v, p)
    return vals


def _r(v, vals: dict[str, float]) -> float:
    """Resolve a number or '$param'."""
    if isinstance(v, str):
        return vals[v[1:]]
    return float(v)


class _Ctx:
    def __init__(self, batch, cat: Catalogue) -> None:
        self.b, self.cat = batch, cat
        self.count = 0
        self.sensors_changed = 0


def _sense(sensor: Sensor, vals: dict) -> bool:
    s = body.snapshot()
    if sensor.prox_below_mm is not None:
        return body.prox_close(s, _r(sensor.prox_below_mm, vals))
    if sensor.touch is not None:
        return bool(s["touched"]) == bool(sensor.touch)
    return bool(s["cube_visible"]) == bool(sensor.cube_visible)


async def _exec(steps: list, vals: dict, ctx: _Ctx) -> None:
    for s in steps:
        if ctx.b.aborted:
            return
        if isinstance(s, Repeat):
            for _ in range(max(1, min(MAX_REPEAT, int(_r(s.times, vals))))):
                await _exec(s.steps, vals, ctx)
                if ctx.b.aborted:
                    return
            continue
        ctx.count += 1
        if ctx.count > MAX_EXPANDED:
            raise ManeuverError(f"too long: more than {MAX_EXPANDED} steps")
        b = ctx.b
        if isinstance(s, If):
            await _exec(s.then if _sense(s.sensor, vals) else s.else_, vals, ctx)
        elif isinstance(s, Call):
            d = ctx.cat.get(s.name)
            if not d:
                raise ManeuverError(f"unknown maneuver {s.name!r}")
            decl = parse_params(d["params"])
            sub = bind_args(decl, {k: _r(v, vals) for k, v in s.args.items()})
            await _exec(parse_steps(d["steps"]), sub, ctx)
        elif isinstance(s, Drive):
            sw = None
            if s.stop_when:
                sw = {"prox_below_mm": _r(s.stop_when.prox_below_mm, vals) if s.stop_when.prox_below_mm is not None else None,
                      "touch": s.stop_when.touch, "cube_moved": s.stop_when.cube_moved}
            await b.drive(_r(s.mm, vals), _r(s.speed, vals), s.fast, sw)
        elif isinstance(s, Turn):
            await b.turn(_r(s.deg, vals))
        elif isinstance(s, Wheels):
            await b.wheels(_r(s.left, vals), _r(s.right, vals), _r(s.ms, vals))
        elif isinstance(s, Head):
            await b.head(_r(s.deg, vals))
        elif isinstance(s, Lift):
            await b.lift(_r(s.height, vals))
        elif isinstance(s, Wait):
            await b.wait(_r(s.ms, vals))


async def run_steps(steps: Any, params: dict | None = None, args: dict | None = None,
                    label: str = "move", cat: Catalogue | None = None,
                    self_slug: str | None = None) -> dict:
    """Validate, then execute under one control acquisition + one governor batch.
    Returns the batch summary dict (adds `error` if it couldn't start)."""
    cat = cat or catalogue()
    decl = parse_params(params)
    parsed = cat.validate(steps, decl, self_slug=self_slug)
    vals = bind_args(decl, args)
    try:
        async with body.ENGINE.batch(label) as b:
            ctx = _Ctx(b, cat)
            await _exec(parsed, vals, ctx)
    except body.BodyError as e:
        return {"error": str(e)}
    out = b.summary()
    out["start"], out["end"] = b.start, b.end or body.snapshot()
    s0, s1 = out["start"], out["end"]
    out["disp_mm"] = round(body._travel(s0, s1), 1) if s0 and s1 else 0.0
    out["steps"] = ctx.count
    return out


def format_summary(label: str, r: dict) -> str:
    if "error" in r:
        return f"error: {r['error']}"
    bits = [f"{r['steps']} steps", f"moved {r['disp_mm']:.0f}mm", f"turned {r['heading_deg']:.0f}deg"]
    if r["prox_before"] is not None or r["prox_after"] is not None:
        bits.append(f"distance ahead {r['prox_before']}->{r['prox_after']}mm")
    if r.get("cube_moved"):
        bits.append("cube moved")
    bits.append(f"{r['duration_s']:.1f}s")
    txt = f"{label}: " + ", ".join(bits)
    if r["stop_reason"]:
        txt += f". {r['stop_reason']}"
    if r["notes"]:
        txt += f" ({'; '.join(r['notes'])})"
    return txt


async def return_home(start: dict, end: dict, passes: int = 3) -> dict:
    """Drive back to the pose where a trial started (same map only). Closed loop: re-reads
    the pose after each pass and corrects, because real drives under/overshoot."""
    cur, out = end, {}
    for _ in range(passes):
        if start["origin_id"] != cur["origin_id"]:
            return {"error": "can't return: map changed"}
        dx, dy = start["x"] - cur["x"], start["y"] - cur["y"]
        dist = math.hypot(dx, dy)
        steps: list[dict] = []
        heading = cur["heading_deg"]
        if dist > 15:
            bearing = math.degrees(math.atan2(dy, dx))
            steps.append({"step": "turn", "deg": round(body.wrap180(bearing - heading), 1)})
            left = dist
            while left > 1:
                c = min(left, body.BODY_MAX_DRIVE_MM)
                steps.append({"step": "drive", "mm": round(c, 1), "speed": 60})
                left -= c
            heading = bearing
        fix = body.wrap180(start["heading_deg"] - heading)
        if abs(fix) > 4:
            steps.append({"step": "turn", "deg": round(fix, 1)})
        if not steps:
            return out
        out = await run_steps(steps, label="return")
        if out.get("error") or out.get("aborted"):
            return out
        cur = body.snapshot()
    return out


# ================================================================== dry run
def _flatten(steps: list, vals: dict, cat: Catalogue, assume: bool, out: list, depth: int = 0) -> None:
    for s in steps:
        if len(out) > MAX_EXPANDED:
            return
        if isinstance(s, Repeat):
            for _ in range(max(1, min(MAX_REPEAT, int(_r(s.times, vals))))):
                _flatten(s.steps, vals, cat, assume, out, depth)
        elif isinstance(s, If):
            _flatten(s.then if assume else s.else_, vals, cat, assume, out, depth)
        elif isinstance(s, Call):
            d = cat.get(s.name)
            if not d or depth >= MAX_DEPTH:
                raise ManeuverError(f"can't expand maneuver {s.name!r}")
            sub = bind_args(parse_params(d["params"]), {k: _r(v, vals) for k, v in s.args.items()})
            _flatten(parse_steps(d["steps"]), sub, cat, assume, out, depth + 1)
        else:
            out.append((s, vals))


def dry_run(steps: Any, params: dict | None = None, args: dict | None = None,
            cat: Catalogue | None = None, assume_sensors: bool = False) -> dict:
    """Kinematic simulation (differential drive, track ~48 mm). Nothing moves.
    `if` steps take the else branch unless assume_sensors=True. Returns predicted end pose,
    max distance from start, total time, and any governor violations (what would be capped
    or aborted)."""
    cat = cat or catalogue()
    decl = parse_params(params)
    parsed = cat.validate(steps, decl)
    vals = bind_args(decl, args)
    flat: list = []
    _flatten(parsed, vals, cat, assume_sensors, flat)
    viol: list[str] = []
    if len(flat) > MAX_EXPANDED:
        viol.append(f"more than {MAX_EXPANDED} steps")
        flat = flat[:MAX_EXPANDED]
    x = y = th = 0.0   # th in radians
    t_total = maxd = 0.0
    for i, (s, v) in enumerate(flat, 1):
        dt = 0.0
        tag = f"step {i} ({s.step})"
        if isinstance(s, Drive):
            mm, sp = _r(s.mm, v), abs(_r(s.speed, v))
            cap = body.BODY_FAST_SPEED_MMPS if s.fast else body.BODY_MAX_SPEED_MMPS
            if sp > cap:
                viol.append(f"{tag}: speed {sp:.0f} > {cap:.0f} mm/s, will be capped")
            sp = max(10.0, min(sp, cap))
            if abs(mm) > body.BODY_MAX_DRIVE_MM:
                viol.append(f"{tag}: {mm:.0f}mm > {body.BODY_MAX_DRIVE_MM:.0f}mm per step, will be capped")
                mm = math.copysign(body.BODY_MAX_DRIVE_MM, mm)
            x += mm * math.cos(th)
            y += mm * math.sin(th)
            dt = abs(mm) / sp
        elif isinstance(s, Turn):
            d = _r(s.deg, v)
            if abs(d) > 360:
                viol.append(f"{tag}: turn > 360deg, will be capped")
                d = math.copysign(360, d)
            th += math.radians(d)
            dt = abs(d) / body.TURN_SPEED_DPS
        elif isinstance(s, Wheels):
            cap = body.BODY_MAX_SPEED_MMPS
            lw, rw, ms = _r(s.left, v), _r(s.right, v), _r(s.ms, v)
            if max(abs(lw), abs(rw)) > cap:
                viol.append(f"{tag}: wheel speed > {cap:.0f} mm/s, will be capped")
                lw, rw = max(-cap, min(cap, lw)), max(-cap, min(cap, rw))
            if ms > body.BODY_MAX_WHEELS_MS:
                viol.append(f"{tag}: {ms:.0f}ms > {body.BODY_MAX_WHEELS_MS:.0f}ms per step, will be capped")
                ms = body.BODY_MAX_WHEELS_MS
            dt = max(0.0, ms) / 1000
            vv, w = (lw + rw) / 2, (rw - lw) / TRACK_MM
            if abs(w) < 1e-9:
                x += vv * dt * math.cos(th)
                y += vv * dt * math.sin(th)
            else:
                th1 = th + w * dt
                x += vv / w * (math.sin(th1) - math.sin(th))
                y += -vv / w * (math.cos(th1) - math.cos(th))
                th = th1
        elif isinstance(s, (Head, Lift)):
            dt = HEAD_LIFT_S
        elif isinstance(s, Wait):
            dt = max(0.0, min(5000.0, _r(s.ms, v))) / 1000
        t_total += dt
        maxd = max(maxd, math.hypot(x, y))
    if maxd > body.BODY_LEASH_MM:
        viol.append(f"strays {maxd:.0f}mm from the start, leash is {body.BODY_LEASH_MM:.0f}mm: will abort")
    if t_total > body.BODY_MAX_MANEUVER_S:
        viol.append(f"takes {t_total:.0f}s, limit is {body.BODY_MAX_MANEUVER_S:.0f}s: will abort")
    return {"ok": not viol, "steps": len(flat),
            "end": {"x_mm": round(x, 1), "y_mm": round(y, 1),
                    "heading_deg": round(body.wrap180(math.degrees(th)), 1)},
            "max_dist_mm": round(maxd, 1), "total_s": round(t_total, 2), "violations": viol}


# =================================================================== helpers
def known_maneuvers_prompt(limit: int = 15) -> str:
    """'MANEUVERS YOU KNOW' section for the system prompt (best first)."""
    try:
        docs = catalogue().all()
    except Exception:
        docs = []
    if not docs:
        return "MANEUVERS YOU KNOW\n(none saved yet; make some with save_maneuver)\n"
    docs.sort(key=lambda d: (d["stats"].get("competency") is None, -(d["stats"].get("competency") or 0),
                             d["name"]))
    lines = []
    for d in docs[:limit]:
        c = d["stats"].get("competency")
        lines.append(f"- {d['slug']} ({d['status']}{'' if c is None else f', {c:.1f}'}): "
                     f"{d['description'][:70]}")
    more = f"\n(+{len(docs) - limit} more: list_maneuvers)" if len(docs) > limit else ""
    return "MANEUVERS YOU KNOW\n" + "\n".join(lines) + more + "\n"


def _author() -> str:
    try:
        from . import turn
        return f"vector/{turn.channel}"
    except Exception:
        return "vector"


def _human_err(e: Exception) -> str:
    return f"error: {e}"


# ======================================================================= tools
Steps = Annotated[Union[list[dict[str, Any]], str], Field(description=(
    'List of steps, e.g. [{"step":"turn","deg":20},{"step":"drive","mm":50,"speed":60}]. '
    'Steps: drive{mm,speed,fast,stop_when{prox_below_mm,touch,cube_moved}}, turn{deg}, '
    'wheels{left,right,ms}, head{deg}, lift{height 0..1}, wait{ms}, maneuver{name,args}, '
    'repeat{times,steps}, if{sensor{prox_below_mm|touch|cube_visible},then,else}.'))]


@tool
async def move(steps: Steps) -> str:
    """Move your body now with a list of steps (one smooth run, measured). Units: drive mm
    (+forward/-back, max 300 per step, speed 10-120 mm/s), turn deg (+left/-right), head
    deg (-22..45), lift 0..1, wheels left/right mm/s for ms (max 3000), wait ms. A drive can
    stop early: {"step":"drive","mm":200,"stop_when":{"prox_below_mm":40}}. Load the
    'movement' skill first. You stop by yourself if picked up, at a cliff, or too far from
    where you started. Returns what you actually did (measured)."""
    try:
        r = await run_steps(steps, label="move")
    except ManeuverError as e:
        return _human_err(e)
    if "error" in r:
        return f"error: {r['error']}"
    with contextlib.suppress(Exception):
        attempts().append("(adhoc)", {}, _compact(r))
    return format_summary("moved", r)


def _compact(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("start", "end", "label")}


@tool
def sense(what: Literal["all", "body", "distance", "flags", "cube"] = "all") -> str:
    """Read your own body sensors right now (nothing moves). body = position (mm, relative
    to where you woke), heading, head, lift, wheels. distance = the forward distance
    sensor (prox_mm, how sure it is, whether the lift blocks it). flags = charger, picked
    up, held, cliff, touched. cube = cube seen/tapped/moved. all = everything."""
    return body.describe(what)


@tool
def save_maneuver(
        name: Annotated[str, Field(description="Short name by intent, e.g. 'nod', 'nudge_forward', 'dribble'")],
        description: Annotated[str, Field(description="One line: what it does and when to use it")],
        steps: Steps,
        params: Annotated[dict | None, Field(description=(
            'Optional tunable numbers used in steps as "$name": {"mm": {"default": 50, "min": 10, '
            '"max": 200}}'))] = None,
        tags: Annotated[list[str] | None, Field(description="Optional words, e.g. ['dance','fun']")] = None) -> str:
    """Save a step list as a named maneuver you can run again (new = 'draft' until one
    run goes well and you call record_attempt). Saving an existing name makes a new version.
    Steps may call other maneuvers (max depth 4, no loops) and use $params."""
    try:
        d = catalogue().save(name, description, steps, params, tags, _author())
    except ManeuverError as e:
        return _human_err(e)
    return f"saved '{d['slug']}' v{d['version']} ({d['status']}); run it with run_maneuver"


@tool
async def run_maneuver(
        name: Annotated[str, Field(description="Saved maneuver name")],
        values: Annotated[dict | None, Field(description='Optional param values, e.g. {"mm": 80}')] = None) -> str:
    """Run a saved maneuver on your body (measured result). `values` overrides its params
    (clamped to their min/max). Call record_attempt afterwards with how it went."""
    cat = catalogue()
    d = cat.get(name)
    if not d:
        return f"error: no saved maneuver {name!r}; see list_maneuvers"
    try:
        r = await run_steps(d["steps"], d["params"], values, label=d["slug"], cat=cat, self_slug=d["slug"])
    except ManeuverError as e:
        return _human_err(e)
    if "error" in r:
        return f"error: {r['error']}"
    cat.mark_run(d["slug"])
    aid = ""
    with contextlib.suppress(Exception):
        aid = attempts().append(d["slug"], dict(values or {}), _compact(r))
    return (format_summary(d["slug"], r) +
            f". Now call record_attempt('{d['slug']}', success|partial|failure, note)." + (f" [{aid}]" if aid else ""))


@tool
def list_maneuvers(query: Annotated[str, Field(description="Optional word to search names, descriptions, tags")] = "") -> str:
    """List the maneuvers you know (name, tested/draft, how reliable 0-1, what it does)."""
    q = (query or "").strip().lower()
    docs = [d for d in catalogue().all()
            if not q or q in d["slug"] or q in d["description"].lower() or q in " ".join(d.get("tags", []))]
    if not docs:
        return "no maneuvers" + (f" matching {query!r}" if q else " saved yet")
    out = []
    for d in docs[:30]:
        c = d["stats"].get("competency")
        ps = ",".join(d["params"]) or "-"
        out.append(f"{d['slug']} [{d['status']}{'' if c is None else f' {c:.1f}'}] params={ps}: {d['description']}")
    return "\n".join(out)


@tool
def show_maneuver(name: str) -> str:
    """Show a saved maneuver's steps, params, stats and recent attempts."""
    cat = catalogue()
    d = cat.get(name)
    if not d:
        return f"error: no saved maneuver {name!r}"
    st = d["stats"]
    rec = [f"{r['time'][5:16]} {r['outcome'] or '?'} {r.get('note') or ''}".strip()
           for r in attempts().recent(d["slug"], 3)]
    return json.dumps({"name": d["slug"], "description": d["description"], "status": d["status"],
                       "version": d["version"], "params": d["params"], "steps": d["steps"],
                       "stats": st, "recent": rec}, separators=(",", ":"))


@tool
def delete_maneuver(name: str) -> str:
    """Delete a saved maneuver (other maneuvers that call it will stop working)."""
    cat = catalogue()
    users = [d["slug"] for d in cat.all() if json.dumps(d["steps"]).find(json.dumps(slugify(name))) >= 0
             and d["slug"] != slugify(name)]
    ok = cat.delete(name)
    if not ok:
        return f"error: no saved maneuver {name!r}"
    return f"deleted {name!r}" + (f"; warning: used by {', '.join(users)}" if users else "")


@tool
def record_attempt(
        name: Annotated[str, Field(description="The maneuver you just ran")],
        outcome: Literal["success", "partial", "failure"],
        note: Annotated[str, Field(description="What happened / what to change, one line")] = "") -> str:
    """Record how the last run of a maneuver went (judge from the measured result and what
    you saw). Updates its reliability score; the first success marks a draft as 'tested'."""
    try:
        d = catalogue().record_outcome(name, outcome)
    except ManeuverError as e:
        return _human_err(e)
    with contextlib.suppress(Exception):
        attempts().fill(d["slug"], outcome, note)
    c = d["stats"]["competency"]
    return f"recorded {outcome} for '{d['slug']}': reliability {c:.2f}, status {d['status']}"


_MEASURES = ("displacement_mm", "heading_deg", "prox_after_mm", "cube_moved")


def _measure(r: dict, measure: str) -> float | None:
    if measure == "displacement_mm":
        return float(r["disp_mm"])
    if measure == "heading_deg":
        return float(r["heading_deg"])
    if measure == "prox_after_mm":
        return None if r["prox_after"] is None else float(r["prox_after"])
    return 1.0 if r.get("cube_moved") else 0.0


def _score(val: float | None, target: str | float) -> float:
    if val is None:
        return -1e9
    if target == "max":
        return val
    if target == "min":
        return -val
    return -abs(val - float(target))


@tool
async def tune(
        name: Annotated[str, Field(description="Saved maneuver to tune")],
        param: Annotated[str, Field(description="Which numeric param to search")],
        low: float, high: float,
        trials: Annotated[int, Field(description="How many real runs (3-20)")] = 8,
        measure: Literal["displacement_mm", "heading_deg", "prox_after_mm", "cube_moved"] = "displacement_mm",
        target: Annotated[str, Field(description="A number to reach, or 'max' / 'min'")] = "max",
        fixed: Annotated[dict | None, Field(description="Fixed values for the other params")] = None) -> str:
    """Find the best value of ONE param by really running the maneuver several times (a
    search, no thinking per run; it drives back to the start after each run). Example: make
    'nudge' move exactly 100 mm: tune('nudge','mm',20,200,8,'displacement_mm','100').
    Saves the best value as the param's default. Keep the range small and safe."""
    cat = catalogue()
    d = cat.get(name)
    if not d:
        return f"error: no saved maneuver {name!r}"
    if param not in d["params"]:
        return f"error: {d['slug']} has no param {param!r} (has: {', '.join(d['params']) or 'none'})"
    try:
        n = max(3, min(20, int(trials)))
        t_s = str(target).strip().lower()
        tgt: str | float = t_s if t_s in ("max", "min") else float(t_s)
        lo, hi = sorted((float(low), float(high)))
    except (TypeError, ValueError):
        return "error: low/high must be numbers and target a number or max/min"
    if hi - lo < 1e-6:
        return "error: low and high must differ"
    p = d["params"][param]
    if p.get("min") is not None:
        lo = max(lo, p["min"])
    if p.get("max") is not None:
        hi = min(hi, p["max"])
    if hi - lo < 1e-6:
        return f"error: range is outside the param's allowed {p.get('min')}..{p.get('max')}"
    return await _tune_run(cat, d, param, lo, hi, n, measure, tgt, fixed or {})


async def _tune_run(cat: Catalogue, d: dict, param: str, lo: float, hi: float, n: int,
                    measure: str, tgt: str | float, args: dict) -> str:
    rows: list[tuple[float, float | None, float]] = []   # (value, measured, score)
    failure: str | None = None
    home: dict = {}     # where the first trial started: every trial returns here (no drift)

    async def trial(v: float) -> float:
        nonlocal failure
        if failure:
            return -1e9
        v = round(v, 1)
        r = await run_steps(d["steps"], d["params"], {**args, param: v}, label=f"tune:{d['slug']}",
                            cat=cat, self_slug=d["slug"])
        if "error" in r or r.get("aborted"):
            failure = r.get("error") or r["stop_reason"]
            return -1e9
        m = _measure(r, measure)
        sc = _score(m, tgt)
        rows.append((v, m, sc))
        with contextlib.suppress(Exception):
            attempts().append(d["slug"], {**args, param: v}, _compact(r) | {"tune": True})
        home.setdefault("pose", r["start"])
        back = await return_home(home["pose"], r["end"])
        if back.get("aborted") or back.get("error"):
            failure = "couldn't drive back to the start: " + str(back.get("error") or back["stop_reason"])
        return sc

    phi = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, dd = b - phi * (b - a), a + phi * (b - a)
    fc = await trial(c)
    fd = await trial(dd) if not failure else -1e9
    used = 2
    while used < n and not failure:
        if fc > fd:
            b, dd, fd = dd, c, fc
            c = b - phi * (b - a)
            fc = await trial(c)
        else:
            a, c, fc = c, dd, fd
            dd = a + phi * (b - a)
            fd = await trial(dd)
        used += 1
    table = " | ".join(f"{v:g}->{'?' if m is None else f'{m:.0f}'}" for v, m, _ in rows)
    if not rows:
        return f"error: tune stopped before any trial finished ({failure})"
    best = max(rows, key=lambda t: t[2])
    if failure:
        return (f"tune stopped after {len(rows)} trial(s): {failure}. Nothing saved. "
                f"{param}->{measure}: {table}")
    cat.set_param_default(d["slug"], param, best[0])
    goal = tgt if isinstance(tgt, str) else f"target {tgt:g}"
    return (f"tuned '{d['slug']}' {param}: best {best[0]:g} ({measure} {'?' if best[1] is None else f'{best[1]:.0f}'}, "
            f"{goal}), saved as default. {len(rows)} trials, {param}->{measure}: {table}")


MANEUVER_TOOLS = [move, sense, save_maneuver, run_maneuver, list_maneuvers, show_maneuver,
                  delete_maneuver, record_attempt, tune]
MANEUVER_CONTROL_TOOLS = {"move", "run_maneuver", "tune"}
