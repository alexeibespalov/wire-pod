"""Tests for app/body.py (governor, transports) and app/maneuvers.py. No robot, no SDK.

Run:  cd vector-agent && python -m pytest tests/test_body.py -q
(needs: pydantic, langchain-core, pytest)
"""
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import body, maneuvers  # noqa: E402
from app.body import ST_CLIFF, ST_FALLING, ST_HELD, ST_ON_CHARGER, ST_PICKED_UP  # noqa: E402

UPD = ["x", "y", "heading_deg", "origin_id", "pitch_deg", "head_deg", "lift_mm", "wheel_l", "wheel_r",
       "accel", "gyro", "prox_mm", "prox_quality", "prox_found", "prox_lift", "touched", "touch_raw", "status"]


def run(coro):
    return asyncio.run(coro)


def feed_set(**over):
    s = body.FEED.snapshot()
    kw = {k: s[k] for k in UPD}
    kw.update(over)
    body.FEED.update(**kw)


def feed_move(along=0.0, dturn=0.0):
    s = body.FEED.snapshot()
    h = math.radians(s["heading_deg"])
    feed_set(x=s["x"] + along * math.cos(h), y=s["y"] + along * math.sin(h),
             heading_deg=body.wrap180(s["heading_deg"] + dturn))


class FakeTransport(body.Transport):
    """Pretends to be the robot: displacement = 0.8 * commanded mm. Moves the fake feed."""
    name = "fake"

    def __init__(self):
        self.acquired = self.released = self.stops = 0
        self.calls = []
        self.spans = []
        self.on_tick = None        # fn(i) called each tick of a drive
        self.drive_s = 0.05
        self.boom = False
        self.gain = 0.8

    async def acquire(self):
        self.acquired += 1

    async def release(self):
        self.released += 1

    async def stop(self):
        self.stops += 1

    async def drive(self, mm, speed):
        self.calls.append(("drive", mm, speed))
        t0 = time.monotonic()
        n = 6
        for i in range(n):
            await asyncio.sleep(self.drive_s / n)
            if self.boom:
                raise RuntimeError("boom")
            feed_move(along=mm * self.gain / n)
            if self.on_tick:
                self.on_tick(i)
        self.spans.append((t0, time.monotonic()))
        return "ACTION_RESULT_SUCCESS"

    async def turn(self, deg):
        self.calls.append(("turn", deg))
        await asyncio.sleep(0.01)
        feed_move(dturn=deg)
        return "ACTION_RESULT_SUCCESS"

    async def head(self, deg):
        self.calls.append(("head", deg))
        await asyncio.sleep(0.005)
        feed_set(head_deg=deg)
        return "ACTION_RESULT_SUCCESS"

    async def lift(self, mm):
        self.calls.append(("lift", mm))
        await asyncio.sleep(0.005)
        feed_set(lift_mm=mm)
        return "ACTION_RESULT_SUCCESS"

    async def wheels(self, left, right):
        self.calls.append(("wheels", left, right))

    def connected(self):
        return True


@pytest.fixture
def rig(monkeypatch, tmp_path):
    fake = FakeTransport()
    monkeypatch.setattr(body, "ENGINE", body.BodyEngine(lambda: fake, battery_check=lambda: _false()))
    monkeypatch.setattr(body, "is_connected", lambda: True)
    monkeypatch.setattr(body, "SETTLE_S", 0.0)
    monkeypatch.setattr(body, "POLL_S", 0.005)
    monkeypatch.setattr(body, "BODY_STALE_S", 5.0)
    monkeypatch.setattr(maneuvers, "catalogue_dir", lambda: tmp_path / "maneuvers")
    body.FEED.reset()
    feed_set(accel=(0.0, 0.0, 9810.0))
    return fake


async def _false():
    return False


# ================================================================== governor
@pytest.mark.parametrize("bit,word", [(ST_CLIFF, "cliff"), (ST_PICKED_UP, "picked up"),
                                      (ST_HELD, "held"), (ST_FALLING, "falling")])
def test_hazard_aborts_and_stops(rig, bit, word):
    def trip(i):
        if i == 2:
            feed_set(status=bit)
    rig.on_tick = trip

    async def go():
        async with body.ENGINE.batch("t") as b:
            r1 = await b.drive(100)
            r2 = await b.turn(30)          # batch is dead: refused
            return b, r1, r2
    b, r1, r2 = run(go())
    assert r1.aborted and word in r1.stop_reason and r1.stop_reason.startswith("stopped:")
    assert r2.aborted and r2.code == "refused"
    assert rig.stops >= 1 and rig.released == 1
    assert ("turn", 30) not in rig.calls


def test_refuse_to_start_when_picked_up(rig):
    feed_set(status=ST_PICKED_UP)
    with pytest.raises(body.BodyError, match="picked up"):
        run(body.drive(50))
    assert rig.acquired == 0


def test_leash(rig, monkeypatch):
    monkeypatch.setattr(body, "BODY_LEASH_MM", 100)
    rig.drive_s = 0.2
    r = run(body.drive(300, 100))
    assert r.aborted and "too far" in r.stop_reason
    assert abs(r.disp_mm) < 300 * 0.8      # was stopped before arriving
    assert rig.stops >= 1


def test_origin_change_aborts(rig):
    rig.on_tick = lambda i: feed_set(origin_id=7) if i == 1 else None
    r = run(body.drive(100))
    assert r.aborted and "relocalised" in r.stop_reason


def test_unexpected_movement_ends_step_only(rig):
    """Pushing something makes the wheels meet resistance ('unexpected movement'): that
    step stops (motors stopped), but it isn't a hard abort, so a push can carry on."""
    rig.on_tick = lambda i: body.FEED.on_unexpected_movement() if i == 1 else None
    r = run(body.drive(100))
    assert not r.aborted and "bumped" in r.stop_reason
    assert rig.stops >= 1


def test_stale_feed_aborts_mid_motion(rig, monkeypatch):
    monkeypatch.setattr(body, "BODY_STALE_S", 0.15)
    rig.gain = 0.0
    rig.drive_s = 0.0

    async def silent_drive(mm, speed):      # feed stops updating while "moving"
        feed_set()
        await asyncio.sleep(1.0)
        return "ACTION_RESULT_SUCCESS"
    rig.drive = silent_drive
    t = time.monotonic()
    r = run(body.drive(100))
    assert r.aborted and "sensors" in r.stop_reason
    assert time.monotonic() - t < 0.8
    assert rig.stops >= 1


def test_stale_feed_refuses_to_start(rig, monkeypatch):
    monkeypatch.setattr(body, "BODY_STALE_S", 0.1)
    feed_set(ts=time.time() - 5)
    with pytest.raises(body.BodyError, match="sensors"):
        run(body.drive(50))


def test_time_cap(rig):
    rig.drive_s = 0.5

    async def go():
        async with body.ENGINE.batch("t", max_s=0.1) as b:
            return await b.drive(100)
    r = run(go())
    assert r.aborted and "time limit" in r.stop_reason


def test_charger_refuses_wheels_but_not_head(rig):
    feed_set(status=ST_ON_CHARGER)

    async def go():
        async with body.ENGINE.batch("t") as b:
            h = await b.head(20)
            d = await b.drive(50)
            return h, d
    h, d = run(go())
    assert h.ok and not h.aborted
    assert d.aborted and "charger" in d.stop_reason
    assert not any(c[0] == "drive" for c in rig.calls)


def test_battery_low_refuses(monkeypatch, rig):
    async def low():
        return True
    monkeypatch.setattr(body, "ENGINE", body.BodyEngine(lambda: rig, battery_check=low))
    r = run(body.drive(50))
    assert r.aborted and "battery" in r.stop_reason


def test_clamps(rig):
    async def go():
        async with body.ENGINE.batch("t") as b:
            return await b.drive(1000, 500), await b.head(99), await b.lift(5)
    d, h, lf = run(go())
    assert rig.calls[0] == ("drive", 300.0, 120.0)
    assert ("head", 45.0) in rig.calls
    assert ("lift", 92.0) in rig.calls
    assert any("capped" in n for n in d.notes)


def test_stop_when_prox(rig):
    rig.drive_s = 0.3

    def near(i):
        if i == 2:
            feed_set(prox_mm=35, prox_quality=0.5, prox_found=True)
    rig.on_tick = near
    r = run(body.drive(200, 60, stop_when={"prox_below_mm": 40}))
    assert not r.aborted and r.stop_reason.startswith("stop_when")
    assert r.prox_after == 35
    assert rig.stops >= 1


def test_measured_result(rig):
    r = run(body.drive(100, 60))
    assert r.ok and r.code == "ACTION_RESULT_SUCCESS"
    assert r.disp_mm == pytest.approx(80, abs=0.5)
    r = run(body.turn(-90))
    assert r.heading_deg == pytest.approx(-90, abs=0.5)


# ----------------------------------------------------- control + lock + watchdog
def test_one_control_acquisition_per_batch(rig):
    async def go():
        async with body.ENGINE.batch("m") as b:
            for _ in range(3):
                await b.drive(20)
            await b.turn(10)
    run(go())
    assert rig.acquired == 1 and rig.released == 1


def test_exception_stops_and_releases(rig):
    rig.boom = True
    with pytest.raises(RuntimeError):
        async def go():
            async with body.ENGINE.batch("m") as b:
                await b.drive(50)
                raise RuntimeError("tool bug")
        run(go())
    assert rig.stops >= 1 and rig.released == 1


def test_cancellation_stops_and_releases(rig):
    rig.drive_s = 2.0

    async def go():
        t = asyncio.ensure_future(body.drive(200))
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # lock must be free again
        await asyncio.wait_for(body.ENGINE.lock().acquire(), 1)
        body.ENGINE.lock().release()
    run(go())
    assert rig.stops >= 1 and rig.released == 1


def test_motions_serialise(rig):
    rig.drive_s = 0.1

    async def go():
        await asyncio.gather(body.drive(30), body.drive(30), body.drive(30))
    run(go())
    spans = sorted(rig.spans)
    assert len(spans) == 3
    for (_, e1), (s2, _) in zip(spans, spans[1:]):
        assert s2 >= e1 - 1e-3          # no overlap
    assert rig.acquired == 3 and rig.released == 3


def test_wirepod_control_borrows_on_grpc(monkeypatch):
    """While wire-pod holds control we act under it on gRPC (verified on the robot) and
    never request control ourselves."""
    from app import turn
    t = body.GrpcTransport(get_robot=lambda: object())
    eng = body.BodyEngine(lambda: t)
    monkeypatch.setattr(turn, "wirepod_has_control", True)
    assert eng.transport_for_batch() is t and t.borrow

    async def probe_ok():
        return True
    monkeypatch.setattr(t, "_probe_borrow", probe_ok)
    asyncio.run(t.acquire())
    assert not t._have_control           # nothing requested, nothing to release
    monkeypatch.setattr(turn, "wirepod_has_control", False)
    assert eng.transport_for_batch() is t and not t.borrow


def test_describe_and_snapshot(rig):
    feed_set(x=12, y=3, prox_mm=120, prox_found=True, prox_quality=0.4, status=ST_ON_CHARGER)
    text = body.describe("all")
    assert "prox_mm=120" in text and "on_charger=yes" in text and "heading=" in text
    assert body.describe("distance").startswith("prox_mm=120")
    assert "on_charger" in body.snapshot()["flags"]


# ============================================================== maneuver schema
def cat(tmp_path):
    return maneuvers.Catalogue(tmp_path / "maneuvers")


def test_schema_valid_and_errors(tmp_path):
    c = cat(tmp_path)
    c.validate([{"step": "turn", "deg": 10}, {"step": "drive", "mm": 50, "stop_when": {"prox_below_mm": 40}},
                {"step": "if", "sensor": {"touch": True}, "then": [{"step": "head", "deg": 20}],
                 "else": [{"step": "wait", "ms": 100}]}], {})
    for bad in ([{"step": "fly"}], [{"step": "drive"}], [{"step": "drive", "mm": "far"}],
                [{"step": "drive", "mm": 5, "bogus": 1}], [], "nope",
                [{"step": "if", "sensor": {}, "then": []}]):
        with pytest.raises(maneuvers.ManeuverError):
            c.validate(bad, {})


def test_undeclared_param_rejected_and_clamped(tmp_path):
    c = cat(tmp_path)
    with pytest.raises(maneuvers.ManeuverError, match="not a declared param"):
        c.validate([{"step": "drive", "mm": "$x"}], {})
    decl = maneuvers.parse_params({"mm": {"default": 50, "min": 10, "max": 200}})
    c.validate([{"step": "drive", "mm": "$mm"}], decl)
    assert maneuvers.bind_args(decl, {"mm": 999})["mm"] == 200
    assert maneuvers.bind_args(decl, {"mm": -5})["mm"] == 10
    assert maneuvers.bind_args(decl, {})["mm"] == 50
    with pytest.raises(maneuvers.ManeuverError, match="unknown arg"):
        maneuvers.bind_args(decl, {"zz": 1})
    # default is clamped into range too
    assert maneuvers.parse_params({"a": {"default": 500, "max": 100}})["a"].default == 100


def test_cycle_rejected(tmp_path):
    c = cat(tmp_path)
    c.save("a", "a", [{"step": "turn", "deg": 5}])
    c.save("b", "b", [{"step": "maneuver", "name": "a"}])
    with pytest.raises(maneuvers.ManeuverError, match="loop"):
        c.save("a", "a", [{"step": "maneuver", "name": "b"}])      # a -> b -> a
    with pytest.raises(maneuvers.ManeuverError, match="loop"):
        c.save("self", "x", [{"step": "maneuver", "name": "self"}])
    with pytest.raises(maneuvers.ManeuverError, match="unknown maneuver"):
        c.save("c", "c", [{"step": "maneuver", "name": "ghost"}])


def test_depth_limit(tmp_path):
    c = cat(tmp_path)
    c.save("m0", "x", [{"step": "turn", "deg": 5}])
    for i in range(1, 5):                                           # m4 -> m3 -> ... -> m0 : depth 4 ok
        c.save(f"m{i}", "x", [{"step": "maneuver", "name": f"m{i - 1}"}])
    with pytest.raises(maneuvers.ManeuverError, match="deeper"):
        c.save("m5", "x", [{"step": "maneuver", "name": "m4"}])     # depth 5


def test_expansion_cap(tmp_path):
    c = cat(tmp_path)
    ok = [{"step": "repeat", "times": 10, "steps": [{"step": "repeat", "times": 10, "steps": [{"step": "wait", "ms": 1}]}]}]
    c.validate(ok, {})                                               # 100
    big = [{"step": "repeat", "times": 20, "steps": [{"step": "repeat", "times": 20, "steps": [{"step": "wait", "ms": 1}]}]}]
    with pytest.raises(maneuvers.ManeuverError, match="too long"):
        c.validate(big, {})
    with pytest.raises(maneuvers.ManeuverError, match="repeat times"):
        c.validate([{"step": "repeat", "times": 99, "steps": [{"step": "wait", "ms": 1}]}], {})


# ================================================================ catalogue / stats
def test_catalogue_roundtrip_versions_and_limit(tmp_path, monkeypatch):
    c = cat(tmp_path)
    d = c.save("Little Nod!", "nods", [{"step": "head", "deg": 20}, {"step": "head", "deg": 0}],
               {"depth": 20}, ["Fun", "fun", " body "])
    assert d["slug"] == "little_nod" and d["status"] == "draft" and d["version"] == 1
    assert d["tags"] == ["body", "fun"] and d["params"]["depth"]["default"] == 20.0
    again = c.get("little_nod")
    assert again["steps"][0] == {"step": "head", "deg": 20.0}
    assert c.get("little") is not None                               # unique prefix lookup
    assert list(c.root.glob("*.tmp")) == [] and not list(c.root.glob(".*"))
    for i in range(8):
        d = c.save("little_nod", "nods", [{"step": "head", "deg": 10 + i}])
    assert d["version"] == 9 and len(d["history"]) == 5
    assert d["history"][-1]["version"] == 8
    same = c.save("little_nod", "nods v", [{"step": "head", "deg": 17}])    # unchanged steps
    assert same["version"] == 9
    monkeypatch.setattr(maneuvers, "MAX_MANEUVERS", 2)
    c.save("second", "x", [{"step": "wait", "ms": 1}])
    with pytest.raises(maneuvers.ManeuverError, match="delete one"):
        c.save("third", "x", [{"step": "wait", "ms": 1}])
    assert c.delete("second") and c.get("second") is None


def test_stats_ema_and_status(tmp_path):
    c = cat(tmp_path)
    c.save("n", "x", [{"step": "wait", "ms": 1}])
    assert c.get("n")["status"] == "draft" and c.get("n")["stats"]["competency"] is None
    d = c.record_outcome("n", "failure")
    assert d["status"] == "draft" and d["stats"]["competency"] == 0.0
    d = c.record_outcome("n", "success")        # 0.8*0 + 0.2*1
    assert d["status"] == "tested" and d["stats"]["competency"] == pytest.approx(0.2)
    d = c.record_outcome("n", "partial")        # 0.8*0.2 + 0.2*0.5
    assert d["stats"]["competency"] == pytest.approx(0.26)
    assert (d["stats"]["successes"], d["stats"]["partials"], d["stats"]["failures"]) == (1, 1, 1)
    c.mark_run("n")
    assert c.get("n")["stats"]["runs"] == 1 and c.get("n")["stats"]["last_used"]
    with pytest.raises(maneuvers.ManeuverError):
        c.record_outcome("n", "great")
    # changing steps drops it back to draft
    d = c.save("n", "x", [{"step": "wait", "ms": 2}])
    assert d["status"] == "draft" and d["stats"]["successes"] == 1


def test_attempts_log_fill(tmp_path):
    log = maneuvers.AttemptLog(tmp_path / "attempts.jsonl")
    a = log.append("nod", {"mm": 5}, {"net_mm": 3})
    b = log.append("nod", {}, {})
    assert log.fill("nod", "success", "ok") == b       # newest open one
    assert log.fill("nod", "failure", "x") == a
    assert log.fill("nod", "failure", "x") is None
    rows = [json.loads(line) for line in (tmp_path / "attempts.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in rows] == ["failure", "success"]


# ===================================================================== dry run
def test_dry_run_maths(tmp_path):
    c = cat(tmp_path)
    r = maneuvers.dry_run([{"step": "wheels", "left": 50, "right": 50, "ms": 1000}], cat=c)
    assert r["end"]["x_mm"] == pytest.approx(50) and r["end"]["y_mm"] == pytest.approx(0, abs=1e-6)
    assert r["total_s"] == pytest.approx(1.0) and r["ok"]
    r = maneuvers.dry_run([{"step": "wheels", "left": -50, "right": 50, "ms": 1000}], cat=c)
    assert r["end"]["heading_deg"] == pytest.approx(math.degrees(100 / body.BODY_TRACK_MM), abs=0.2)
    assert r["max_dist_mm"] == pytest.approx(0, abs=1e-6)
    # half circle: left slow, right fast, turns left; quarter turn then straight goes up +y
    r = maneuvers.dry_run([{"step": "turn", "deg": 90}, {"step": "drive", "mm": 100, "speed": 50}], cat=c)
    assert r["end"]["x_mm"] == pytest.approx(0, abs=1e-6) and r["end"]["y_mm"] == pytest.approx(100)
    assert r["total_s"] == pytest.approx(0.9 + 2.0)
    # repeat + param + violations
    r = maneuvers.dry_run([{"step": "repeat", "times": 3, "steps": [{"step": "drive", "mm": "$d", "speed": 200}]}],
                          {"d": {"default": 250}}, cat=c)
    assert not r["ok"]
    assert any("speed" in v for v in r["violations"])
    assert r["max_dist_mm"] == pytest.approx(750)
    assert any("leash" in v for v in r["violations"])
    r = maneuvers.dry_run([{"step": "drive", "mm": 900}], cat=c)
    assert any("per step" in v for v in r["violations"]) and r["max_dist_mm"] == pytest.approx(300)
    r = maneuvers.dry_run([{"step": "wheels", "left": 50, "right": 50, "ms": 9000}], cat=c)
    assert any("3000ms" in v for v in r["violations"])


def test_dry_run_calls_other_maneuvers(tmp_path):
    c = cat(tmp_path)
    c.save("step", "s", [{"step": "drive", "mm": "$mm", "speed": 50}], {"mm": 40})
    r = maneuvers.dry_run([{"step": "maneuver", "name": "step", "args": {"mm": 60}},
                           {"step": "maneuver", "name": "step"}], cat=c)
    assert r["end"]["x_mm"] == pytest.approx(100)


# ============================================================ runner + tools + tune
def test_run_steps_executes_with_one_acquisition(rig, tmp_path):
    steps = [{"step": "repeat", "times": 2, "steps": [{"step": "turn", "deg": 10}, {"step": "drive", "mm": 50}]},
             {"step": "head", "deg": 20}]
    r = run(maneuvers.run_steps(steps))
    assert r["steps"] == 5 and rig.acquired == 1 and rig.released == 1
    assert r["disp_mm"] > 0 and r["heading_deg"] == pytest.approx(20, abs=0.5)
    assert not r["aborted"]
    assert "5 steps" in maneuvers.format_summary("moved", r)


def test_if_sensor_branch(rig):
    steps = [{"step": "if", "sensor": {"prox_below_mm": 100}, "then": [{"step": "head", "deg": 30}],
              "else": [{"step": "head", "deg": -10}]}]
    run(maneuvers.run_steps(steps))
    assert rig.calls == [("head", -10.0)]
    feed_set(prox_mm=50, prox_quality=0.5, prox_found=True)
    rig.calls.clear()
    run(maneuvers.run_steps(steps))
    assert rig.calls == [("head", 30.0)]


def test_tools_end_to_end(rig):
    out = run(maneuvers.move.ainvoke({"steps": [{"step": "bogus"}]}))
    assert out.startswith("error:")
    out = run(maneuvers.move.ainvoke({"steps": json.dumps([{"step": "drive", "mm": 50}])}))
    assert "moved" in out and "40mm" in out
    out = maneuvers.save_maneuver.invoke({"name": "nudge", "description": "nudge fwd",
                                          "steps": [{"step": "drive", "mm": "$mm", "speed": 60}],
                                          "params": {"mm": {"default": 50, "min": 10, "max": 300}}})
    assert out.startswith("saved 'nudge'")
    out = run(maneuvers.run_maneuver.ainvoke({"name": "nudge", "values": {"mm": 100}}))
    assert "moved 80mm" in out and "record_attempt" in out
    out = maneuvers.record_attempt.invoke({"name": "nudge", "outcome": "success", "note": "good"})
    assert "status tested" in out
    assert "nudge" in maneuvers.list_maneuvers.invoke({"query": "nud"})
    assert '"steps"' in maneuvers.show_maneuver.invoke({"name": "nudge"})
    assert "MANEUVERS YOU KNOW" in maneuvers.known_maneuvers_prompt()
    assert maneuvers.run_maneuver.name in maneuvers.MANEUVER_CONTROL_TOOLS
    assert maneuvers.sense.name not in maneuvers.MANEUVER_CONTROL_TOOLS
    assert "removed" or maneuvers.delete_maneuver.invoke({"name": "nudge"}).startswith("deleted")


def test_run_aborted_reports_reason(rig):
    rig.on_tick = lambda i: feed_set(status=ST_PICKED_UP) if i == 1 else None
    out = run(maneuvers.move.ainvoke({"steps": [{"step": "drive", "mm": 100}, {"step": "turn", "deg": 90}]}))
    assert "stopped: picked up" in out
    assert not any(c[0] == "turn" for c in rig.calls)


def test_tune_hits_target_with_fake_gain(rig):
    maneuvers.save_maneuver.invoke({"name": "nudge", "description": "d",
                                    "steps": [{"step": "drive", "mm": "$mm", "speed": 60}],
                                    "params": {"mm": {"default": 50, "min": 10, "max": 300}}})
    rig.drive_s = 0.01
    out = run(maneuvers.tune.ainvoke({"name": "nudge", "param": "mm", "low": 20, "high": 300, "trials": 12,
                                      "measure": "displacement_mm", "target": "100"}))
    assert out.startswith("tuned 'nudge' mm"), out
    d = maneuvers.catalogue().get("nudge")
    best = d["params"]["mm"]["default"]
    assert abs(0.8 * best - 100) <= 6, (best, out)          # true optimum is 125 mm
    drives = [c for c in rig.calls if c[0] == "drive"]
    assert len(drives) > 12                                  # trials + the drive-backs
    # it drove back to (about) where it started each time
    s = body.FEED.snapshot()
    assert math.hypot(s["x"], s["y"]) < 40
    assert rig.acquired == rig.released


def test_tune_max_and_validation(rig):
    maneuvers.save_maneuver.invoke({"name": "nudge", "description": "d",
                                    "steps": [{"step": "drive", "mm": "$mm"}],
                                    "params": {"mm": {"default": 50, "min": 10, "max": 300}}})
    rig.drive_s = 0.01
    out = run(maneuvers.tune.ainvoke({"name": "nudge", "param": "mm", "low": 20, "high": 200, "trials": 6,
                                      "measure": "displacement_mm", "target": "max"}))
    assert out.startswith("tuned")
    assert maneuvers.catalogue().get("nudge")["params"]["mm"]["default"] > 120
    assert run(maneuvers.tune.ainvoke({"name": "nudge", "param": "zz", "low": 1, "high": 2})).startswith("error")
    assert run(maneuvers.tune.ainvoke({"name": "ghost", "param": "mm", "low": 1, "high": 2})).startswith("error")


def test_tune_stops_on_abort(rig):
    maneuvers.save_maneuver.invoke({"name": "nudge", "description": "d",
                                    "steps": [{"step": "drive", "mm": "$mm"}],
                                    "params": {"mm": {"default": 50, "min": 10, "max": 300}}})
    rig.drive_s = 0.01
    rig.on_tick = lambda i: feed_set(status=ST_CLIFF) if i == 1 else None
    out = run(maneuvers.tune.ainvoke({"name": "nudge", "param": "mm", "low": 20, "high": 200, "trials": 6,
                                      "target": "max"}))
    assert "stopped" in out and "Nothing saved" in out or out.startswith("error")
    assert maneuvers.catalogue().get("nudge")["params"]["mm"]["default"] == 50


def test_borrow_probe_falls_back_to_taking_control(monkeypatch):
    """If wire-pod doesn't really hold control, the no-op probe hangs: take control ourselves."""
    t = body.GrpcTransport(get_robot=lambda: object(), borrow=True)
    calls = []

    async def probe_fails():
        return False
    monkeypatch.setattr(t, "_probe_borrow", probe_fails)

    class FakeConn:
        def request_control(self, *_a, **_k):
            import concurrent.futures
            f = concurrent.futures.Future(); f.set_result(None); calls.append("request"); return f

    class FakeRobot:
        conn = FakeConn()
    t._get_robot = lambda: FakeRobot()
    import sys, types
    fake = types.ModuleType("anki_vector"); fake_conn = types.ModuleType("anki_vector.connection")
    class ControlPriorityLevel:
        DEFAULT_PRIORITY = 20
    fake_conn.ControlPriorityLevel = ControlPriorityLevel
    monkeypatch.setitem(sys.modules, "anki_vector", fake)
    monkeypatch.setitem(sys.modules, "anki_vector.connection", fake_conn)
    asyncio.run(t.acquire())
    assert calls == ["request"] and t._have_control and not t.borrow
