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
from langgraph.func import entrypoint, task
from langgraph.graph.message import add_messages

from . import config
from .prompt import build_system_prompt
from .tools import TOOLS, TOOLS_BY_NAME

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
llm_with_tools = llm.bind_tools(TOOLS)


def _trim(history: list[AnyMessage]) -> list[AnyMessage]:
    """Keep the tail of the conversation, starting on a human turn so we never hand
    the model an orphaned tool result."""
    if len(history) <= config.MAX_HISTORY_MESSAGES:
        return history
    tail = history[-config.MAX_HISTORY_MESSAGES:]
    for i, m in enumerate(tail):
        if isinstance(m, HumanMessage):
            return tail[i:]
    return []


@task
async def call_model(messages: list[AnyMessage]) -> AIMessage:
    return await llm_with_tools.ainvoke(messages)


@task
async def call_tool(tool_call: dict) -> ToolMessage:
    tool = TOOLS_BY_NAME.get(tool_call["name"])
    if tool is None:
        content = f"error: no tool called {tool_call['name']}"
    else:
        try:
            content = str(await tool.ainvoke(tool_call["args"]))
        except Exception as e:  # report to the model, don't crash the turn
            content = f"error: {e}"
    return ToolMessage(content=content, tool_call_id=tool_call["id"], name=tool_call["name"])


def build_agent(checkpointer):
    @entrypoint(checkpointer=checkpointer)
    async def vector_agent(user_text: str, *, previous: list[AnyMessage] | None = None):
        messages = add_messages(_trim(previous or []), [HumanMessage(user_text)])
        system = SystemMessage(build_system_prompt())

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

        return entrypoint.final(value=response.content, save=messages)

    return vector_agent
