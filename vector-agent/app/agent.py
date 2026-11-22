"""The ReAct loop, built on LangGraph's Functional API.

    @entrypoint (checkpointed per conversation thread)
        loop: @task call_model  -> if no tool calls: done
              @task call_tool x N (concurrently)  -> back to call_model

`previous` (the saved message history for this thread) is LangGraph short-term
memory; `entrypoint.final(save=...)` stores the updated history.
"""
import asyncio
import json

from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, SystemMessage,
                                     ToolMessage)
from langchain_openai import ChatOpenAI
from langgraph.config import get_config
from langgraph.func import entrypoint, task
from langgraph.graph.message import add_messages

from . import activity, compaction, config, guard, turn
from .prompt import build_system_prompt
from .tools import TOOLS

MAX_TOOL_RESULT_CHARS = 6000  # keep web results from bloating a small model's context

llm = ChatOpenAI(
    model=config.LLM_MODEL,
    base_url=config.LLM_BASE_URL,
    api_key=config.LLM_API_KEY,
    temperature=config.LLM_TEMPERATURE,
    extra_body=config.LLM_EXTRA_BODY,
    streaming=True,
    timeout=60,
    max_retries=1,
)


_UNJOURNALED = {"recent_activity", "search_logs", "load_skill", "sense", "list_routines", "list_schedules",
                "list_maneuvers", "show_maneuver", "practice_status", "now_playing", "sleep_routine_status"}


def _brief(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."


def _request_text(messages: list[AnyMessage]) -> str:
    """The latest things people said (newest first): context for the safety guard."""
    said = [str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage)]
    return "\n---\n".join(said[:2])


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
    llm_with_tools = llm.bind_tools(tools)
    tool_names = sorted(tools_by_name)

    @task
    async def call_model(messages: list[AnyMessage]) -> AIMessage:
        return await llm_with_tools.ainvoke(messages)

    @task
    async def call_tool(tool_call: dict, request: str = "") -> ToolMessage:
        tool = tools_by_name.get(tool_call["name"])
        blocked = None
        if tool is None:
            content = f"error: no tool called {tool_call['name']}"
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

        response = None
        for _ in range(config.MAX_AGENT_STEPS):
            response = await call_model([system, *messages])
            messages = add_messages(messages, [response])
            if not response.tool_calls:
                break
            request = _request_text(messages)
            results = await asyncio.gather(*(call_tool(tc, request) for tc in response.tool_calls))
            messages = add_messages(messages, list(results))
        else:
            # step budget exhausted mid-tool-loop: close the turn with plain text
            response = AIMessage(content="Whoa, I got a bit tangled up there. Can you say that again?")
            messages = add_messages(messages, [response])

        compaction.maybe_compact(thread, messages, llm)   # background, after the turn
        return entrypoint.final(value=response.content, save=messages)

    return vector_agent
