"""Runtime configuration, all from environment variables (see deploy/pi/.env.example)."""
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"missing required env var {name}")
    return val


# --- LLM (any OpenAI-compatible chat endpoint)
LLM_BASE_URL = _env("UPSTREAM_BASE")
LLM_API_KEY = _env("UPSTREAM_KEY")
LLM_MODEL = _env("FORCE_MODEL", "qwen3.6-flash")
# Qwen thinks by default (~5x slower first token); wire-pod can't turn it off, we do.
LLM_EXTRA_BODY = json.loads(_env("LLM_EXTRA_BODY", '{"enable_thinking": false}'))
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.8"))

# --- Robot (via wire-pod's local SDK web API)
WIREPOD_URL = _env("WIREPOD_URL", "http://127.0.0.1:8080").rstrip("/")
VECTOR_SERIAL = _env("VECTOR_SERIAL", "00609161")

# --- Web search (Tavily remote MCP). Optional: no key -> no web tools.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
TAVILY_MCP_URL = _env("TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/")
# Only fast tools are exposed; crawl/map/research are far too slow for a spoken reply.
TAVILY_TOOLS = [t.strip() for t in _env("TAVILY_TOOLS", "tavily_search,tavily_extract").split(",") if t.strip()]
TAVILY_DEFAULTS = _env("TAVILY_DEFAULTS", '{"search_depth": "basic", "max_results": 5}')

# --- Agent behaviour
DATA_DIR = Path(_env("AGENT_DATA_DIR", "/data"))
THREAD_IDLE_MINUTES = float(_env("THREAD_IDLE_MINUTES", "10"))
MAX_HISTORY_MESSAGES = int(_env("MAX_HISTORY_MESSAGES", "40"))
MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "6"))
TIMEZONE = ZoneInfo(_env("AGENT_TZ", "Europe/London"))

# --- Server
LISTEN_HOST = _env("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(_env("LISTEN_PORT", "8091"))
DEBUG = _env("AGENT_DEBUG", _env("PROXY_DEBUG", "0")) == "1"
