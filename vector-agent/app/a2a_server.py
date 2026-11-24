"""Vector's A2A front door: other agents (Cortana, a phone assistant) can talk to him.

The server itself is shared (agent_core.a2a). Here: his agent card and how one A2A turn
runs on his one body: turns are serialised with voice turns, the reply goes back as text,
and the camera is off for remote agents unless A2A_ALLOW_CAMERA=1 (see app/agent.py).
"""
import asyncio
import logging

from fastapi import FastAPI

from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from . import config, turn
from agent_core import a2a
from .audio import PLAYER

log = logging.getLogger("vector-agent")


def agent_card() -> AgentCard:
    url = config.A2A_PUBLIC_URL
    skill = lambda id_, name, desc, ex: AgentSkill(id=id_, name=name, description=desc, tags=["vector", "robot"],
                                                   examples=ex, input_modes=["text"], output_modes=["text"])
    return AgentCard(
        name="Vector",
        description=("Vector, a small home robot with a personality (Anki Vector running WireOS + "
                     "wire-pod). Chat with him, ask questions (he can search the web), and have him "
                     "act at home: show pictures, emoji or drawings on his face, play animations, "
                     "move, or say something out loud. Knows who's in the room via his face recognition."),
        provider=AgentProvider(organization="Vector at home", url=url),
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[
            skill("chat", "Chat with Vector",
                  "Talk to Vector; he answers in character, remembers context per conversation "
                  "and long-term facts, and can search the web.",
                  ["How are you doing today?", "What's the weather tomorrow?"]),
            skill("act", "Do something at home",
                  "Vector acts on his real body at home: show a picture/emoji/drawing on his face, "
                  "play an animation, dance, move, change eye colour.",
                  ["Show a penguin on your face", "Draw a rocket", "Do a happy dance"]),
            skill("announce", "Say something out loud at home",
                  "Vector speaks a message aloud in the room in his own voice.",
                  ["Tell Alexei dinner is ready", "Say good morning to everyone"]),
            skill("memory", "What Vector remembers",
                  "Ask what Vector currently believes about someone in the family or the home, "
                  "from his own long-term memory.",
                  ["What do you remember about Leo's favourite game?"]),
            skill("presence", "Who's home",
                  "Who Vector can currently see, via his on-device face recognition.",
                  ["Can you see anyone right now?"]),
        ],
        supported_interfaces=a2a.interfaces(url),
    )


def build_app(get_agent) -> FastAPI:
    async def run_turn(text: str, thread: str) -> str:
        from . import sleep_routine
        sleep_routine.note_activity()
        try:
            async with turn.hold(config.TURN_LOCK_WAIT_S):
                PLAYER.turn_started()
                turn.reset(via="a2a")
                try:
                    async with asyncio.timeout(config.TURN_HARD_LIMIT_S):
                        return str(await get_agent().ainvoke(text, {"configurable": {"thread_id": thread}}) or "")
                except TimeoutError:
                    log.error("[%s] a2a turn cancelled after %.0fs", thread, config.TURN_HARD_LIMIT_S)
                    return "Sorry, that took too long and I gave up. Try again?"
                finally:
                    turn.reset()  # back to voice defaults
                    PLAYER.turn_ended(0.5)
        except turn.Busy:
            return "I'm busy with another conversation right now; try again in a moment."

    return a2a.build_app(agent_card(), run_turn)
