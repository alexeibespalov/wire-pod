"""Vector's runtime configuration, all from environment variables (see deploy/pi/.env.example).

The settings every agent has (LLM, web/home tools, history, turn limits, sleep model, belief
store, A2A) are defined in agent_core/settings.py and loaded here; this file adds who Vector
is and everything about his body. Core modules read all of it through agent_core.settings.cfg.
"""
import os
import sys
from pathlib import Path

from agent_core import settings
from agent_core.settings import env as _env

settings.load_shared(globals(), provider_default="openai", a2a_port=8095, listen_port=8091)

# --- Who he is (read by the shared core: prompts, logs, the reconcile job)
ASSISTANT_NAME = "Vector"
LOGGER_NAME = "vector-agent"
ASSISTANT_DESC = "a small home robot who lives with a family (grown-ups and kids)"
PRONOUNS = ("he", "him", "his")
SELF_NODE = "robot:vector"            # the node his beliefs about himself are filed under
SEES_SPEAKERS = True                  # face recognition says who is in front of him
HEARS_OVERHEARD = True                # his wake word misfires: he catches talk not meant for him
RECONCILE_TAG = "sleep"               # how the reconcile job's lines read in his journal
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
BELIEFS_TENANT = BELIEFS_TENANT or "vector"   # noqa: F821  (defined by load_shared)
SKILL_EXAMPLES = {
    "name": "watch-video-games",
    "description": 'Watch someone play a video game on the TV and react ("watch me play", "watch my game")',
    "instructions": "routines/maneuvers/schedule settings",
    "tools": "['schedule_action', 'look']"}
ACTIVITY_TEXT = {
    "kinds": "conversation, tool, memory, schedule, practice, safety, body, sleep",
    "did": "tools you used and why, background schedules, practice, safety blocks, movements that stopped",
    "questions": '"what are you doing?", "why did you do that?", "what happened?", "were you practising?"',
    "log_words": "'error', 'practice', 'schedule', 'wake'"}
SUMMARY_PROMPT = """\
You keep the running notes for Vector, a small home robot, about his ongoing spoken \
conversation with his family (grown-ups and kids). Below are his notes so far and the \
next part of the transcript. Each human line starts with the time and who his face \
recognition saw in front of him ("eyes"); that's usually the speaker, but if someone \
said who they are, believe that.

Write the UPDATED notes: one plain-text document that replaces the old notes. Keep \
what would help him carry on naturally: who he talked with and when, what each person \
asked, liked, made, promised or is in the middle of (games, quizzes, stories, bets, \
questions he still owes an answer to), jokes and nicknames that stuck, and anything \
he did (pictures, music, moves). Attribute things to people by name. Merge, don't \
repeat; drop chit-chat that doesn't matter any more; older days can shrink to a line \
or two. Lines tagged "(overheard, not said to you)" are misheard bits of people talking \
near him, not to him: at most note the topic ("overheard grown-ups on a work call about \
X"), never as something anyone told him. Most recent things last. At most {words} words. \
No preamble."""

# --- Models only Vector uses (the shared LLM settings are in agent_core/settings.py)
# Body programs (app/programs.py, Claude only): the model that writes them.
PROGRAM_MODEL = _env("PROGRAM_MODEL", LLM_MODEL)

# Vision (camera "look" tool): any multimodal model. qwen3.6-flash and Claude both see.
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
# Only fast tools are exposed; crawl/map/research are far too slow for a spoken reply.
# Home Assistant's MCP Server integration (same Pi). Token: a long-lived access token of a
# dedicated non-admin HA user; set in the Pi's .env profiles, never in git.

# --- Agent behaviour
# Face images: written here by the agent, read by wire-pod at FACE_DIR_IN_WIREPOD
# (same host folder bind-mounted into both containers).
FACE_DIR = DATA_DIR / "face"
FACE_DIR_IN_WIREPOD = _env("FACE_DIR_IN_WIREPOD", "/face")
# Conversation length: past HISTORY_TOKENS of history, the oldest part is folded into a
# rolling summary (app/compaction.py) leaving about KEEP_TOKENS of recent messages.
# ~16k tokens is roughly 100+ spoken exchanges and costs ~1 s extra first-word latency.
SLEEP_KEEP_TOKENS = int(_env("SLEEP_KEEP_TOKENS", "2500"))  # overnight: fold down to a short tail
# Vector abandons a voice request after ~25 s (cloud-with-X face): past this, the agent
# stops calling tools and answers with what it has.
# Hard limits so nothing can wedge him: a turn is cancelled after TURN_HARD_LIMIT_S whatever
# it's doing, and a new request waits at most TURN_LOCK_WAIT_S for the previous one.
# Keep listening after a reply (no "Hey Vector" needed for follow-ups):
#   always    = after every spoken reply, except when a built-in routine was started
#   questions = only when the reply ends with a question
#   never     = only if the model adds the cue itself
FOLLOW_UP_MODE = _env("FOLLOW_UP_MODE", "always")
# Where Vector lives: default for weather/local questions. Set in the Pi's .env (kept out of git).

# --- Long-term memory: the belief store on the NUC (beliefs-api; app/beliefs.py). Unset =
# notes-only memory, exactly as before. The token is in the Pi's .env, never in git.

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

# --- Server
DEBUG = _env("AGENT_DEBUG", _env("PROXY_DEBUG", "0")) == "1"

settings.use(sys.modules[__name__])
