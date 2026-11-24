"""Cortana's built-in tools (web search and Home Assistant arrive over MCP: app/mcp_tools.py)."""
from langchain_core.tools import tool

from . import beliefs, config, memory, turn
from agent_core import a2a
from .activity import ACTIVITY_TOOLS
from .music import MUSIC_TOOLS
from .reminders import REMINDER_TOOLS
from .skills import SKILL_TOOLS, load_skill


@tool
def remember(fact: str) -> str:
    """Save a lasting fact about someone in the family or the home to long-term memory,
    as a short third-person sentence that names who it's about (e.g. "Leo's swimming
    lesson is on Tuesdays", "The bins go out on Wednesday night"). Use when someone tells
    you something worth knowing on another day, or asks you to remember something. It goes
    into today's notes and becomes part of your long-term memory overnight."""
    return memory.add(fact)


@tool
def forget(about: str) -> str:
    """Let go of what you remember about this word or phrase, when someone asks you to
    forget something or a fact is wrong. Today's notes about it go at once; a long-term
    belief is dropped in your next nightly run."""
    return memory.forget(about)


_peer = a2a.peer_tool(lambda: turn.channel)                               # e.g. ask_vector
TOOLS = [remember, forget, *(beliefs.BELIEF_TOOLS if beliefs.ENABLED else []),   # recall, belief_history
         load_skill, *REMINDER_TOOLS, *ACTIVITY_TOOLS, *SKILL_TOOLS,
         *(MUSIC_TOOLS if config.MPD_HOST else []), *([_peer] if _peer is not None else [])]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
