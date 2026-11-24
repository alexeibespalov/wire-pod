"""Long-term memory on the belief store: what his prompt shows, how notes become statements
(each with a reason and evidence), forgetting, and the tools' fallbacks. No network, no robot.

Run:  cd vector-agent && python -m pytest tests/test_beliefs.py -q
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import belief_sleep, beliefs, config, memory  # noqa: E402
from agent_core import notes, reconcile  # noqa: E402

LEO = {"id": "b1", "store": "fabric", "subject": "home/person:leo#favourite_game@human:leo", "node": "home/person:leo",
       "field": "favourite_game", "source": "human:leo", "value": "Minecraft", "claim": "favourite_game: Minecraft",
       "class": "FACT", "actor": "human:leo", "valid_from": "2026-11-21T16:05:00Z"}
ARIA = {**LEO, "id": "b2", "subject": "home/person:leo#favourite_game@human:aria", "source": "human:aria",
        "actor": "human:aria", "value": "Animal Crossing", "claim": "favourite_game: Animal Crossing"}
NOTE = {"id": "b3", "store": "dialogue", "subject": "note-1", "claim": "Aria has a wife.", "class": "OBSERVATION",
        "actor": "agent:vector", "valid_from": "2026-11-23T21:41:00Z"}


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setattr(beliefs, "ENABLED", True)
    monkeypatch.setattr(notes, "_PATH", tmp_path / "memories.json")
    monkeypatch.setattr(beliefs, "_snap", {"version": "v", "beliefs": [], "fetched": 0})

    async def down(*a, **k):
        raise beliefs.Unavailable("test: NUC unplugged")
    monkeypatch.setattr(beliefs, "request", down)
    return beliefs._snap


def test_prompt_shows_settled_beliefs_and_both_sides_of_an_unsettled_one(on):
    assert "nothing cemented yet" in beliefs.prompt_section()
    on["beliefs"] = [{**LEO, "contested_with": ["b2"]}, {**ARIA, "contested_with": ["b1"]}, NOTE]
    text = beliefs.prompt_section()
    assert "- Aria has a wife." in text
    unsettled = text.split("UNSETTLED")[1]
    assert unsettled.count("Leo: favourite game") == 1                      # one line for the pair
    assert "Minecraft (Leo said" in unsettled and "BUT Animal Crossing (Aria said" in unsettled
    on["beliefs"] = [LEO, NOTE]
    assert "UNSETTLED" not in beliefs.prompt_section()
    assert "- Leo: favourite game: Minecraft (Leo said)" in beliefs.prompt_section()


def test_wording():
    assert beliefs.node_label("home/place:coffee-table") == "The coffee table (place)"
    assert beliefs.node_label("home/household:family") == "The family"
    assert (beliefs.who("human:leo"), beliefs.who("agent:vector"), beliefs.who("human:unknown")) == ("Leo", "you", "someone")


def test_recall_falls_back_to_the_cached_beliefs_when_the_store_is_down(on):
    on["beliefs"] = [LEO, NOTE]
    out = asyncio.run(beliefs.recall.ainvoke({"question": "what is Leo's favourite game?"}))
    assert "cached" in out and "Minecraft" in out and "wife" not in out
    out = asyncio.run(beliefs.belief_history.ainvoke({"about": "Leo's favourite game"}))
    assert out.startswith("error") and "can't be reached" in out


def test_forgetting_drops_notes_now_and_leaves_a_request_for_the_sleep_job(on):
    memory.add("Leo is going to Spain on Friday")
    out = memory.forget("Spain")
    assert "dropped 1" in out and "next sleep" in out
    notes = memory.all_facts()
    assert len(notes) == 1 and notes[0].startswith(memory.FORGET_PREFIX) and notes[0].endswith("Spain")


def test_line_time_is_the_latest_matching_day_not_in_the_future():
    now = datetime(2026, 11, 24, 21, 0, tzinfo=config.TIMEZONE)            # a Tuesday
    assert belief_sleep.line_time("LEO (Tue 4:15 PM): hi", now) == now.replace(hour=16, minute=15)
    assert belief_sleep.line_time("LEO (Tue 11:30 PM): hi", now).day == 17  # not tonight: last week
    assert belief_sleep.line_time("LEO (Sat 10:39 AM): hi", now).day == 21
    assert belief_sleep.line_time("SOMEONE: hi", now) is None


def test_every_statement_gets_a_reason_and_evidence_and_no_note_is_lost():
    ex = {"now": "2026-11-24T21:00:00.000+00:00", "meta": {
        "L1": {"id": "h1", "thread": "voice-1", "t": "2026-11-24T16:05:00.000+00:00"},
        "N1": {"note": "Leo's favourite game is Animal Crossing", "saved": "2026-11-24T12:00+00:00",
               "t": "2026-11-24T12:00:00.501+00:00"},
        "N2": {"note": "Aria has a wife.", "saved": "2026-11-24T13:00+00:00", "t": "2026-11-24T13:00:00.502+00:00"},
        "N3": {"note": "Asked to forget (Tue 24 November): Spain", "saved": "2026-11-24T14:00+00:00",
               "t": "2026-11-24T14:00:00.503+00:00"}}}
    plan = {"statements": [
        {"node": "person:Leo", "field": "Favourite Game", "value": "Minecraft", "said_by": "Leo", "from": ["L1", "N1"]},
        {"node": "person:maya", "field": "age", "value": "four", "said_by": "unknown", "reason": "heard it", "from": ["L1"]},
        {"node": "person:aria", "field": "plan_party", "value": "Saturday", "said_by": "unknown", "from": ["N2"]},
        {"node": "nonsense", "field": "x", "value": "y", "said_by": "leo"}],
        "retract": [{"belief": "B1", "reason": "asked", "from": ["N3"]}]}
    out, used = reconcile._statements(plan, ex)
    assert all(s["reason"] and s["evidence"]["records"] for s in out)       # the store would refuse anything less
    party = next(s for s in out if s.get("field") == "plan_party")
    assert party["source"] == "agent:vector" and party["reason"] == "from my own notes"   # his own note, no speaker
    leo = next(s for s in out if s.get("field") == "favourite_game")
    assert (leo["node"], leo["field"], leo["source"]) == ("home/person:leo", "favourite_game", "human:leo")
    assert leo["reason"] == "Leo said so" and leo["valid_from"] == "2026-11-24T16:05:00.000+00:00"
    assert leo["evidence"]["records"][0] == "msg:h1" and leo["origin"] == "thread:voice-1"
    notes = [s for s in out if s.get("store") == "dialogue"]
    assert len(notes) == 1                                                   # the unknown speaker heard in the room
    assert "didn't recognise" in notes[0]["claim"] and "Maya" in notes[0]["claim"]
    assert not any("forget" in s.get("claim", "").lower() for s in out)      # a forget request is never a belief
    assert [s["valid_from"] for s in out] == sorted(s["valid_from"] for s in out)
