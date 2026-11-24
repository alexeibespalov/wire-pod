"""agent-core on its own: settings forwarding, the A2A round trip, and that the reconcile
prompts speak about whichever agent is running. No network, no model.

Run:  cd agent-core && python -m pytest tests -q
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

import httpx
import pytest

from agent_core import settings

cfg = types.ModuleType("fake_agent_config")
os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp(prefix="agent-core-test-"))
settings.load_shared(cfg.__dict__, provider_default="anthropic", a2a_port=8096)
cfg.__dict__.update(
    ASSISTANT_NAME="Cortana", LOGGER_NAME="test-agent", ASSISTANT_DESC="the voice assistant of a family home",
    PRONOUNS=("she", "her", "her"), SELF_NODE="assistant:cortana", SEES_SPEAKERS=False, HEARS_OVERHEARD=False,
    SKILLS_DIR=Path(tempfile.mkdtemp()), SUMMARY_PROMPT="notes for {name}, at most {words} words",
    SKILL_EXAMPLES={"name": "n", "description": "d", "instructions": "i", "tools": "[]"},
    ACTIVITY_TEXT={"kinds": "k", "did": "d", "questions": "q", "log_words": "w"},
    A2A_TOKEN="secret", PEER_NAME="Vector", PEER_A2A_URL="http://peer", PEER_A2A_TOKEN="secret", PEER_TIMEOUT_S=5)
sys.modules[cfg.__name__] = cfg
settings.use(cfg)

from agent_core import a2a, beliefs, notes, reconcile  # noqa: E402


def test_settings_forward_to_the_registered_config(monkeypatch):
    assert settings.cfg.ASSISTANT_NAME == "Cortana" and settings.cfg.A2A_PORT == 8096
    monkeypatch.setattr(cfg, "TURN_BUDGET_S", 1.5)
    assert settings.cfg.TURN_BUDGET_S == 1.5                 # one set of values per process
    with pytest.raises(AttributeError):
        settings.cfg.TURN_BUDGET_S = 3


def _app(seen):
    from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

    async def run_turn(text, thread):
        seen.append((text, thread))
        return "Leo likes Minecraft {{playAnimationWI||happy}}"
    card = AgentCard(name="Vector", description="d", provider=AgentProvider(organization="o", url="http://peer"),
                     version="1", capabilities=AgentCapabilities(streaming=True, push_notifications=False),
                     default_input_modes=["text"], default_output_modes=["text"],
                     skills=[AgentSkill(id="chat", name="chat", description="d", tags=["t"], examples=["hi"],
                                        input_modes=["text"], output_modes=["text"])],
                     supported_interfaces=a2a.interfaces("http://peer"))
    return a2a.build_app(card, run_turn, token="secret")


def test_a2a_round_trip_and_token():
    seen = []
    t = httpx.ASGITransport(app=_app(seen))

    async def go():
        reply = await a2a.ask("http://peer", "secret", "what about Leo?", timeout=5, context_id="cortana-x", transport=t)
        with pytest.raises(PermissionError):
            await a2a.ask("http://peer", "wrong", "x", timeout=5, transport=t)
        return reply
    assert asyncio.run(go()) == "Leo likes Minecraft"        # the reply text, without body cues
    assert seen == [("what about Leo?", "a2a-cortana-x")]    # one thread per A2A context


def test_peer_tool_is_named_for_the_peer_and_never_used_on_an_a2a_turn(monkeypatch):
    channel = ["voice"]
    tool = a2a.peer_tool(lambda: channel[0])
    assert tool.name == "ask_vector" and "Vector" in tool.description
    assert a2a.hidden_on_a2a("a2a") == {"ask_vector"} and a2a.hidden_on_a2a("voice") == set()
    asked = []

    async def fake_ask(url, token, text, **kw):
        asked.append((url, token, text))
        return "Minecraft"
    monkeypatch.setattr(a2a, "ask", fake_ask)
    assert asyncio.run(tool.ainvoke({"question": "Leo's favourite game?"})) == "Vector says: Minecraft"
    assert asked[0][:2] == ("http://peer", "secret") and asked[0][2].startswith("This is Cortana")
    channel[0] = "a2a"
    assert asyncio.run(tool.ainvoke({"question": "again?"})).startswith("error")
    assert len(asked) == 1
    monkeypatch.setattr(cfg, "PEER_NAME", "")
    assert a2a.peer_tool(lambda: "voice") is None            # no peer configured: no tool


def test_reconcile_speaks_about_the_agent_that_runs_it(monkeypatch):
    w = reconcile._who()
    prompt = reconcile._EXTRACT.format(**w, today="t", fields="f", known="k", notes="n", transcript="x",
                                       speaker_rule=reconcile._SPEAKER_RULE.format(name=w["name"]), overheard_rule="")
    assert "Cortana, the voice assistant" in prompt and "assistant:cortana" in prompt
    assert "cannot see who is speaking" in prompt and "Vector" not in prompt.replace('"Vector says', "")
    assert (reconcile._source("cortana"), reconcile._source("Leo"), reconcile._source("vector")) \
        == ("agent:cortana", "human:leo", "agent:vector")      # what the other assistant said keeps its name
    assert beliefs.who("agent:cortana") == "you" and beliefs.who("agent:vector") == "Vector"
    assert reconcile._node("assistant:Cortana") == "home/assistant:cortana"


def test_notes_are_tagged_with_who_wrote_them(monkeypatch, tmp_path):
    monkeypatch.setattr(notes, "_PATH", tmp_path / "m.json")
    monkeypatch.setattr(beliefs, "ENABLED", True)
    notes.add("Bin day is Wednesday")
    assert notes._load()[0]["by"] == "cortana"
    assert "night run" in notes.forget("bin day")
    left = notes.all_facts()
    assert len(left) == 1 and left[0].startswith(notes.FORGET_PREFIX)     # never deleted: a request for the job
