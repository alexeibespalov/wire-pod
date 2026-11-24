"""Vector's agent: the shared ReAct loop (agent_core.loop) with his tools, prompt and guard."""
import re

from langchain_core.messages import AIMessage, AnyMessage

from . import config, guard, llm as llms, memory, turn
from agent_core import a2a, loop
from agent_core.loop import for_model  # noqa: F401  (tests and the server use it)
from .prompt import build_context, build_system_prompt
from .tools import TOOLS

llm = llms.chat(streaming=True, timeout=60, max_retries=1)   # Qwen or Claude: agent_core/llm.py

_SILENT = re.compile(r"^\W*SILENT\W*$", re.I)   # same as server._SILENT_WORD
_UNJOURNALED = {"recent_activity", "search_logs", "load_skill", "sense", "list_routines", "list_schedules",
                "list_maneuvers", "show_maneuver", "practice_status", "now_playing", "sleep_routine_status"}



# Always offered. Everything else arrives when its skill is loaded (skills/*/SKILL.md
# `tools:`), which keeps each model call small (~8k tokens of tool schemas -> ~2.5k) and
# gives the small model fewer, more relevant tools to choose from.
CORE_TOOLS = ["drive", "turn", "move_head", "move_lift", "do_action", "set_eye_color", "battery_status",
              "remember", "forget", "recall", "belief_history", "load_skill", "recent_activity", "search_logs", "stop_music",
              "now_playing", "list_schedules", "cancel_schedule", "start_practice", "practice_status",
              "stop_practice", "stop_program",
              "sleep_routine_status", "say_aloud"]
if config.PEER_NAME:
    CORE_TOOLS.append(f"ask_{config.PEER_NAME.lower()}")


def active_tool_names(messages: list[AnyMessage], available: dict, always: list[str]) -> list[str]:
    return loop.active_tool_names(messages, available, always, CORE_TOOLS)


def _tool_gate(tool: str, channel: str) -> str | None:
    if channel == "a2a" and tool in {"look", "show_last_view"} and not config.A2A_ALLOW_CAMERA:
        return "error: your camera isn't available to remote agents (privacy setting)"
    return None


def _after_turn(user_text: str, response: AIMessage, messages: list[AnyMessage], channel: str) -> list[AnyMessage]:
    if channel == "voice" and _SILENT.match(llms.text(response) or "") and not user_text.startswith("(No words"):
        # "not for me" (overheard): keep what he heard, tagged, so it's remembered as
        # overheard talk, not as something said to him
        return memory.tag_overheard_in(messages)
    return messages


def build_agent(checkpointer, extra_tools: list | None = None):
    return loop.build_agent(
        checkpointer, TOOLS, extra_tools, name="vector_agent", llm=llm,
        core_tools=CORE_TOOLS, unjournaled=_UNJOURNALED,
        build_system_prompt=build_system_prompt, build_context=build_context,
        channel=lambda: turn.channel, pre_tool=guard.pre_tool,
        tool_gate=_tool_gate, hidden=a2a.hidden_on_a2a, after_turn=_after_turn,
        tangled="Whoa, I got a bit tangled up there. Can you say that again?")
