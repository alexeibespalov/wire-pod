"""Watching the TV: turning back after WireOS turns him toward a voice, overhearing
(app/listen.py), and how ended schedules show up in his prompt. No robot.

Run:  cd vector-agent && python -m pytest tests/test_watching.py -q
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import body, listen, scheduler  # noqa: E402

UPD = ["x", "y", "heading_deg", "origin_id", "pitch_deg", "head_deg", "lift_mm", "wheel_l", "wheel_r",
       "accel", "gyro", "prox_mm", "prox_quality", "prox_found", "prox_lift", "touched", "touch_raw", "status"]


def feed_set(**over):
    s = body.FEED.snapshot()
    kw = {k: s[k] for k in UPD}
    kw.update(over)
    body.FEED.update(**kw)


@pytest.fixture
def watching(monkeypatch):
    body.FEED.reset()
    feed_set(heading_deg=10.0, origin_id=3)
    turns: list[float] = []

    async def fake_turn(deg):
        turns.append(deg)
        return body.MotionResult(kind="turn", code="ok")

    monkeypatch.setattr(body, "turn", fake_turn)
    monkeypatch.setattr(scheduler, "_schedules", [{"id": "ab12", "status": "active", "hold_still": True,
                                                   "every_s": 15, "title": "watching TV"}])
    monkeypatch.setattr(scheduler, "_save", lambda: None)
    monkeypatch.setattr(scheduler, "_wake_heading", None)
    monkeypatch.setattr(scheduler, "_drift", 0.0)
    monkeypatch.setattr(scheduler, "_drift_origin", None)
    return turns


def test_turns_back_after_wake_word_turn(watching):
    scheduler.on_wake_word()                 # "Hey Vector" from the sofa behind him
    feed_set(heading_deg=100.0, origin_id=3)  # WireOS swung him 90deg toward the voice
    scheduler.on_turn_start()
    feed_set(heading_deg=130.0, origin_id=3)  # ...and he turned 30deg more on request
    asyncio.run(scheduler._reaim())
    assert watching == [pytest.approx(-90.0)]   # only the firmware's part is undone


def test_turn_by_hand_is_left_alone(watching):
    feed_set(heading_deg=60.0, origin_id=3)  # someone turned him toward the TV
    asyncio.run(scheduler._reaim())
    assert watching == []


def test_small_turns_and_relocalising_are_ignored(watching):
    scheduler.on_wake_word()
    feed_set(heading_deg=20.0, origin_id=3)  # 10deg: not worth a turn
    scheduler.on_turn_start()
    asyncio.run(scheduler._reaim())
    assert watching == []
    scheduler.on_wake_word()
    feed_set(heading_deg=120.0, origin_id=4)  # picked up: headings don't compare
    scheduler.on_turn_start()
    asyncio.run(scheduler._reaim())
    assert watching == []


def test_wake_word_with_no_turn_start_still_counts(watching, monkeypatch):
    scheduler.on_wake_word()
    feed_set(heading_deg=-80.0, origin_id=3)   # turned, then nobody said anything
    h, o, _t = scheduler._wake_heading
    monkeypatch.setattr(scheduler, "_wake_heading", (h, o, time.time() - 10))
    asyncio.run(scheduler._reaim())
    assert watching == [pytest.approx(90.0)]


def test_no_wake_tracking_without_a_hold_still_schedule(watching, monkeypatch):
    monkeypatch.setattr(scheduler, "_schedules", [])
    scheduler.on_wake_word()
    assert scheduler._wake_heading is None


def test_stopped_watch_shows_in_prompt(monkeypatch):
    s = {"id": "0494", "status": "active", "every_s": 15, "title": "watching shorts", "runs": 46}
    monkeypatch.setattr(scheduler, "_schedules", [s])
    scheduler._end(s, "failed", "my camera stopped giving me pictures")
    sec = scheduler.prompt_section()
    assert "RECENTLY STOPPED" in sec and "camera stopped" in sec and "46 runs" in sec
    assert "RIGHT NOW" not in sec
    scheduler._end(s, "done", "later")            # first ending wins
    assert s["status"] == "failed"
    s["ended_at"] = time.time() - 3 * 3600           # long ago: dropped
    assert scheduler.prompt_section() == ""


def test_overhear_gets_the_transcript(monkeypatch):
    async def fake_sdk(endpoint, **params):
        assert endpoint == "overhear" and params["seconds"] == "10"
        asyncio.get_running_loop().call_later(0.05, listen.deliver, "[overheard] he scored a goal!")
        return "listening"

    monkeypatch.setattr(listen.robot, "sdk", fake_sdk)
    assert asyncio.run(listen.overhear(10)) == "he scored a goal!"
    assert listen.deliver("[overheard] late") is False   # nobody waiting any more


def test_overhear_fails_softly(monkeypatch):
    async def unpatched(endpoint, **params):
        raise RuntimeError("overhear: not found")

    monkeypatch.setattr(listen.robot, "sdk", unpatched)
    assert asyncio.run(listen.overhear(10)) is None


def test_capture_text_helpers():
    assert listen.is_capture("[overheard] (music) la la")
    assert not listen.is_capture("what's the weather")
    assert listen.strip("[overheard] hello there") == "hello there"
    assert listen.addressed_to_him("Hey Vector, who's winning?")
    assert listen.addressed_to_him("vector what was that")
    assert not listen.addressed_to_him("the vectors in this game are wild")


def test_listens_about_once_a_minute(monkeypatch):
    monkeypatch.setattr(listen, "_last_try", float("-inf"))
    assert listen.due()

    async def fake_sdk(endpoint, **params):
        asyncio.get_running_loop().call_later(0.01, listen.deliver, "[overheard] goal")
        return "listening"

    monkeypatch.setattr(listen.robot, "sdk", fake_sdk)
    asyncio.run(listen.overhear(10))
    assert not listen.due()                      # looks 2-4 (15/30/45 s later) skip it
    monkeypatch.setattr(listen, "_last_try", time.monotonic() - 56)
    assert listen.due()                          # the 4th look, a minute on, listens again
