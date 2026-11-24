"""Settings shared by every agent, and the handle core modules read them through.

`load_shared(ns, ...)` fills an agent's config namespace with the settings both agents
have (LLM, web/home tools, history, turn limits, sleep model, belief store, A2A).
`use(module)` registers that config module; `cfg.X` then reads `module.X`.

Identity settings each agent must define itself (no defaults on purpose):
    ASSISTANT_NAME   "Vector"                 LOGGER_NAME    "vector-agent"
    ASSISTANT_DESC   "a small home robot who lives with a family (grown-ups and kids)"
    PRONOUNS         ("he", "him", "his")     SELF_NODE      "robot:vector"
    SEES_SPEAKERS    True (face recognition)  HEARS_OVERHEARD True (wake word misfires)
    SKILLS_DIR       Path to the built-in skills folder
    SUMMARY_PROMPT   the rolling-notes prompt ({words}, optional {name})
    SKILL_EXAMPLES   {"name", "description", "instructions", "tools"} for create_skill
"""
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


class _Cfg:
    """Attribute access forwarded to the registered config module."""
    _mod = None

    def __getattr__(self, name):
        mod = object.__getattribute__(self, "_mod")
        if mod is None:
            raise RuntimeError("agent_core.settings.use(<config module>) has not been called: "
                               "import the agent's config before any agent_core module")
        return getattr(mod, name)

    def __setattr__(self, name, value):
        raise AttributeError("settings are read-only here: change the agent's config module")


cfg = _Cfg()


def use(module) -> None:
    object.__setattr__(cfg, "_mod", module)


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"missing required env var {name}")
    return val


def load_shared(ns: dict, *, provider_default: str = "anthropic", a2a_port: int = 8095,
                listen_port: int = 8091) -> None:
    """Define the shared settings in `ns` (an agent's config module globals)."""
    e = env
    # --- LLM. Two providers (see agent_core/llm.py):
    #   openai    = any OpenAI-compatible endpoint (Alibaba Model Studio / Qwen)
    #   anthropic = Claude through the Anthropic API
    provider = e("LLM_PROVIDER", provider_default).strip().lower()
    anthropic = provider == "anthropic"
    model = e("FORCE_MODEL", "claude-haiku-5-5" if anthropic else "qwen3.6-flash")
    ns.update(
        LLM_PROVIDER=provider, ANTHROPIC=anthropic,
        LLM_BASE_URL=e("UPSTREAM_BASE", ""), LLM_API_KEY=e("UPSTREAM_KEY", ""),
        ANTHROPIC_API_KEY=e("ANTHROPIC_API_KEY", ""), LLM_MODEL=model,
        # Qwen thinks by default (~5x slower first token); we turn it off.
        LLM_EXTRA_BODY=json.loads(e("LLM_EXTRA_BODY", '{"enable_thinking": false}')),
        LLM_TEMPERATURE=float(e("LLM_TEMPERATURE", "0.8")),   # openai provider only (Claude 5.x rejects it)
        # Claude: thinking is always on; effort is the speed/quality dial. Voice = low.
        LLM_EFFORT=e("LLM_EFFORT", "low"), LLM_BACKGROUND_EFFORT=e("LLM_BACKGROUND_EFFORT", "medium"),
        # Server-side refusal fallbacks ("default"); empty = off (Haiku has none).
        LLM_FALLBACKS=e("LLM_FALLBACKS", "" if "haiku" in model else "default"),
        # Background thinking on the Message Batches API (Claude only): half price, results
        # within about an hour, so a job submits in one session and collects in a later one.
        SLEEP_MODEL=e("SLEEP_MODEL", "claude-sonnet-5-5"),
        SLEEP_BATCH=e("SLEEP_BATCH", "1") == "1" and anthropic,
        SLEEP_BATCH_MAX_AGE_H=float(e("SLEEP_BATCH_MAX_AGE_H", "26")),
    )
    # --- Web search (Tavily remote MCP) and Home Assistant's MCP server. Optional.
    ns.update(
        TAVILY_API_KEY=os.environ.get("TAVILY_API_KEY", "").strip(),
        TAVILY_MCP_URL=e("TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/"),
        TAVILY_TOOLS=[t.strip() for t in e("TAVILY_TOOLS", "tavily_search,tavily_extract").split(",") if t.strip()],
        TAVILY_DEFAULTS=e("TAVILY_DEFAULTS", '{"search_depth": "basic", "max_results": 5}'),
        HA_TOKEN=os.environ.get("HA_TOKEN", "").strip(),
        HA_MCP_URL=e("HA_MCP_URL", "http://127.0.0.1:8123/api/mcp"),
    )
    # --- Conversation
    ns.update(
        DATA_DIR=Path(e("AGENT_DATA_DIR", "/data")),
        THREAD_IDLE_MINUTES=float(e("THREAD_IDLE_MINUTES", "0")),   # 0 = one continuous conversation
        # Past HISTORY_TOKENS the oldest part is folded into a rolling summary, leaving KEEP_TOKENS.
        HISTORY_TOKENS=int(e("HISTORY_TOKENS", "16000")), KEEP_TOKENS=int(e("KEEP_TOKENS", "8000")),
        SUMMARY_WORDS=int(e("SUMMARY_WORDS", "400")),
        MAX_AGENT_STEPS=int(e("MAX_AGENT_STEPS", "6")),
        TURN_BUDGET_S=float(e("TURN_BUDGET_S", "14")),          # past this: answer with what you have
        TURN_HARD_LIMIT_S=float(e("TURN_HARD_LIMIT_S", "60")),  # a turn is cancelled after this
        TURN_LOCK_WAIT_S=float(e("TURN_LOCK_WAIT_S", "20")),
        TIMEZONE=ZoneInfo(e("AGENT_TZ", "Europe/London")),
        HOME_LOCATION=os.environ.get("AGENT_LOCATION", "").strip(),
    )
    # --- Long-term memory: the belief store on the NUC (beliefs-api). Unset = notes only.
    ns.update(
        BELIEFS_URL=os.environ.get("BELIEFS_URL", "").strip().rstrip("/"),
        BELIEFS_TOKEN=os.environ.get("BELIEFS_TOKEN", "").strip(),
        BELIEFS_TENANT=e("BELIEFS_TENANT", ""),                  # each agent sets its own default
        BELIEFS_TIMEOUT_S=float(e("BELIEFS_TIMEOUT_S", "6")),    # never hang on the NUC
        # "Overheard" notes older than this are no longer listed in the prompt (recall still finds them)
        BELIEFS_OVERHEARD_DAYS=float(e("BELIEFS_OVERHEARD_DAYS", "7")),
    )
    # --- A2A: this agent's own door (off unless A2A_TOKEN is set) and the other agent it can ask.
    port = int(e("A2A_PORT", str(a2a_port)))
    ns.update(
        A2A_TOKEN=os.environ.get("A2A_TOKEN", "").strip(),
        A2A_HOST=e("A2A_HOST", "0.0.0.0"), A2A_PORT=port,
        A2A_PUBLIC_URL=e("A2A_PUBLIC_URL", f"http://127.0.0.1:{port}").rstrip("/"),
        PEER_NAME=os.environ.get("PEER_NAME", "").strip(),               # e.g. "Cortana"
        PEER_A2A_URL=os.environ.get("PEER_A2A_URL", "").strip().rstrip("/"),   # e.g. http://127.0.0.1:8096
        PEER_A2A_TOKEN=os.environ.get("PEER_A2A_TOKEN", "").strip(),     # the peer's A2A_TOKEN
        PEER_TIMEOUT_S=float(e("PEER_TIMEOUT_S", "10")),
    )
    # --- Logs and the local HTTP door
    ns.update(
        LOG_MAX_BYTES=int(e("LOG_MAX_BYTES", str(5 * 1024 * 1024))), LOG_BACKUPS=int(e("LOG_BACKUPS", "4")),
        LISTEN_HOST=e("LISTEN_HOST", "127.0.0.1"), LISTEN_PORT=int(e("LISTEN_PORT", str(listen_port))),
    )
