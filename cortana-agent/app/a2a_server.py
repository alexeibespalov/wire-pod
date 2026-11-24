"""Cortana's A2A front door: other agents (Vector, a phone assistant) can talk to her.

The server itself is shared (agent_core.a2a). Here: her agent card and how one A2A turn
runs: it takes the same turn lock as voice, has its own conversation thread per A2A
context, and the reply goes back as text (nothing is spoken on the satellite).
"""
import asyncio
import logging

from fastapi import FastAPI

from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from . import activity, config, turn
from agent_core import a2a
from .conversation import STATE

log = logging.getLogger("cortana-agent")


def agent_card() -> AgentCard:
    url, name = config.A2A_PUBLIC_URL, config.ASSISTANT_NAME
    skill = lambda id_, title, desc, ex: AgentSkill(id=id_, name=title, description=desc,   # noqa: E731
                                                    tags=["cortana", "home"], examples=ex,
                                                    input_modes=["text"], output_modes=["text"])
    return AgentCard(
        name=name,
        description=(f"{name}, the family's voice assistant at home (a speaker in the hall). Ask her "
                     "questions, and ask what she remembers about the family and the home."),
        provider=AgentProvider(organization=f"{name} at home", url=url),
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[
            skill("chat", f"Chat with {name}", "Ask a question; she answers briefly and can search the web.",
                  ["What's the weather tomorrow?"]),
            skill("memory", f"What {name} remembers",
                  "Ask what she currently believes about someone in the family or the home, from her "
                  "own long-term memory.", ["What do you remember about bin day?"]),
        ],
        supported_interfaces=a2a.interfaces(url),
    )


def build_app() -> FastAPI:
    async def run_turn(text: str, thread: str) -> str:
        try:
            async with turn.hold(config.TURN_LOCK_WAIT_S):
                turn.channel = "a2a"
                try:
                    async with asyncio.timeout(config.TURN_HARD_LIMIT_S):
                        reply = str(await STATE["agent"].ainvoke(text, {"configurable": {"thread_id": thread}}) or "")
                    activity.record("conversation", f"another agent asked: \"{text[:120]}\" -> I said: \"{reply[:160]}\"",
                                    channel="a2a")
                    return reply
                except TimeoutError:
                    log.error("[%s] a2a turn cancelled after %.0fs", thread, config.TURN_HARD_LIMIT_S)
                    return "Sorry, that took too long and I gave up. Try again?"
                finally:
                    turn.channel = "voice"
        except turn.Busy:
            return "I'm busy with another conversation right now; try again in a moment."

    return a2a.build_app(agent_card(), run_turn)
