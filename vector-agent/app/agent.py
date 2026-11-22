"""The ReAct loop, built on LangGraph's Functional API.

    @entrypoint (checkpointed per conversation thread)
        loop: @task call_model  -> if no tool calls: done
              @task call_tool x N (concurrently)  -> back to call_model

`previous` (the saved message history for this thread) is LangGraph short-term
memory; `entrypoint.final(save=...)` stores the updated history.
"""
import asyncio
import json
import re
import time

from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, SystemMessage,
                                     ToolMessage)
from langgraph.config import get_config
from langgraph.func import entrypoint, task
from langgraph.graph.message import add_messages

from . import activity, compaction, config, guard, llm as llms, skills, turn
from .prompt import build_context, build_system_prompt
from .tools import TOOLS

MAX_TOOL_RESULT_CHARS = 6000  # keep web results from bloating a small model's context

llm = llms.chat(streaming=True, timeout=60, max_retries=1)   # Qwen or Claude: app/llm.py


_UNJOURNALED = {"recent_activity", "search_logs", "load_skill", "sense", "list_routines", "list_schedules",
                "list_maneuvers", "show_maneuver", "practice_status", "now_playing", "sleep_routine_status"}


def _brief(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."


def _request_text(messages: list[AnyMessage]) -> str:
    """The latest things people said (newest first): context for the safety guard."""
    said = [llms.text(m) for m in reversed(messages) if isinstance(m, HumanMessage)]
    return "\n---\n".join(said[:2])


# Always offered. Everything else arrives when its skill is loaded (skills/*/SKILL.md
# `tools:`), which keeps each model call small (~8k tokens of tool schemas -> ~2.5k) and
# gives the small model fewer, more relevant tools to choose from.
CORE_TOOLS = ["drive", "turn", "move_head", "move_lift", "do_action", "set_eye_color", "battery_status",
              "remember", "forget", "load_skill", "recent_activity", "search_logs", "stop_music",
              "now_playing", "list_schedules", "cancel_schedule", "start_practice", "practice_status",
              "stop_practice", "stop_program",
              "sleep_routine_status", "say_aloud"]
_LOADED = re.compile(r"^SKILL (\S+) loaded")
_CACHE = {"cache_control": {"type": "ephemeral"}}


def _loaded_skills(messages: list[AnyMessage]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "load_skill":
            if (mm := _LOADED.match(str(m.content))) and mm.group(1) not in out:
                out.append(mm.group(1))
    return out


def active_tool_names(messages: list[AnyMessage], available: dict, always: list[str]) -> list[str]:
    """Core + always-on extras (web search) + tools of skills loaded in this conversation,
    in a stable order (core first, then skills in the order they were loaded)."""
    names = [n for n in CORE_TOOLS if n in available] + [n for n in always if n not in CORE_TOOLS]
    for n in skills.tools_for(_loaded_skills(messages)):
        if n in available and n not in names:
            names.append(n)
    return names


def _cached(m: AnyMessage) -> AnyMessage:
    """Copy of m with a prompt-cache breakpoint: the prefix up to here is cached for ~5 min
    and re-used by the next call that starts the same way (Alibaba explicit cache and
    Anthropic prompt caching take the same cache_control marker)."""
    return m.model_copy(update={"content": [{"type": "text", "text": m.content, **_CACHE}]})


def for_model(system: SystemMessage, messages: list[AnyMessage], context: str = "") -> list[AnyMessage]:
    """What the model sees: [system (cached)] + history (breakpoint at its end) + this turn
    (breakpoint at its latest point, so each tool-loop step re-uses the previous one).

    Claude (context == ""): the live RIGHT NOW block is already in `messages` as a SAVED
    system message right after the newest human message, so nothing earlier is ever edited:
    the history only grows, which keeps the prompt cache and earlier thinking blocks valid.
    Qwen (context given): it goes in front of the newest human message and isn't saved; any
    saved mid-history system messages (from a Claude period) are left out."""
    msgs = list(messages)
    if context:
        msgs = [m for m in msgs if not isinstance(m, SystemMessage)]
    cur = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=None)
    if context and cur is not None:
        msgs[cur] = msgs[cur].model_copy(update={"content": f"{context}\n{msgs[cur].content}"})

    def mark(i):
        if 0 <= i < len(msgs) and isinstance(msgs[i].content, str) and msgs[i].content.strip() \
                and not isinstance(msgs[i], ToolMessage):
            msgs[i] = _cached(msgs[i])
            return True
        return False

    if cur is not None:           # end of the stored history, before the newest message
        for i in range(cur - 1, max(cur - 6, -1), -1):
            if mark(i):
                break
        for i in range(len(msgs) - 1, cur - 1, -1):   # latest point of this turn
            if mark(i):
                break
    return [_cached(system), *msgs]


def _as_text(result) -> str:
    """Tool results may be str, a list of MCP content blocks, or (content, artifact)."""
    if isinstance(result, tuple) and len(result) == 2:
        result = result[0]
    if isinstance(result, list):
        parts = []
        for block in result:
            if isinstance(block, dict):
                parts.append(block.get("text") or "")
            else:
                parts.append(getattr(block, "text", None) or str(block))
        result = "\n".join(p for p in parts if p)
    text = str(result)
    return text if len(text) <= MAX_TOOL_RESULT_CHARS else text[:MAX_TOOL_RESULT_CHARS] + " …[truncated]"


def build_agent(checkpointer, extra_tools: list | None = None):
    tools = [*TOOLS, *(extra_tools or [])]
    tools_by_name = {t.name: t for t in tools}
    always = [t.name for t in (extra_tools or [])]          # web search: always on
    tool_names = sorted(tools_by_name)
    _bound: dict[tuple, object] = {}

    def bound(names: list[str], last: tuple = ()):
        if not names:                          # time's up: answer without tools
            key = ("<none>", *last)
            if key not in _bound:
                _bound[key] = llms.no_tools(llm, [tools_by_name[n] for n in last])
            return _bound[key]
        key = tuple(names)
        if key not in _bound:
            _bound[key] = llm.bind_tools([tools_by_name[n] for n in names])
        return _bound[key]

    @task
    async def call_model(messages: list[AnyMessage], names: list[str], last: list[str] | None = None) -> AIMessage:
        return await bound(names, tuple(last or ())).ainvoke(messages)

    @task
    async def call_tool(tool_call: dict, request: str = "", allowed: list[str] | None = None) -> ToolMessage:
        tool = tools_by_name.get(tool_call["name"])
        blocked = None
        if tool is None:
            content = f"error: no tool called {tool_call['name']}"
        elif allowed is not None and tool.name not in allowed:
            owner = [n for n, sk in skills.SKILLS.items() if tool.name in sk.get("tools", [])]
            content = (f"error: {tool.name} isn't available yet: load the "
                       f"{' or '.join(repr(o) for o in owner[:3]) or 'matching'} skill first")
        elif (turn.channel == "a2a" and tool.name in {"look", "show_last_view"}
              and not config.A2A_ALLOW_CAMERA):
            content = "error: your camera isn't available to remote agents (privacy setting)"
        elif blocked := await guard.pre_tool(tool.name, tool_call["args"], request, turn.channel):
            content = blocked            # safety laws: pre-hook said no
        else:
            try:
                content = _as_text(await tool.ainvoke(tool_call["args"]))
            except Exception as e:  # report to the model, don't crash the turn
                content = f"error: {e}"
        if tool is not None and tool.name not in _UNJOURNALED and not blocked:
            activity.record("tool", f"{tool.name}({_brief(tool_call['args'])}) -> {content[:140]}",
                            channel=turn.channel)
        return ToolMessage(content=content, tool_call_id=tool_call["id"], name=tool_call["name"])

    @entrypoint(checkpointer=checkpointer)
    async def vector_agent(user_text: str, *, previous: list[AnyMessage] | None = None):
        thread = get_config()["configurable"]["thread_id"]
        summary, history = compaction.prepare(thread, previous or [])
        messages = add_messages(history, [HumanMessage(user_text)])
        system = SystemMessage(build_system_prompt(tool_names, summary))
        in_place = config.ANTHROPIC     # Claude: live context is a saved system message (see for_model)
        if in_place:
            messages = add_messages(messages, [SystemMessage(build_context())])

        response = None
        t_start = time.monotonic()
        for _ in range(config.MAX_AGENT_STEPS):
            names = active_tool_names(messages, tools_by_name, always)
            last = names
            ctx_block = "" if in_place else build_context()
            if time.monotonic() - t_start > config.TURN_BUDGET_S and turn.channel == "voice":
                # Vector gives up on a reply after ~25 s (cloud-with-X face): wrap up now
                names = []
                hurry = ("(You've taken a while: answer NOW in one or two sentences with "
                         "what you have; no more tools this turn.)")
                if in_place:            # appended after the tool results: still append-only
                    messages = add_messages(messages, [SystemMessage(hurry)])
                else:
                    ctx_block += "\n" + hurry
            response = await call_model(for_model(system, messages, ctx_block), names, last)
            if not names and response.tool_calls:
                # time's up but the model still wants tools: don't run them, just talk
                response = AIMessage(content=llms.text(response).strip() or
                                     "That's taking me a while. Give me a second and ask me again?")
            messages = add_messages(messages, [response])
            if not response.tool_calls:
                break
            request = _request_text(messages)
            results = await asyncio.gather(*(call_tool(tc, request, names) for tc in response.tool_calls))
            messages = add_messages(messages, list(results))
        else:
            # step budget exhausted mid-tool-loop: close the turn with plain text
            response = AIMessage(content="Whoa, I got a bit tangled up there. Can you say that again?")
            messages = add_messages(messages, [response])

        compaction.maybe_compact(thread, messages, llm)   # background, after the turn
        return entrypoint.final(value=llms.text(response), save=messages)

    return vector_agent
