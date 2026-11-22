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

# Vision (camera "look" tool): any multimodal model on the same endpoint. qwen3.6-flash sees.
VISION_MODEL = _env("VISION_MODEL", LLM_MODEL)

# --- Robot (via wire-pod's local SDK web API)
WIREPOD_URL = _env("WIREPOD_URL", "http://127.0.0.1:8080").rstrip("/")
VECTOR_SERIAL = _env("VECTOR_SERIAL", "00609161")

# --- Face screen. Vector 1.0 = 184x96 (Vector 2.0 "xray" = 160x80). wire-pod's showImage
# doesn't scale, so images must match exactly.
FACE_W = int(_env("FACE_W", "184"))
FACE_H = int(_env("FACE_H", "96"))
FACE_FONT = _env("FACE_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
SHOW_PICTURE_SUFFIX = _env("SHOW_PICTURE_SUFFIX", "photo")

# --- Web search (Tavily remote MCP). Optional: no key -> no web tools.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
TAVILY_MCP_URL = _env("TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/")
# Only fast tools are exposed; crawl/map/research are far too slow for a spoken reply.
TAVILY_TOOLS = [t.strip() for t in _env("TAVILY_TOOLS", "tavily_search,tavily_extract").split(",") if t.strip()]
TAVILY_DEFAULTS = _env("TAVILY_DEFAULTS", '{"search_depth": "basic", "max_results": 5}')

# --- Agent behaviour
DATA_DIR = Path(_env("AGENT_DATA_DIR", "/data"))
# Face images: written here by the agent, read by wire-pod at FACE_DIR_IN_WIREPOD
# (same host folder bind-mounted into both containers).
FACE_DIR = DATA_DIR / "face"
FACE_DIR_IN_WIREPOD = _env("FACE_DIR_IN_WIREPOD", "/face")
THREAD_IDLE_MINUTES = float(_env("THREAD_IDLE_MINUTES", "10"))
MAX_HISTORY_MESSAGES = int(_env("MAX_HISTORY_MESSAGES", "40"))
MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "6"))
# Keep listening after a reply (no "Hey Vector" needed for follow-ups):
#   always    = after every spoken reply, except when a built-in routine was started
#   questions = only when the reply ends with a question
#   never     = only if the model adds the cue itself
FOLLOW_UP_MODE = _env("FOLLOW_UP_MODE", "always")
TIMEZONE = ZoneInfo(_env("AGENT_TZ", "Europe/London"))
# Where Vector lives: default for weather/local questions. Set in the Pi's .env (kept out of git).
HOME_LOCATION = os.environ.get("AGENT_LOCATION", "").strip()

# --- A2A (Agent2Agent) server: lets other agents talk to Vector. Disabled unless a token is set.
A2A_TOKEN = os.environ.get("A2A_TOKEN", "").strip()
A2A_HOST = _env("A2A_HOST", "0.0.0.0")
A2A_PORT = int(_env("A2A_PORT", "8095"))
A2A_PUBLIC_URL = _env("A2A_PUBLIC_URL", f"http://127.0.0.1:{A2A_PORT}").rstrip("/")
A2A_ALLOW_CAMERA = _env("A2A_ALLOW_CAMERA", "0") == "1"

# --- Persistent log file (agent-data/logs/agent.log, rotating)
LOG_MAX_BYTES = int(_env("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(_env("LOG_BACKUPS", "4"))

# --- Server
LISTEN_HOST = _env("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(_env("LISTEN_PORT", "8091"))
DEBUG = _env("AGENT_DEBUG", _env("PROXY_DEBUG", "0")) == "1"
