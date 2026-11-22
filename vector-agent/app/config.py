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
THREAD_IDLE_MINUTES = float(_env("THREAD_IDLE_MINUTES", "0"))  # 0 = one continuous conversation
# Conversation length: past HISTORY_TOKENS of history, the oldest part is folded into a
# rolling summary (app/compaction.py) leaving about KEEP_TOKENS of recent messages.
# ~16k tokens is roughly 100+ spoken exchanges and costs ~1 s extra first-word latency.
HISTORY_TOKENS = int(_env("HISTORY_TOKENS", "16000"))
KEEP_TOKENS = int(_env("KEEP_TOKENS", "8000"))
SUMMARY_WORDS = int(_env("SUMMARY_WORDS", "400"))
SLEEP_KEEP_TOKENS = int(_env("SLEEP_KEEP_TOKENS", "2500"))  # overnight: fold down to a short tail
MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "6"))
# Vector abandons a voice request after ~25 s (cloud-with-X face): past this, the agent
# stops calling tools and answers with what it has.
TURN_BUDGET_S = float(_env("TURN_BUDGET_S", "14"))
# Hard limits so nothing can wedge him: a turn is cancelled after TURN_HARD_LIMIT_S whatever
# it's doing, and a new request waits at most TURN_LOCK_WAIT_S for the previous one.
TURN_HARD_LIMIT_S = float(_env("TURN_HARD_LIMIT_S", "60"))
TURN_LOCK_WAIT_S = float(_env("TURN_LOCK_WAIT_S", "20"))
# Keep listening after a reply (no "Hey Vector" needed for follow-ups):
#   always    = after every spoken reply, except when a built-in routine was started
#   questions = only when the reply ends with a question
#   never     = only if the model adds the cue itself
FOLLOW_UP_MODE = _env("FOLLOW_UP_MODE", "always")
TIMEZONE = ZoneInfo(_env("AGENT_TZ", "Europe/London"))
# Where Vector lives: default for weather/local questions. Set in the Pi's .env (kept out of git).
HOME_LOCATION = os.environ.get("AGENT_LOCATION", "").strip()

# --- Exploration photos (app/explore_capture.py): snap the camera when his own exploring
# behaviour meets an obstacle/object. Listen-only, never takes behaviour control.
EXPLORE_DIR = DATA_DIR / "explore"          # photos/, captures.jsonl, analyses.jsonl
EXPLORE_CAPTURE = _env("EXPLORE_CAPTURE", "1") == "1"
EXPLORE_PROX_MM = int(_env("EXPLORE_PROX_MM", "110"))              # "object close" threshold
EXPLORE_PROX_MIN_QUALITY = float(_env("EXPLORE_PROX_MIN_QUALITY", "0.02"))  # ignore noise (idle ~0.01)
EXPLORE_PROX_COOLDOWN_S = float(_env("EXPLORE_PROX_COOLDOWN_S", "20"))
EXPLORE_OBJECT_COOLDOWN_S = float(_env("EXPLORE_OBJECT_COOLDOWN_S", "60"))  # per object
EXPLORE_MIN_INTERVAL_S = float(_env("EXPLORE_MIN_INTERVAL_S", "6"))         # any two photos
EXPLORE_MAX_PER_HOUR = int(_env("EXPLORE_MAX_PER_HOUR", "60"))
EXPLORE_MAX_PHOTOS = int(_env("EXPLORE_MAX_PHOTOS", "500"))        # disk cap: newest N kept
EXPLORE_MOVING_GRACE_S = float(_env("EXPLORE_MOVING_GRACE_S", "8"))  # still "exploring" this long after last motion

# --- Sleep routine (app/sleep_routine.py): chores done while he sleeps, never waking him.
SLEEP_ROUTINE = _env("SLEEP_ROUTINE", "1") == "1"
SLEEP_CALM_SECONDS = float(_env("SLEEP_CALM_SECONDS", "120"))      # calm power mode this long = asleep
SLEEP_QUIET_MINUTES = float(_env("SLEEP_QUIET_MINUTES", "10"))     # and no conversation for this long
SLEEP_FALLBACK = _env("SLEEP_FALLBACK", "1") == "1"                # on charger + idle (+ night) = asleep
SLEEP_FALLBACK_IDLE_MINUTES = float(_env("SLEEP_FALLBACK_IDLE_MINUTES", "30"))
SLEEP_FALLBACK_NIGHT_ONLY = _env("SLEEP_FALLBACK_NIGHT_ONLY", "1") == "1"
SLEEP_NIGHT_START = int(_env("SLEEP_NIGHT_START", "22"))           # local hour (AGENT_TZ)
SLEEP_NIGHT_END = int(_env("SLEEP_NIGHT_END", "7"))
SLEEP_SESSION_BUDGET_S = float(_env("SLEEP_SESSION_BUDGET_S", "1200"))
SLEEP_MIN_INTERVAL_MIN = float(_env("SLEEP_MIN_INTERVAL_MIN", "20"))  # between sessions
SLEEP_VLM_BATCH = int(_env("SLEEP_VLM_BATCH", "25"))               # photos analysed per session
SLEEP_VLM_DELAY_S = float(_env("SLEEP_VLM_DELAY_S", "2"))          # rate limit between calls
SLEEP_VLM_MAX_ATTEMPTS = int(_env("SLEEP_VLM_MAX_ATTEMPTS", "3"))

# --- Map understanding (nav map from the listen-only SDK link + exploration photo descriptions)
NAVMAP_FEED_SEC = float(_env("NAVMAP_FEED_SEC", "5"))       # min seconds between nav map pushes (robot only sends on change)
FINDINGS_HOURS = float(_env("FINDINGS_HOURS", "24"))        # how far back exploration findings go
FINDINGS_MAX = int(_env("FINDINGS_MAX", "8"))               # max photos summarised per answer

# --- A2A (Agent2Agent) server: lets other agents talk to Vector. Disabled unless a token is set.
A2A_TOKEN = os.environ.get("A2A_TOKEN", "").strip()
A2A_HOST = _env("A2A_HOST", "0.0.0.0")
A2A_PORT = int(_env("A2A_PORT", "8095"))
A2A_PUBLIC_URL = _env("A2A_PUBLIC_URL", f"http://127.0.0.1:{A2A_PORT}").rstrip("/")
A2A_ALLOW_CAMERA = _env("A2A_ALLOW_CAMERA", "0") == "1"

# --- Music / podcasts / MIDI
MUSIC_DIR = _env("MUSIC_DIR", "/media/Music")      # read-only mount of the NAS music share
MAX_QUEUE = int(_env("MAX_QUEUE", "60"))
MAX_PLAY_MINUTES = int(_env("MAX_PLAY_MINUTES", "180"))
MAX_MIDI_MINUTES = int(_env("MAX_MIDI_MINUTES", "10"))
SOUNDFONT = _env("SOUNDFONT", "/usr/share/sounds/sf2/TimGM6mb.sf2")

# --- Known places (app/places.py): which surface is he on? decided while he sleeps
PLACE_MIN_PHOTOS = int(_env("PLACE_MIN_PHOTOS", "3"))          # described photos needed per stay
PLACE_GROUP_GAP_H = float(_env("PLACE_GROUP_GAP_H", "3"))      # a gap this long splits a stay
PLACE_MATCH_CONF = float(_env("PLACE_MATCH_CONF", "0.75"))     # >= this: it's that place
PLACE_PROBABLE_CONF = float(_env("PLACE_PROBABLE_CONF", "0.5"))  # >= this: probably it (never a new place)
PLACE_CONFIRM_SIGHTINGS = int(_env("PLACE_CONFIRM_SIGHTINGS", "2"))  # provisional -> confirmed
PLACE_PROVISIONAL_DAYS = float(_env("PLACE_PROVISIONAL_DAYS", "7"))  # unconfirmed one-offs expire
PLACES_MAX = int(_env("PLACES_MAX", "20"))


# --- Scheduled actions (app/scheduler.py) and routines (app/routines.py)
SCHEDULE_MAX_ACTIVE = int(_env("SCHEDULE_MAX_ACTIVE", "5"))
SCHEDULE_MIN_EVERY_S = float(_env("SCHEDULE_MIN_EVERY_S", "5"))
SCHEDULE_DEFAULT_MINUTES = float(_env("SCHEDULE_DEFAULT_MINUTES", "30"))
SCHEDULE_MAX_MINUTES = float(_env("SCHEDULE_MAX_MINUTES", "180"))
SCHEDULE_SPEECH_GAP_S = float(_env("SCHEDULE_SPEECH_GAP_S", "20"))      # per repeating schedule
SCHEDULE_CAMERA_PER_HOUR = int(_env("SCHEDULE_CAMERA_PER_HOUR", "400"))  # background looks (VLM calls)

# --- Practice (app/practice.py). Idle practice = practise his least reliable maneuver on his
# own when awake, off the charger and nobody has talked to him for a while. Off by default.
BODY_IDLE_PRACTICE = _env("BODY_IDLE_PRACTICE", "0") == "1"
BODY_IDLE_AFTER_MIN = float(_env("BODY_IDLE_AFTER_MIN", "15"))
BODY_IDLE_MINUTES = float(_env("BODY_IDLE_MINUTES", "5"))
BODY_IDLE_CHECK_MIN = float(_env("BODY_IDLE_CHECK_MIN", "10"))

# --- Persistent log file (agent-data/logs/agent.log, rotating)
LOG_MAX_BYTES = int(_env("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(_env("LOG_BACKUPS", "4"))

# --- Server
LISTEN_HOST = _env("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(_env("LISTEN_PORT", "8091"))
DEBUG = _env("AGENT_DEBUG", _env("PROXY_DEBUG", "0")) == "1"
