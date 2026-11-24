"""Cortana's runtime configuration, all from environment variables (see deploy/pi/cortana.env.example).

The settings every agent has (LLM, web/home tools, history, turn limits, background model,
belief store, A2A) are defined in agent_core/settings.py and loaded here; this file adds who
Cortana is and her voice, timers and music. Core modules read all of it through
agent_core.settings.cfg.
"""
import sys
from pathlib import Path

from agent_core import settings
from agent_core.settings import env as _env

settings.load_shared(globals(), provider_default="anthropic", a2a_port=8096, listen_port=8092)

# --- Who she is (read by the shared core: prompts, logs, the reconcile job)
ASSISTANT_NAME = _env("ASSISTANT_NAME", "Cortana")
LOGGER_NAME = "cortana-agent"
ASSISTANT_DESC = "the voice assistant of a family home (grown-ups and kids)"
PRONOUNS = ("she", "her", "her")
SELF_NODE = f"assistant:{ASSISTANT_NAME.lower()}"   # the node her beliefs about herself are filed under
SEES_SPEAKERS = False                 # she can't see: a speaker is known only if they said who they are
HEARS_OVERHEARD = False               # she only hears what follows her wake word
RECONCILE_TAG = "nightly"             # how the reconcile job's lines read in her journal
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
BELIEFS_TENANT = BELIEFS_TENANT or "cortana"   # noqa: F821  (defined by load_shared)
SKILL_EXAMPLES = {
    "name": "meal-planning",
    "description": 'Plan the week\'s dinners and add the ingredients to the shopping list ("plan dinners", "meal plan")',
    "instructions": "tools and settings",
    "tools": "['set_reminder', 'tavily_search']"}
ACTIVITY_TEXT = {
    "kinds": "conversation, tool, reminder, safety, memory, nightly",
    "did": "tools you used and why, timers and reminders that went off, safety blocks, memory changes",
    "questions": '"what did you do?", "why did you do that?", "what happened?"',
    "log_words": "'error', 'timer', 'heard', 'whisper'"}
SUMMARY_PROMPT = """\
You keep the running notes for {name}, the voice assistant of a family home, about her \
ongoing spoken conversation with the family (grown-ups and kids). Below are her notes so \
far and the next part of the transcript. Each human line starts with the time it was \
heard; she can't see who's speaking, so only name people who said who they are.

Write the UPDATED notes: one plain-text document that replaces the old notes. Keep \
what would help her carry on naturally: who she talked with and when, what people \
asked, planned, promised or are in the middle of (recipes, homework, plans, questions \
she still owes an answer to), jokes that stuck, and anything she did (timers, \
reminders, the shopping list, the lights). Attribute things to people by name. Merge, don't \
repeat; drop chit-chat that doesn't matter any more; older days can shrink to a line \
or two. Most recent things last. At most {words} words. No preamble."""


# --- LLM (see app/llm.py). Claude by default; any OpenAI-compatible endpoint also works.
# Claude: thinking is always on; effort is the speed/quality dial. Voice: low.
# Server-side refusal fallbacks ("default"); empty for Haiku, which has none.

# --- Web search (Tavily remote MCP). Optional: no key -> no web tools.
# Home Assistant's MCP Server integration. Optional: no token -> no smart-home tools.

# --- Voice: the Pi Zero satellite (wyoming-satellite) + speech services on this host
SATELLITE_URI = _env("SATELLITE_URI", "tcp://192.168.1.96:10700")   # empty = text only
WHISPER_URL = _env("WHISPER_URL", "http://127.0.0.1:10330").rstrip("/")  # whisper.cpp whisper-server
WHISPER_TIMEOUT = float(_env("WHISPER_TIMEOUT", "10"))
PIPER_URI = _env("PIPER_URI", "tcp://127.0.0.1:10200")              # Wyoming Piper
PIPER_VOICE = _env("PIPER_VOICE", "")                               # empty = the server's default
VOICE_FX = _env("VOICE_FX", "")      # "shimmer": a light digital-AI chorus on her voice; empty = plain
# End of speech (webrtcvad on the 16 kHz mic stream)
VAD_MODE = int(_env("VAD_MODE", "2"))                    # 0 (lenient) .. 3 (strict)
VAD_SILENCE_S = float(_env("VAD_SILENCE_S", "0.8"))      # this much quiet after speech = done
VAD_START_TIMEOUT_S = float(_env("VAD_START_TIMEOUT_S", "6"))   # nobody spoke after the chime
VAD_MAX_S = float(_env("VAD_MAX_S", "15"))               # hard cap on one utterance
# Keep listening after a reply without the wake word:
#   questions = only when the reply ends with a question (default)
#   always    = after every spoken reply
#   never     = wake word every time
FOLLOW_UP_MODE = _env("FOLLOW_UP_MODE", "questions")

# --- Music: MPD on the Pi Zero plays the NAS library on the speaker (deploy/pizero/music.sh).
# Empty = no music.
MPD_HOST = _env("MPD_HOST", "192.168.1.96")
MPD_PORT = int(_env("MPD_PORT", "6600"))

# --- Agent behaviour
NIGHT_KEEP_TOKENS = int(_env("NIGHT_KEEP_TOKENS", "2500"))    # overnight: fold down to a short tail
NIGHTLY_AT = _env("NIGHTLY_AT", "03:30")                       # local time of the nightly chores
# Past this, stop calling tools and answer with what she has (nobody likes waiting).

# --- Memory. json = a facts file on the data volume. kumiho = shared graph memory with
# Vector (not set up yet; see app/memory.py).

# --- Timers and reminders (app/reminders.py)
REMINDERS_MAX = int(_env("REMINDERS_MAX", "20"))
REMINDER_RETRY_MIN = float(_env("REMINDER_RETRY_MIN", "15"))  # keep trying to say it this long

# --- Persistent log file (DATA_DIR/logs/agent.log, rotating)

# --- Server (health + a localhost text door for testing)
DEBUG = _env("AGENT_DEBUG", "0") == "1"

settings.use(sys.modules[__name__])
