"""One place that builds chat models, so the rest of the agent doesn't care which provider
runs Vector's brain (config.LLM_PROVIDER):

* openai    - ChatOpenAI against any OpenAI-compatible endpoint (Qwen on Alibaba Model Studio)
* anthropic - ChatAnthropic (langchain-anthropic, on the official anthropic SDK)

Claude 5.x specifics handled here:
* Thinking is always on; `output_config.effort` is the speed dial (config.LLM_EFFORT).
  Sampling params (temperature) are rejected, so they're only sent to the openai provider.
* Thinking blocks are bound to the exact conversation that produced them. The agent edits
  its history every turn (the live [RIGHT NOW] block on the newest message, a system prompt
  that changes as skills and memories change, tools that appear when a skill loads,
  compaction), so every request sets prefix_mismatch_behavior="drop_block": stale thinking
  blocks are dropped (unbilled) instead of the request failing with a 400.
* Refusals: server-side fallbacks ("default") retry on the model Anthropic recommends.
* No OpenAI JSON mode: JSON callers go through json_model() / parse_json(), which ask by
  prompt (every JSON prompt here already says "Reply as JSON") and parse tolerantly.
* Message content is a list of blocks (thinking, text, tool_use), not a string: use text().
"""
import json
import re

from . import config

_BETA_BINDING = "thinking-binding-controls-2026-08-01"
_BETA_FALLBACK = "server-side-fallback-2026-07-01"
_MIN_CLAUDE_TOKENS = 4096   # thinking counts toward max_tokens: small caps would cut replies


def chat(*, temperature: float | None = None, timeout: float = 60, max_retries: int = 1,
         max_tokens: int | None = None, streaming: bool = False, model: str | None = None,
         background: bool = False):
    """A chat model for this provider. `temperature` is ignored for Claude; `background`
    picks the background effort (sleep jobs, practice) instead of the voice one."""
    if config.ANTHROPIC:
        from langchain_anthropic import ChatAnthropic
        betas = [_BETA_BINDING]
        extra = {}
        if config.LLM_FALLBACKS:
            betas.append(_BETA_FALLBACK)
            extra["fallbacks"] = config.LLM_FALLBACKS
        return ChatAnthropic(
            model=model or config.LLM_MODEL,
            api_key=config.ANTHROPIC_API_KEY,
            max_tokens=max(max_tokens or 0, _MIN_CLAUDE_TOKENS),
            timeout=timeout,
            max_retries=max_retries,
            streaming=streaming,
            thinking={"type": "adaptive",
                      "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            output_config={"effort": config.LLM_BACKGROUND_EFFORT if background else config.LLM_EFFORT},
            betas=betas,
            model_kwargs=extra,
        )
    from langchain_openai import ChatOpenAI
    kw = {"max_tokens": max_tokens} if max_tokens else {}
    return ChatOpenAI(model=model or config.LLM_MODEL, base_url=config.LLM_BASE_URL,
                      api_key=config.LLM_API_KEY,
                      temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
                      extra_body=config.LLM_EXTRA_BODY, timeout=timeout, max_retries=max_retries,
                      streaming=streaming, **kw)


def no_tools(model, tools: list):
    """The model for a final answer WITHOUT tool calls. Claude rejects a request whose
    history has tool blocks but declares no tools, so there we keep the tools and forbid
    calling them; OpenAI-style endpoints just get no tools."""
    if config.ANTHROPIC and tools:
        return model.bind_tools(tools, tool_choice={"type": "none"})
    return model


def text(msg_or_content) -> str:
    """The plain text of a message or its content: a str (OpenAI-style) or a list of blocks
    (Claude: thinking / text / tool_use); only text blocks count."""
    c = getattr(msg_or_content, "content", msg_or_content)
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    parts = []
    for b in c:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text") or "")
    return "".join(parts)


def json_model(model):
    """The model set up to answer in JSON: OpenAI JSON mode where it exists; Claude is asked
    by the prompt itself (and parse_json copes with fences or a sentence around it)."""
    if config.ANTHROPIC:
        return model
    return model.bind(response_format={"type": "json_object"})


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_json(s: str):
    """Parse a JSON object from model text: plain, fenced, or with words around it."""
    s = _FENCE.sub("", (s or "").strip()).strip()
    try:
        return json.loads(s)
    except ValueError:
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            return json.loads(s[i:j + 1])
        raise


async def ainvoke_json(model, messages):
    """Ask for a JSON object and return it parsed (raises ValueError if it isn't JSON)."""
    res = await json_model(model).ainvoke(messages)
    return parse_json(text(res))
