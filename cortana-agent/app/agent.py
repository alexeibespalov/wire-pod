"""Cortana's agent: the shared ReAct loop (agent_core.loop) with her tools, prompt and guard."""
from langchain_core.messages import AnyMessage

from . import beliefs, config, guard, llm as llms, turn
from agent_core import a2a, loop
from agent_core.loop import for_model  # noqa: F401
from .prompt import build_context, build_system_prompt
from .tools import TOOLS

llm = llms.chat(streaming=True, timeout=60, max_retries=1)   # Claude (or Qwen): agent_core/llm.py

_UNJOURNALED = {"recall", "belief_history",      # these journal themselves, in more detail (kind "memory")
                "recent_activity", "search_logs", "load_skill", "list_timers",
                "set_timer", "set_reminder", "cancel_timer", "now_playing"}   # (reminders journal themselves)



# Always offered. Everything else arrives when its skill is loaded (skills/*/SKILL.md
# `tools:`), which keeps each model call small and gives the model fewer, more relevant tools.
CORE_TOOLS = ["remember", "forget", "load_skill", "recent_activity", "search_logs",
              "set_timer", "set_reminder", "list_timers", "cancel_timer"]
if beliefs.ENABLED:
    CORE_TOOLS += ["recall", "belief_history"]
if config.PEER_NAME:
    CORE_TOOLS.append(f"ask_{config.PEER_NAME.lower()}")


def active_tool_names(messages: list[AnyMessage], available: dict, always: list[str]) -> list[str]:
    return loop.active_tool_names(messages, available, always, CORE_TOOLS)


def build_agent(checkpointer, extra_tools: list | None = None):
    return loop.build_agent(
        checkpointer, TOOLS, extra_tools, name="cortana_agent", llm=llm,
        core_tools=CORE_TOOLS, unjournaled=_UNJOURNALED,
        build_system_prompt=build_system_prompt, build_context=build_context,
        channel=lambda: turn.channel, pre_tool=guard.pre_tool, hidden=a2a.hidden_on_a2a)
