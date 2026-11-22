"""Body engine and safety governor: measured, bounded physical motion for the agent.

Layers
* StateFeed   : thread-safe snapshot of robot_state + events, fed from the shared listen-only
                SDK connection (app/faces.py). Registered with `register()` before faces.start().
* Transports  : how motion commands reach the robot. `GrpcTransport` (raw gRPC on that same
                SDK connection, control via the connection's own BehaviorControl stream) and
                `HttpTransport` (wire-pod /api-sdk, closed loop against the state feed).
                Choose with BODY_TRANSPORT=grpc|http (default grpc).
* BodyEngine  : one motion at a time (asyncio.Lock). `async with ENGINE.batch(label) as b:`
                acquires control ONCE, runs the governor over every primitive, and on every exit
                path (done, abort, exception, cancellation) sends stop and releases control.

The governor is not model-controllable: speed/step/time/leash caps, and aborts on cliff,
picked up, held, falling, unexpected movement, tilt, origin change (moved/relocalised), stale
feed, disconnected SDK, low battery. On the charger wheel moves are refused.
Abort reasons are plain words ("stopped: picked up").

The anki_vector SDK is imported lazily so this module imports (and is testable) without it.
"""
import asyncio
import contextlib
import itertools
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("vector-agent")


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# ------------------------------------------------------------------ settings (move into config.py)
BODY_TRANSPORT = os.environ.get("BODY_TRANSPORT", "grpc").strip().lower()   # grpc | http
BODY_LEASH_MM = _f("BODY_LEASH_MM", 400)           # max distance from the batch's start pose
BODY_MAX_SPEED_MMPS = _f("BODY_MAX_SPEED_MMPS", 120)
BODY_FAST_SPEED_MMPS = _f("BODY_FAST_SPEED_MMPS", 220)
BODY_MAX_DRIVE_MM = _f("BODY_MAX_DRIVE_MM", 300)   # per drive step
BODY_MAX_WHEELS_MS = _f("BODY_MAX_WHEELS_MS", 3000)  # per timed-wheels step
BODY_MAX_MANEUVER_S = _f("BODY_MAX_MANEUVER_S", 60)  # per batch
BODY_STALE_S = _f("BODY_STALE_S", 1.0)             # state feed older than this = abort
BODY_MAX_PITCH_DEG = _f("BODY_MAX_PITCH_DEG", 30)  # |pose pitch| beyond this = abort
BODY_MAX_TILT_DEG = _f("BODY_MAX_TILT_DEG", 40)    # tilt from accelerometer (static only)
BODY_PROX_MIN_QUALITY = _f("BODY_PROX_MIN_QUALITY", 0.03)
BODY_LOCK_WAIT_S = _f("BODY_LOCK_WAIT_S", 75)      # how long a 2nd motion tool waits its turn
BODY_TRACK_MM = _f("BODY_TRACK_MM", 48)            # wheel track (dry-run kinematics, http turns)

POLL_S = 0.04          # governor poll period while a motion runs
SETTLE_S = 0.25        # wait (at most) this long for a fresh state sample after a motion
ACTION_TIMEOUT_S = _f("BODY_ACTION_TIMEOUT_S", 35)
BORROW_PROBE_S = _f("BODY_BORROW_PROBE_S", 2.0)
STRAY_WHEEL_MMPS = _f("BODY_STRAY_WHEEL_MMPS", 25)  # wheels this fast during a head/lift/wait step = abort    # no-op action must return this fast under control  # one gRPC action (a 300 mm crawl fits)
TURN_SPEED_DPS = 100.0   # assumed turn rate (deg/s) for dry-run/timeouts
HEAD_MIN_DEG, HEAD_MAX_DEG = -22.0, 45.0
LIFT_MIN_MM, LIFT_MAX_MM = 32.0, 92.0

# RobotState.status bits (RobotStatus in messages.proto)
ST_MOVING = 0x1
ST_PICKED_UP = 0x8
ST_FALLING = 0x20
ST_ANIMATING = 0x40
ST_PATHING = 0x80
ST_ON_CHARGER = 0x1000
ST_CHARGING = 0x2000
ST_CLIFF = 0x4000
ST_WHEELS = 0x8000
ST_HELD = 0x10000
_FLAG_NAMES = {ST_MOVING: "moving", ST_PICKED_UP: "picked_up", ST_FALLING: "falling",
               ST_ANIMATING: "animating", ST_ON_CHARGER: "on_charger", ST_CHARGING: "charging",
               ST_CLIFF: "cliff", ST_WHEELS: "wheels_moving", ST_HELD: "held"}

OBJECT_TYPE_LIGHTCUBE = 2   # ObjectType.BLOCK_LIGHTCUBE1 in cube.proto
CUBE_VISIBLE_S = 2.0


def _now() -> float:
    return time.time()


def wrap180(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


class BodyError(Exception):
    """The body refused or could not start the motion (message is plain words)."""


# =========================================================================== state feed
class StateFeed:
    """Latest robot_state + event counters. Written from the SDK thread, read anywhere."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._s: dict = {
                "ts": 0.0, "x": 0.0, "y": 0.0, "heading_deg": 0.0, "origin_id": 0,
                "pitch_deg": 0.0, "tilt_deg": None, "head_deg": 0.0, "lift_mm": LIFT_MIN_MM,
                "wheel_l": 0.0, "wheel_r": 0.0, "accel": (0.0, 0.0, 0.0), "gyro": (0.0, 0.0, 0.0),
                "prox_mm": 0, "prox_quality": 0.0, "prox_found": False, "prox_lift": False,
                "touched": False, "touch_raw": 0, "status": 0,
                "unexpected_seq": 0, "unexpected_at": 0.0,
                "cube_tapped_seq": 0, "cube_moved_seq": 0, "cube_axis": "", "cube_seen_at": 0.0,
            }
            self._unwrapped = 0.0       # continuous heading (deg), survives +-180 wrap
            self._last_heading: float | None = None

    # -- writers
    def update(self, *, x=0.0, y=0.0, heading_deg=0.0, origin_id=0, pitch_deg=0.0,
               head_deg=0.0, lift_mm=LIFT_MIN_MM, wheel_l=0.0, wheel_r=0.0,
               accel=(0.0, 0.0, 0.0), gyro=(0.0, 0.0, 0.0), prox_mm=0, prox_quality=0.0,
               prox_found=False, prox_lift=False, touched=False, touch_raw=0, status=0,
               ts: float | None = None) -> None:
        ax, ay, az = accel
        mag = math.sqrt(ax * ax + ay * ay + az * az)
        tilt = None
        if 7000 <= mag <= 13000:        # ~1 g: nearly static, so the accel vector is gravity
            tilt = math.degrees(math.atan2(math.sqrt(ax * ax + ay * ay), abs(az)))
        with self._lock:
            if self._last_heading is not None:
                self._unwrapped += wrap180(heading_deg - self._last_heading)
            self._last_heading = heading_deg
            self._s.update(
                ts=_now() if ts is None else ts, x=x, y=y, heading_deg=heading_deg,
                origin_id=origin_id, pitch_deg=pitch_deg, tilt_deg=tilt, head_deg=head_deg,
                lift_mm=lift_mm, wheel_l=wheel_l, wheel_r=wheel_r, accel=tuple(accel),
                gyro=tuple(gyro), prox_mm=prox_mm, prox_quality=prox_quality,
                prox_found=prox_found, prox_lift=prox_lift, touched=touched,
                touch_raw=touch_raw, status=int(status))

    def on_unexpected_movement(self) -> None:
        with self._lock:
            self._s["unexpected_seq"] += 1
            self._s["unexpected_at"] = _now()

    def on_cube(self, kind: str, axis: str = "") -> None:
        with self._lock:
            if kind == "tapped":
                self._s["cube_tapped_seq"] += 1
            elif kind == "moved":
                self._s["cube_moved_seq"] += 1
            elif kind == "axis":
                self._s["cube_axis"] = axis
            elif kind == "seen":
                self._s["cube_seen_at"] = _now()

    # -- readers
    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._s)
            s["heading_unwrapped"] = self._unwrapped
        s["age_s"] = (_now() - s["ts"]) if s["ts"] else None
        s["cube_visible"] = bool(s["cube_seen_at"]) and _now() - s["cube_seen_at"] <= CUBE_VISIBLE_S
        st = s["status"]
        s["flags"] = sorted(n for bit, n in _FLAG_NAMES.items() if st & bit)
        s["connected"] = is_connected()
        return s


FEED = StateFeed()


def _sdk_connected() -> bool:
    try:
        from . import faces
        return faces.get_robot() is not None
    except Exception:
        return False


is_connected = _sdk_connected    # tests override


def _on_state(_robot, _type, msg, *_a, **_k) -> None:        # SDK thread
    p = msg.pose
    pd = msg.prox_data
    td = msg.touch_data
    FEED.update(x=float(p.x), y=float(p.y), heading_deg=math.degrees(msg.pose_angle_rad),
                origin_id=int(p.origin_id), pitch_deg=math.degrees(msg.pose_pitch_rad),
                head_deg=math.degrees(msg.head_angle_rad), lift_mm=float(msg.lift_height_mm),
                wheel_l=float(msg.left_wheel_speed_mmps), wheel_r=float(msg.right_wheel_speed_mmps),
                accel=(msg.accel.x, msg.accel.y, msg.accel.z), gyro=(msg.gyro.x, msg.gyro.y, msg.gyro.z),
                prox_mm=int(pd.distance_mm), prox_quality=float(pd.signal_quality),
                prox_found=bool(pd.found_object), prox_lift=bool(pd.is_lift_in_fov),
                touched=bool(td.is_being_touched), touch_raw=int(td.raw_touch_value),
                status=int(msg.status))


def _on_unexpected(_robot, _type, _msg, *_a, **_k) -> None:
    FEED.on_unexpected_movement()


def _on_tapped(_robot, _type, _msg, *_a, **_k) -> None:
    FEED.on_cube("tapped")


def _on_moved(_robot, _type, _msg, *_a, **_k) -> None:
    FEED.on_cube("moved")


def _on_axis(_robot, _type, msg, *_a, **_k) -> None:
    try:
        from_enum = {1: "x-", 2: "x+", 3: "y-", 4: "y+", 5: "z-", 6: "z+"}
        FEED.on_cube("axis", from_enum.get(int(msg.up_axis), str(int(msg.up_axis))))
    except Exception:
        FEED.on_cube("axis", "?")


def _on_observed(_robot, _type, msg, *_a, **_k) -> None:
    try:
        if int(msg.object_type) == OBJECT_TYPE_LIGHTCUBE:
            FEED.on_cube("seen")
    except Exception:
        pass


def subscribe(r) -> None:
    """faces.register_subscriber hook: runs in the SDK thread on every (re)connect."""
    ev = r.events
    for name, fn in (("robot_state", _on_state), ("unexpected_movement", _on_unexpected),
                     ("object_tapped", _on_tapped), ("object_moved", _on_moved),
                     ("object_up_axis_changed", _on_axis), ("robot_observed_object", _on_observed)):
        ev.subscribe_by_name(fn, name)
    log.info("body: state feed subscribed")


def register() -> None:
    """Call once in the server lifespan BEFORE faces.start()."""
    from . import faces
    faces.register_subscriber(subscribe)


def snapshot() -> dict:
    return FEED.snapshot()


def prox_close(s: dict, below_mm: float) -> bool:
    return (s["prox_found"] and not s["prox_lift"] and 0 < s["prox_mm"] < below_mm
            and s["prox_quality"] >= BODY_PROX_MIN_QUALITY)


def describe(what: str = "all") -> str:
    """Compact key=value text for the LLM. what: body | distance | flags | cube | all."""
    s = snapshot()
    if s["ts"] == 0.0 or s["age_s"] is None:
        return "no sensor data yet (body feed not connected)"
    yn = lambda b: "yes" if b else "no"  # noqa: E731
    parts = {
        "body": (f"x={s['x']:.0f}mm y={s['y']:.0f}mm heading={s['heading_deg']:.0f}deg "
                 f"head={s['head_deg']:.0f}deg lift={s['lift_mm']:.0f}mm "
                 f"wheels={s['wheel_l']:.0f}/{s['wheel_r']:.0f}mmps "
                 f"pitch={s['pitch_deg']:.0f}deg" +
                 "" +
                 f" map_id={s['origin_id']}"),
        "distance": (f"prox_mm={s['prox_mm']} quality={s['prox_quality']:.2f} "
                     f"object_ahead={yn(s['prox_found'])} lift_blocks_sensor={yn(s['prox_lift'])}"),
        "flags": (f"on_charger={yn(s['status'] & ST_ON_CHARGER)} picked_up={yn(s['status'] & ST_PICKED_UP)} "
                  f"held={yn(s['status'] & ST_HELD)} cliff={yn(s['status'] & ST_CLIFF)} "
                  f"falling={yn(s['status'] & ST_FALLING)} moving={yn(s['status'] & (ST_MOVING | ST_WHEELS))} "
                  f"touched={yn(s['touched'])} unexpected_moves={s['unexpected_seq']}"),
        "cube": (f"cube_visible={yn(s['cube_visible'])} cube_tapped={s['cube_tapped_seq']} "
                 f"cube_moved={s['cube_moved_seq']} cube_up_axis={s['cube_axis'] or 'unknown'}"),
    }
    what = (what or "all").strip().lower()
    out = [parts[what]] if what in parts else [parts[k] for k in ("body", "distance", "flags", "cube")]
    if s["age_s"] > BODY_STALE_S:
        out.append(f"WARNING: sensor data is {s['age_s']:.0f}s old")
    return "\n".join(out)


# ============================================================================ results
@dataclass
class MotionResult:
    kind: str
    ok: bool = True
    disp_mm: float = 0.0          # net straight-line displacement, signed along start heading
    heading_deg: float = 0.0      # net heading change
    prox_before: int | None = None
    prox_after: int | None = None
    duration_s: float = 0.0
    code: str = ""                # action result code / "timed" / "refused"
    stop_reason: str | None = None   # None = ran to completion
    aborted: bool = False         # True = governor/hazard stop (batch is dead after this)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("kind", "ok", "code", "stop_reason", "aborted")}
        d.update(disp_mm=round(self.disp_mm, 1), heading_deg=round(self.heading_deg, 1),
                 prox_before=self.prox_before, prox_after=self.prox_after,
                 duration_s=round(self.duration_s, 2), notes=list(self.notes))
        return d

    def line(self) -> str:
        bits = [self.kind]
        if self.disp_mm:
            bits.append(f"moved {self.disp_mm:.0f}mm")
        if self.heading_deg:
            bits.append(f"turned {self.heading_deg:.0f}deg")
        if self.prox_before is not None and self.prox_after is not None and self.prox_before != self.prox_after:
            bits.append(f"prox {self.prox_before}->{self.prox_after}mm")
        bits.append(f"{self.duration_s:.1f}s")
        if self.stop_reason:
            bits.append(self.stop_reason)
        return ", ".join(bits)


def _prox_val(s: dict) -> int | None:
    return int(s["prox_mm"]) if s["prox_found"] and not s["prox_lift"] else None


def _travel(a: dict, b: dict) -> float:
    """Signed straight-line distance a->b: positive if it lies ahead of a's heading."""
    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
    d = math.hypot(dx, dy)
    h = math.radians(a["heading_deg"])
    return d if (dx * math.cos(h) + dy * math.sin(h)) >= 0 else -d


# ========================================================================== transports
class Transport:
    name = "base"

    async def acquire(self) -> None: ...
    async def release(self) -> None: ...
    async def drive(self, mm: float, speed: float) -> str: ...
    async def turn(self, deg: float) -> str: ...
    async def head(self, deg: float) -> str: ...
    async def lift(self, mm: float) -> str: ...
    async def wheels(self, left: float, right: float) -> None: ...
    async def stop(self) -> None: ...
    def connected(self) -> bool:
        return is_connected()


_tag_counter = itertools.count(0)


def _next_tag() -> int:
    from anki_vector.messaging import protocol
    span = protocol.LAST_SDK_TAG - protocol.FIRST_SDK_TAG
    # upper half of the SDK tag range, so we never collide with the SDK's own behaviour ids
    return protocol.FIRST_SDK_TAG + span // 2 + (next(_tag_counter) % (span // 2 - 1))


class GrpcTransport(Transport):
    """Raw gRPC on the shared listen-only SDK connection (bypasses requires_control; we hold
    control ourselves). Control uses the connection's own BehaviorControl stream: it is
    always opened, even with behavior_control_level=None, so request_control() works later."""
    name = "grpc"

    def __init__(self, get_robot=None, borrow: bool = False) -> None:
        self._get_robot = get_robot
        self._have_control = False
        # borrow: wire-pod already holds behaviour control (voice turn). Verified on the robot
        # (2026-11-22): actions from our connection are then accepted WITHOUT requesting
        # control, precisely; requesting it would steal it and cut his speech.
        self.borrow = borrow

    def _robot(self):
        if self._get_robot:
            r = self._get_robot()
        else:
            from . import faces
            r = faces.get_robot()
        if r is None:
            raise BodyError("my body connection is down")
        return r

    async def _run(self, make_coro):
        r = self._robot()
        return await asyncio.wrap_future(r.conn.run_coroutine(make_coro(r)))

    async def acquire(self) -> None:
        if self.borrow:
            self._robot()          # still fail fast if the connection is down
            if await self._probe_borrow():
                return
            # Nobody actually holds control (wire-pod finished talking, or no voice turn at
            # all): actions would hang while his own behaviours drive him around. Take it.
            log.info("body: wire-pod isn't holding control after all; taking it")
            self.borrow = False
        from anki_vector.connection import ControlPriorityLevel
        r = self._robot()
        try:
            await asyncio.wrap_future(r.conn.request_control(ControlPriorityLevel.DEFAULT_PRIORITY, timeout=8.0))
        except Exception as e:
            raise BodyError(f"couldn't get control of my body ({type(e).__name__})") from e
        self._have_control = True

    async def _probe_borrow(self) -> bool:
        """Is someone (wire-pod) really holding control? A no-op head move (to where the head
        already is) returns at once under control and hangs without it."""
        from anki_vector.messaging import protocol
        tag = _next_tag()
        head = FEED.snapshot().get("head_deg") or 0.0
        req = protocol.SetHeadAngleRequest(angle_rad=math.radians(head), max_speed_rad_per_sec=10.0,
                                           accel_rad_per_sec2=10.0, duration_sec=0.0, id_tag=tag,
                                           num_retries=0)
        try:
            await asyncio.wait_for(self._run(lambda r: r.conn.grpc_interface.SetHeadAngle(req)),
                                   BORROW_PROBE_S)
            return True
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._cancel_tag(tag))
            return False

    async def release(self) -> None:
        if not self._have_control:
            return
        self._have_control = False
        try:
            r = self._robot()
            # The SDK waits for a "control lost" event that this robot never sends after a
            # release (it times out every time), though the release itself works: send it
            # and don't wait.
            fut = r.conn.release_control(timeout=4.0)
            fut.add_done_callback(lambda f: f.exception())   # swallow the timeout
        except Exception as e:
            log.warning("body: release_control failed: %s", e)

    @staticmethod
    def _code(resp) -> str:
        from anki_vector.messaging import protocol
        try:
            return protocol.ActionResult.ActionResultCode.Name(resp.result.code)
        except Exception:
            return "UNKNOWN"

    async def _action(self, rpc: str, req_name: str, **fields) -> str:
        from anki_vector.messaging import protocol
        tag = _next_tag()
        req = getattr(protocol, req_name)(id_tag=tag, num_retries=0, **fields)
        try:
            # an action sent while nobody holds control waits forever: never hang a turn
            resp = await asyncio.wait_for(
                self._run(lambda r: getattr(r.conn.grpc_interface, rpc)(req)), ACTION_TIMEOUT_S)
        except (asyncio.CancelledError, asyncio.TimeoutError) as e:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._cancel_tag(tag))
            if isinstance(e, asyncio.TimeoutError):
                raise BodyError("stopped: my body didn't respond (no control?)") from e
            raise
        return self._code(resp)

    async def _cancel_tag(self, tag: int) -> None:
        from anki_vector.messaging import protocol
        await self._run(lambda r: r.conn.grpc_interface.CancelActionByIdTag(
            protocol.CancelActionByIdTagRequest(id_tag=tag)))

    async def drive(self, mm: float, speed: float) -> str:
        return await self._action("DriveStraight", "DriveStraightRequest",
                                  speed_mmps=abs(speed), dist_mm=mm, should_play_animation=False)

    async def turn(self, deg: float) -> str:
        return await self._action("TurnInPlace", "TurnInPlaceRequest", angle_rad=math.radians(deg),
                                  speed_rad_per_sec=math.radians(TURN_SPEED_DPS),
                                  accel_rad_per_sec2=math.radians(400), tol_rad=0.0, is_absolute=0)

    async def head(self, deg: float) -> str:
        return await self._action("SetHeadAngle", "SetHeadAngleRequest", angle_rad=math.radians(deg),
                                  max_speed_rad_per_sec=10.0, accel_rad_per_sec2=10.0, duration_sec=0.0)

    async def lift(self, mm: float) -> str:
        return await self._action("SetLiftHeight", "SetLiftHeightRequest", height_mm=mm,
                                  max_speed_rad_per_sec=10.0, accel_rad_per_sec2=10.0, duration_sec=0.0)

    async def wheels(self, left: float, right: float) -> None:
        from anki_vector.messaging import protocol
        await self._run(lambda r: r.conn.grpc_interface.DriveWheels(protocol.DriveWheelsRequest(
            left_wheel_mmps=left, right_wheel_mmps=right, left_wheel_mmps2=0.0, right_wheel_mmps2=0.0)))

    async def stop(self) -> None:
        from anki_vector.messaging import protocol
        await asyncio.wait_for(self._run(lambda r: r.conn.grpc_interface.StopAllMotors(
            protocol.StopAllMotorsRequest())), 3.0)


class HttpTransport(Transport):
    """wire-pod /api-sdk fallback. Only integer speeds exist there, so every motion is a
    closed loop against the state feed: start the motor, poll the pose, stop at the target."""
    name = "http"
    LAG_S = 0.12           # command + feed latency: stop this much early
    HEAD_RAD_S = 2         # integer speed units of move_head / move_lift (rad/s, assumed)

    def __init__(self) -> None:
        self._assumed = False

    async def _sdk(self, endpoint: str, **kw) -> str:
        from . import robot
        return await robot.sdk(endpoint, **kw)

    async def acquire(self) -> None:
        from . import turn as turn_state
        if turn_state.wirepod_has_control:
            return                       # asking again would steal control and cut his speech
        try:
            await self._sdk("assume_behavior_control", priority="default")
        except Exception as e:
            raise BodyError(f"couldn't get control of my body ({e})") from e
        self._assumed = True
        await asyncio.sleep(0.4)         # let the grant land

    async def release(self) -> None:
        if not self._assumed:
            return
        self._assumed = False
        with contextlib.suppress(Exception):
            await self._sdk("release_behavior_control")

    async def _loop(self, done, limit_s: float) -> str:
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit_s:
            if done(FEED.snapshot()):
                return "ACTION_RESULT_SUCCESS"
            await asyncio.sleep(0.03)
        return "TIMEOUT"

    async def drive(self, mm: float, speed: float) -> str:
        a = FEED.snapshot()
        v = int(round(abs(speed))) * (1 if mm >= 0 else -1)
        lead = abs(v) * self.LAG_S
        try:
            await self._sdk("move_wheels", lw=v, rw=v)
            return await self._loop(lambda s: abs(_travel(a, s)) >= abs(mm) - lead,
                                    abs(mm) / max(abs(v), 1) + 3.0)
        finally:
            with contextlib.suppress(Exception):
                await self._sdk("move_wheels", lw=0, rw=0)

    async def turn(self, deg: float) -> str:
        a = FEED.snapshot()
        sign = 1 if deg >= 0 else -1
        cur = 0
        try:
            while True:
                s = FEED.snapshot()
                left = abs(deg) - sign * (s["heading_unwrapped"] - a["heading_unwrapped"])
                v = 40 if left > 25 else 20          # mm/s per wheel; rate = 2v/track
                if left <= 2.0 or left <= math.degrees(2 * v / BODY_TRACK_MM) * self.LAG_S:
                    return "ACTION_RESULT_SUCCESS"
                if v != cur:
                    cur = v
                    await self._sdk("move_wheels", lw=-sign * v, rw=sign * v)
                await asyncio.sleep(0.03)
        finally:
            with contextlib.suppress(Exception):
                await self._sdk("move_wheels", lw=0, rw=0)

    async def _servo(self, endpoint: str, key: str, target: float, tol: float) -> str:
        try:
            s = FEED.snapshot()
            err = target - s[key]
            if abs(err) <= tol:
                return "ACTION_RESULT_SUCCESS"
            sign = 1 if err > 0 else -1
            await self._sdk(endpoint, speed=sign * self.HEAD_RAD_S)
            return await self._loop(lambda s: sign * (target - s[key]) <= tol, 4.0)
        finally:
            with contextlib.suppress(Exception):
                await self._sdk(endpoint, speed=0)

    async def head(self, deg: float) -> str:
        return await self._servo("move_head", "head_deg", deg, 3.0)

    async def lift(self, mm: float) -> str:
        return await self._servo("move_lift", "lift_mm", mm, 3.0)

    async def wheels(self, left: float, right: float) -> None:
        await self._sdk("move_wheels", lw=int(round(left)), rw=int(round(right)))

    async def stop(self) -> None:
        for ep, kw in (("move_wheels", {"lw": 0, "rw": 0}), ("move_head", {"speed": 0}),
                       ("move_lift", {"speed": 0})):
            with contextlib.suppress(Exception):
                await self._sdk(ep, **kw)


def make_transport(name: str | None = None) -> Transport:
    return HttpTransport() if (name or BODY_TRANSPORT) == "http" else GrpcTransport()


async def _battery_low() -> bool:
    """True only when wire-pod positively says the battery is low (level 1)."""
    try:
        from . import robot
        d = json.loads(await robot.sdk("get_battery"))
        return d.get("battery_level") == 1
    except Exception:
        return False


# ============================================================================== engine
class BodyEngine:
    def __init__(self, transport_factory=None, battery_check=None) -> None:
        self._factory = transport_factory or make_transport
        self._battery_check = battery_check or _battery_low
        self._lock: asyncio.Lock | None = None
        self._lock_loop = None
        self.busy_with: str | None = None

    def lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def transport_for_batch(self) -> Transport:
        from . import turn as turn_state
        t = self._factory()
        # While wire-pod holds control (voice turn), act under its control without asking
        # (asking would steal it and cut his speech).
        if t.name == "grpc":
            t.borrow = bool(turn_state.wirepod_has_control)
        return t

    def batch(self, label: str = "motion", *, max_s: float | None = None,
              leash_mm: float | None = None) -> "Batch":
        return Batch(self, label, max_s or BODY_MAX_MANEUVER_S, leash_mm or BODY_LEASH_MM)


ENGINE = BodyEngine()


class Batch:
    """One serialised run of motions under one control acquisition and one governor."""

    def __init__(self, engine: BodyEngine, label: str, max_s: float, leash_mm: float) -> None:
        self.engine, self.label = engine, label
        self.max_s, self.leash_mm = max_s, leash_mm
        self.transport: Transport | None = None
        self.aborted: str | None = None
        self.results: list[MotionResult] = []
        self.start: dict = {}
        self.end: dict = {}
        self.t0 = 0.0
        self.deadline = 0.0
        self._locked = False
        self._battery_checked = False

    # ---------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "Batch":
        lk = self.engine.lock()
        try:
            await asyncio.wait_for(lk.acquire(), BODY_LOCK_WAIT_S)
        except asyncio.TimeoutError:
            raise BodyError(f"I'm still busy with {self.engine.busy_with or 'another movement'}") from None
        self._locked = True
        self.engine.busy_with = self.label
        try:
            self.transport = self.engine.transport_for_batch()
            if not self.transport.connected():
                raise BodyError("my body connection is down")
            s = FEED.snapshot()
            if s["age_s"] is None or s["age_s"] > BODY_STALE_S:
                raise BodyError("my sensors aren't updating right now (reconnecting; try again "
                                "in a few seconds, don't retry straight away)")
            hazard = self._hazard(s, preflight=True)
            if hazard:
                raise BodyError(f"stopped: {hazard}")
            await self.transport.acquire()
            self.start = FEED.snapshot()
            self.t0 = time.monotonic()
            self.deadline = self.t0 + self.max_s
        except BaseException:
            await self._cleanup()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self._cleanup()
        return False

    async def _cleanup(self) -> None:
        """Every exit path: stop the motors, release control, free the lock."""
        t = self.transport
        try:
            if t is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(asyncio.wait_for(t.stop(), 4.0))
                with contextlib.suppress(BaseException):
                    await asyncio.shield(asyncio.wait_for(t.release(), 8.0))
            self.end = FEED.snapshot()
        finally:
            if self._locked:
                self._locked = False
                self.engine.busy_with = None
                self.engine.lock().release()

    # ----------------------------------------------------------------- governor
    def _hazard(self, s: dict, preflight: bool = False) -> str | None:
        """Plain-words reason the body must stop, or None."""
        st = s["status"]
        if st & ST_PICKED_UP:
            return "picked up"
        if st & ST_HELD:
            return "being held"
        if st & ST_FALLING:
            return "falling"
        if st & ST_CLIFF:
            return "cliff detected"
        if abs(s["pitch_deg"]) > BODY_MAX_PITCH_DEG:
            return "tilted too far"
        # NB: no accelerometer tilt check: Vector's IMU is in his HEAD (head angle reads as
        # tilt) and his own turning acceleration skews it; aborted harmless turns on the
        # robot. pose pitch (body, from the robot) + firmware picked-up/falling/cliff do it.
        if preflight:
            return None
        if s["age_s"] is None or s["age_s"] > BODY_STALE_S:
            return "lost my sensors"
        if not self.transport.connected():
            return "connection lost"
        if s["origin_id"] != self.start["origin_id"]:
            return "moved or relocalised"
        if s["unexpected_seq"] != self.start["unexpected_seq"]:
            return "unexpected movement"
        if time.monotonic() > self.deadline:
            return "time limit"
        if math.hypot(s["x"] - self.start["x"], s["y"] - self.start["y"]) > self.leash_mm:
            return "too far from where I started"
        return None

    def _stop_when(self, sw: dict | None, s: dict, base: dict) -> str | None:
        if not sw:
            return None
        below = sw.get("prox_below_mm")
        if below and prox_close(s, below):
            return f"stop_when: something {s['prox_mm']}mm away"
        if sw.get("touch") and s["touched"]:
            return "stop_when: touched"
        if sw.get("cube_moved") and s["cube_moved_seq"] != base["cube_moved_seq"]:
            return "stop_when: cube moved"
        return None

    async def _settle(self, since: float) -> None:
        end = time.monotonic() + SETTLE_S
        while time.monotonic() < end:
            if FEED.snapshot()["ts"] > since:
                return
            await asyncio.sleep(0.02)

    async def _guard(self, make_coro, kind: str, *, timeout_s: float, stop_when: dict | None = None,
                     sleep_s: float | None = None, check_wheels: bool = False,
                     wheels_still: bool = False) -> MotionResult:
        """Run one motion under the governor. make_coro() -> awaitable returning a result code
        (or None when `sleep_s` is used: timed wheels just wait). Polls the state feed."""
        res = MotionResult(kind=kind)
        if self.aborted:
            res.ok, res.aborted, res.stop_reason = False, True, f"stopped: {self.aborted}"
            res.code = "refused"
            return res
        a = FEED.snapshot()
        if check_wheels and a["status"] & ST_ON_CHARGER:
            self.aborted = "I'm on my charger"
            res.ok, res.aborted, res.code = False, True, "refused"
            res.stop_reason = "stopped: I'm on my charger"
            self.results.append(res)
            return res
        res.prox_before = _prox_val(a)
        t_start = time.monotonic()
        wall_end = t_start + timeout_s
        task = asyncio.ensure_future(make_coro()) if make_coro else None
        reason: str | None = None
        hard = False
        try:
            while True:
                if task is not None and task.done():
                    break
                if task is None and sleep_s is not None and time.monotonic() - t_start >= sleep_s:
                    break
                s = FEED.snapshot()
                reason = self._hazard(s)
                if reason:
                    hard = True
                    break
                if wheels_still and max(abs(s["wheel_l"]), abs(s["wheel_r"])) > STRAY_WHEEL_MMPS:
                    # this step doesn't use the wheels: something else is driving me (his own
                    # behaviours = we don't really have control)
                    reason, hard = "my wheels moved by themselves", True
                    break
                reason = self._stop_when(stop_when, s, a)
                if reason:
                    break
                if time.monotonic() > wall_end:
                    reason, hard = "timed out", True
                    break
                if task is not None:
                    await asyncio.wait({task}, timeout=POLL_S)
                else:
                    await asyncio.sleep(POLL_S)
        except BaseException:
            # cancelled / died mid-motion: the watchdog duty is to stop the motors now
            if task is not None and not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.shield(asyncio.wait_for(self.transport.stop(), 4.0))
            raise
        if reason:
            with contextlib.suppress(BaseException):
                await asyncio.shield(asyncio.wait_for(self.transport.stop(), 4.0))
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            res.stop_reason = f"stopped: {reason}" if hard else reason
            res.aborted, res.ok, res.code = hard, not hard, "stopped"
            if hard:
                self.aborted = reason
                try:
                    from . import activity
                    activity.record("body", f"{self.label}: stopped a {kind} because {reason}")
                except Exception:
                    pass
        elif task is not None:
            try:
                res.code = task.result() or ""
            except BodyError:
                raise
            except Exception as e:
                res.ok, res.code = False, type(e).__name__
                res.stop_reason = f"error: {str(e)[:120]}"
            else:
                if res.code not in ("ACTION_RESULT_SUCCESS", ""):
                    res.ok = False
                    res.stop_reason = f"action ended: {res.code.lower()}"
        else:
            res.code = "timed"
            with contextlib.suppress(BaseException):
                await asyncio.shield(asyncio.wait_for(self.transport.stop(), 4.0))
        await self._settle(a["ts"])
        b = FEED.snapshot()
        res.duration_s = time.monotonic() - t_start
        res.disp_mm = _travel(a, b)
        res.heading_deg = b["heading_unwrapped"] - a["heading_unwrapped"]
        res.prox_after = _prox_val(b)
        self.results.append(res)
        return res

    # -------------------------------------------------------------- primitives
    async def _prep_wheels(self) -> str | None:
        """First wheel move of the batch: battery check. Returns refusal reason or None."""
        if not self._battery_checked:
            self._battery_checked = True
            if await self.engine._battery_check():
                return "battery low"
        return None

    @staticmethod
    def _speed(speed: float, fast: bool) -> tuple[float, list[str]]:
        cap = BODY_FAST_SPEED_MMPS if fast else BODY_MAX_SPEED_MMPS
        sp = max(10.0, abs(float(speed)))
        notes = [f"speed capped to {cap:.0f}mm/s"] if sp > cap else []
        return min(sp, cap), notes

    async def _refused(self, kind: str, reason: str) -> MotionResult:
        self.aborted = reason
        r = MotionResult(kind=kind, ok=False, aborted=True, code="refused", stop_reason=f"stopped: {reason}")
        self.results.append(r)
        return r

    async def drive(self, mm: float, speed: float = 60, fast: bool = False,
                    stop_when: dict | None = None) -> MotionResult:
        sp, notes = self._speed(speed, fast)
        dist = float(mm)
        if abs(dist) > BODY_MAX_DRIVE_MM:
            notes.append(f"distance capped to {BODY_MAX_DRIVE_MM:.0f}mm")
            dist = math.copysign(BODY_MAX_DRIVE_MM, dist)
        if not self.aborted and (why := await self._prep_wheels()):
            return await self._refused("drive", why)
        r = await self._guard(lambda: self.transport.drive(dist, sp), "drive",
                              timeout_s=abs(dist) / sp + 6.0, stop_when=stop_when, check_wheels=True)
        r.notes += notes
        return r

    async def turn(self, deg: float) -> MotionResult:
        d = float(deg)
        notes = []
        if abs(d) > 360:
            d, notes = math.copysign(360, d), ["turn capped to 360deg"]
        if not self.aborted and (why := await self._prep_wheels()):
            return await self._refused("turn", why)
        r = await self._guard(lambda: self.transport.turn(d), "turn",
                              timeout_s=abs(d) / TURN_SPEED_DPS * 2 + 6.0, check_wheels=True)
        r.notes += notes
        return r

    async def head(self, deg: float) -> MotionResult:
        d = max(HEAD_MIN_DEG, min(HEAD_MAX_DEG, float(deg)))
        r = await self._guard(lambda: self.transport.head(d), "head", timeout_s=6.0, wheels_still=True)
        if d != float(deg):
            r.notes.append(f"head clamped to {d:.0f}deg")
        return r

    async def lift(self, height: float) -> MotionResult:
        h = max(0.0, min(1.0, float(height)))
        mm = LIFT_MIN_MM + h * (LIFT_MAX_MM - LIFT_MIN_MM)
        r = await self._guard(lambda: self.transport.lift(mm), "lift", timeout_s=6.0, wheels_still=True)
        if h != float(height):
            r.notes.append("lift clamped to 0..1")
        return r

    async def wheels(self, left: float, right: float, ms: float, fast: bool = False,
                     stop_when: dict | None = None) -> MotionResult:
        cap = BODY_FAST_SPEED_MMPS if fast else BODY_MAX_SPEED_MMPS
        l_, r_ = (max(-cap, min(cap, float(v))) for v in (left, right))
        notes = [f"wheel speed capped to {cap:.0f}mm/s"] if (l_, r_) != (float(left), float(right)) else []
        t = float(ms)
        if t > BODY_MAX_WHEELS_MS:
            t, notes = BODY_MAX_WHEELS_MS, notes + [f"time capped to {BODY_MAX_WHEELS_MS:.0f}ms"]
        t = max(0.0, t)
        if not self.aborted:
            if why := await self._prep_wheels():
                return await self._refused("wheels", why)
            if FEED.snapshot()["status"] & ST_ON_CHARGER:
                return await self._refused("wheels", "I'm on my charger")
            try:
                await self.transport.wheels(l_, r_)
            except BaseException:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(asyncio.wait_for(self.transport.stop(), 4.0))
                raise
        # wheels are running: the guard times them (sleep_s) and stops them afterwards
        r = await self._guard(None, "wheels", timeout_s=t / 1000 + 2.0, stop_when=stop_when,
                              sleep_s=t / 1000)
        r.notes += notes
        return r

    async def stop(self) -> MotionResult:
        t0 = time.monotonic()
        with contextlib.suppress(BaseException):
            await asyncio.shield(asyncio.wait_for(self.transport.stop(), 4.0))
        r = MotionResult(kind="stop", code="stopped", duration_s=time.monotonic() - t0)
        self.results.append(r)
        return r

    async def wait(self, ms: float) -> MotionResult:
        t = max(0.0, min(5000.0, float(ms)))
        return await self._guard(None, "wait", timeout_s=t / 1000 + 2.0, sleep_s=t / 1000,
                                 wheels_still=True)

    # ----------------------------------------------------------------- summary
    def summary(self) -> dict:
        s0 = self.start or FEED.snapshot()
        s1 = FEED.snapshot()
        return {
            "label": self.label,
            "steps": len(self.results),
            "net_mm": round(math.hypot(s1["x"] - s0["x"], s1["y"] - s0["y"]), 1),
            "path_mm": round(sum(abs(r.disp_mm) for r in self.results if r.kind in ("drive", "wheels")), 1),
            "heading_deg": round(s1["heading_unwrapped"] - s0["heading_unwrapped"], 1),
            "prox_before": _prox_val(s0),
            "prox_after": _prox_val(s1),
            "duration_s": round(time.monotonic() - self.t0, 2) if self.t0 else 0.0,
            "stop_reason": (f"stopped: {self.aborted}" if self.aborted else
                            next((r.stop_reason for r in reversed(self.results) if r.stop_reason), None)),
            "aborted": bool(self.aborted),
            "cube_moved": s1["cube_moved_seq"] != s0["cube_moved_seq"],
            "transport": self.transport.name if self.transport else "?",
            "notes": sorted({n for r in self.results for n in r.notes}),
        }


# ------------------------------------------------- single-motion convenience (own batch)
async def _one(label: str, fn) -> MotionResult:
    async with ENGINE.batch(label) as b:
        return await fn(b)


async def drive(mm: float, speed: float = 60, fast: bool = False, stop_when: dict | None = None) -> MotionResult:
    return await _one("drive", lambda b: b.drive(mm, speed, fast, stop_when))


async def turn(deg: float) -> MotionResult:
    return await _one("turn", lambda b: b.turn(deg))


async def head(deg: float) -> MotionResult:
    return await _one("head", lambda b: b.head(deg))


async def lift(height: float) -> MotionResult:
    return await _one("lift", lambda b: b.lift(height))


async def wheels(left: float, right: float, ms: float, fast: bool = False) -> MotionResult:
    return await _one("wheels", lambda b: b.wheels(left, right, ms, fast))


async def stop() -> MotionResult:
    """Emergency stop: no lock (must work while another motion is running)."""
    t = ENGINE.transport_for_batch()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(t.stop(), 4.0)
    return MotionResult(kind="stop", code="stopped")


async def drive_off_charger() -> str:
    """Use his own built-in routine to drive off the charger (the governor refuses plain
    wheel moves there). Returns a plain-words result."""
    if not (FEED.snapshot()["status"] & ST_ON_CHARGER):
        return "not on the charger"
    async with ENGINE.batch("drive off charger", max_s=25) as b:
        if not isinstance(b.transport, GrpcTransport):
            raise BodyError("can't drive off the charger on this transport")
        from anki_vector.messaging import protocol
        await asyncio.wait_for(b.transport._run(
            lambda r: r.conn.grpc_interface.DriveOffCharger(protocol.DriveOffChargerRequest())), 25)
    return "drove off the charger"
