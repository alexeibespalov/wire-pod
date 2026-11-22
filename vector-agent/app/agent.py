"""The ReAct loop, built on LangGraph's Functional API.

    @entrypoint (checkpointed per conversation thread)
        loop: @task call_model  -> if no tool calls: done
              @task call_tool x N (concurrently)  -> back to call_model

`previous` (the saved message history for this thread) is LangGraph short-term
memory; `entrypoint.final(save=...)` stores the updated history.
"""
import asyncio

from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, SystemMessage,
                                     ToolMessage)
from langchain_openai import ChatOpenAI
from langgraph.config import get_config
from langgraph.func import entrypoint, task
from langgraph.graph.message import add_messages

from . import compaction, config, turn
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
    async def call_tool(tool_call: dict) -> ToolMessage:
        tool = tools_by_name.get(tool_call["name"])
        if tool is None:
            content = f"error: no tool called {tool_call['name']}"
        elif (turn.channel == "a2a" and tool.name in {"look", "show_last_view"}
              and not config.A2A_ALLOW_CAMERA):
            content = "error: your camera isn't available to remote agents (privacy setting)"
        else:
            try:
                content = _as_text(await tool.ainvoke(tool_call["args"]))
            except Exception as e:  # report to the model, don't crash the turn
                content = f"error: {e}"
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
            results = await asyncio.gather(*(call_tool(tc) for tc in response.tool_calls))
            messages = add_messages(messages, list(results))
        else:
            # step budget exhausted mid-tool-loop: close the turn with plain text
            response = AIMessage(content="Whoa, I got a bit tangled up there. Can you say that again?")
            messages = add_messages(messages, [response])

        compaction.maybe_compact(thread, messages, llm)   # background, after the turn
        return entrypoint.final(value=response.content, save=messages)

    return vector_agent
