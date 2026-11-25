"""The ReAct loop, built on LangGraph's Functional API.

    @entrypoint (checkpointed per conversation thread)
        loop: @task call_model  -> if no tool calls: done
              @task call_tool x N (concurrently)  -> back to call_model

`previous` (the saved message history for this thread) is LangGraph short-term
memory; `entrypoint.final(save=...)` stores the updated history.

One loop for every agent. What differs per agent is passed to build_agent(): its tools,
which of them are always offered, its prompt builders, its safety guard, and a few hooks.
"""
import asyncio
import json
import re
import time
from typing import Awaitable, Callable

from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, SystemMessage,
                                     ToolMessage)
from langgraph.config import get_config
from langgraph.func import entrypoint, task
from langgraph.graph.message import add_messages

from .settings import cfg as config
from . import activity, compaction, dash, llm as llms, skills

MAX_TOOL_RESULT_CHARS = 6000  # keep web results from bloating a small model's context


def _brief(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."


def _request_text(messages: list[AnyMessage]) -> str:
    """The latest things people said (newest first): context for the safety guard."""
    said = [llms.text(m) for m in reversed(messages) if isinstance(m, HumanMessage)]
    return "\n---\n".join(said[:2])


_LOADED = re.compile(r"^SKILL (\S+) loaded")
_CACHE = {"cache_control": {"type": "ephemeral"}}


def _loaded_skills(messages: list[AnyMessage]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "load_skill":
            if (mm := _LOADED.match(str(m.content))) and mm.group(1) not in out:
                out.append(mm.group(1))
    return out


def active_tool_names(messages: list[AnyMessage], available: dict, always: list[str],
                      core_tools: list[str]) -> list[str]:
    """Core + always-on extras (web search) + tools of skills loaded in this conversation,
    in a stable order (core first, then skills in the order they were loaded). Everything
    else arrives when its skill is loaded (skills/*/SKILL.md `tools:`), which keeps each
    model call small and gives a small model fewer, more relevant tools to choose from."""
    names = [n for n in core_tools if n in available] + [n for n in always if n not in core_tools]
    for n in skills.tools_for(_loaded_skills(messages)):
        if n.endswith("*"):                # e.g. home_*: every tool with that prefix
            for m in sorted(available):
                if m.startswith(n[:-1]) and m not in names:
                    names.append(m)
        elif n in available and n not in names:
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


def build_agent(checkpointer, tools: list, extra_tools: list | None = None, *,
                name: str,
                llm,
                core_tools: list[str],
                unjournaled: set[str],
                build_system_prompt: Callable[[list[str], str], str],
                build_context: Callable[[], str],
                channel: Callable[[], str],
                pre_tool: Callable[[str, dict, str, str], Awaitable[str | None]],
                tool_gate: Callable[[str, str], str | None] | None = None,
                hidden: Callable[[str], set[str]] | None = None,
                after_turn: Callable[[str, AIMessage, list[AnyMessage], str], list[AnyMessage]] | None = None,
                tangled: str = "Sorry, I got a bit tangled up there. Could you ask me again?",
                slow: str = "That's taking me a while. Give me a second and ask me again?"):
    """The agent as a LangGraph entrypoint called `name` (front ends look for that node name).

    tools / extra_tools  the agent's own tools, and the ones that arrived over MCP
    core_tools           names always offered; the rest come with a loaded skill
    unjournaled          tool names not worth a line in the activity journal
    channel()            where this turn came from: "voice", "text", "a2a", ...
    pre_tool(...)        the safety guard: text to return instead of running the tool, or None
    tool_gate(tool, ch)  optional extra refusal by tool name and channel (e.g. no camera over A2A)
    hidden(ch)           optional: tool names not offered at all on this channel
    after_turn(...)      optional: (user_text, response, messages, channel) -> messages to save
    """
    tools = [*tools, *(extra_tools or [])]
    tools_by_name = {t.name: t for t in tools}
    # web search: always on; Home Assistant (home_*): only with the 'home' skill
    always = [t.name for t in (extra_tools or []) if not t.name.startswith("home_")]
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
        ch = channel()
        blocked = None
        if tool is None:
            content = f"error: no tool called {tool_call['name']}"
        elif allowed is not None and tool.name not in allowed:
            owner = [n for n, sk in skills.SKILLS.items() if tool.name in sk.get("tools", [])]
            content = (f"error: {tool.name} isn't available yet: load the "
                       f"{' or '.join(repr(o) for o in owner[:3]) or 'matching'} skill first")
        elif tool_gate is not None and (refused := tool_gate(tool.name, ch)):
            content = refused
        elif blocked := await pre_tool(tool.name, tool_call["args"], request, ch):
            content = blocked            # safety laws: pre-hook said no
        else:
            try:
                content = _as_text(await tool.ainvoke(tool_call["args"]))
            except Exception as e:  # report to the model, don't crash the turn
                content = f"error: {e}"
        if tool is not None and tool.name not in unjournaled and not blocked:
            activity.record("tool", f"{tool.name}({_brief(tool_call['args'])}) -> {content[:140]}",
                            channel=ch)
        return ToolMessage(content=content, tool_call_id=tool_call["id"], name=tool_call["name"])

    async def agent(user_text: str, *, previous: list[AnyMessage] | None = None):
        thread = get_config()["configurable"]["thread_id"]
        ch = channel()
        summary, history = compaction.prepare(thread, previous or [])
        messages = add_messages(history, [HumanMessage(user_text)])
        system = SystemMessage(build_system_prompt(tool_names, summary))
        in_place = config.ANTHROPIC     # Claude: live context is a saved system message (see for_model)
        if in_place:
            messages = add_messages(messages, [SystemMessage(build_context())])
        hide = hidden(ch) if hidden is not None else set()

        dash.emit("think")
        response = None
        t_start = time.monotonic()
        for _ in range(config.MAX_AGENT_STEPS):
            names = [n for n in active_tool_names(messages, tools_by_name, always, core_tools) if n not in hide]
            last = names
            ctx_block = "" if in_place else build_context()
            if time.monotonic() - t_start > config.TURN_BUDGET_S and ch == "voice":
                # nobody waits longer than this for a spoken reply: wrap up now
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
                response = AIMessage(content=llms.text(response).strip() or slow)
            messages = add_messages(messages, [response])
            if not response.tool_calls:
                break
            request = _request_text(messages)
            for tc in response.tool_calls:
                if tc["name"] == "load_skill":
                    dash.emit("skill", (tc.get("args") or {}).get("name", ""))
                else:
                    dash.emit("tool", tc["name"])
            results = await asyncio.gather(*(call_tool(tc, request, names) for tc in response.tool_calls))
            messages = add_messages(messages, list(results))
            dash.emit("think")
        else:
            # step budget exhausted mid-tool-loop: close the turn with plain text
            response = AIMessage(content=tangled)
            messages = add_messages(messages, [response])

        dash.emit("done")
        if after_turn is not None:
            messages = after_turn(user_text, response, messages, ch)
        compaction.maybe_compact(thread, messages, llm)   # background, after the turn
        return entrypoint.final(value=llms.text(response), save=messages)

    agent.__name__ = agent.__qualname__ = name
    return entrypoint(checkpointer=checkpointer)(agent)
