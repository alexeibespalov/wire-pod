"""Tools the agent can call. Robot tools go through wire-pod's local SDK web API
(http://127.0.0.1:8080/api-sdk/...), which already holds an authenticated gRPC
connection to Vector.

Movement safety: every motion is speed- and time-capped and always ends with a
stop command, even on error. Vector's own cliff/pickup reflexes stay active
because we only ever take behaviour control at DEFAULT priority.
"""
from typing import Literal

from langchain_core.tools import tool

from . import memory, robot
from . import turn as turn_state  # NB: `turn` is also a tool name below
from .animations import play_animation
from .draw import draw_picture
from .emoji import show_emoji
from .faces import forget_face, learn_face
from .music_tools import MUSIC_TOOLS
from .explore_capture import recent_exploration_finds, set_exploration_capture
from .face import show_last_view, show_picture, show_text
from .robot import sdk as _sdk
from .skills import SKILL_TOOLS, load_skill
from .routines import ROUTINE_TOOLS
from .scheduler import SCHEDULE_TOOLS
from .maneuvers import MANEUVER_TOOLS
from .practice import PRACTICE_TOOL_LIST
from .activity import ACTIVITY_TOOLS
from .sleep_routine import run_sleep_routine_now, sleep_routine_status
from .navmap import show_map, where_am_i
from .explore import exploration_findings
from .places import PLACE_TOOLS
from .vision import look


# --------------------------------------------------------------------- movement
# The easy, coarse moves. They run on the body engine (app/body.py), so they get the same
# safety governor as maneuvers: stop if picked up / at a cliff / tilted, speed and distance
# caps, one motion at a time. For exact numbers use `move` (movement skill).
_DRIVE_MM = {"tiny": 40, "short": 80, "medium": 160}
_TURN_DEG = {"a little": 25, "quarter": 90, "half": 180, "full circle": 360}
_HEAD_DEG = {"up": 40, "down": -20, "level": 0}


async def _steps(steps: list[dict], label: str) -> str:
    from .maneuvers import ManeuverError, _human_err, format_summary, run_steps
    try:
        r = await run_steps(steps, label=label)
    except ManeuverError as e:
        return _human_err(e)
    except Exception as e:   # BodyError and friends: plain words for the model
        return f"error: {e}"
    if "error" in r:
        return f"error: {r['error']}"
    return format_summary(label, r)


@tool
async def drive(direction: Literal["forward", "backward"],
                distance: Literal["tiny", "short", "medium"] = "short") -> str:
    """Drive Vector's wheels straight forward or backward.
    tiny is about 4 cm, short about 8 cm, medium about 16 cm. Use for requests like
    "come closer", "back up a bit", "scoot forward". On his charger, "forward" first drives
    off it. For exact distances use `move`."""
    from . import body
    pre = ""
    if body.FEED.snapshot()["status"] & body.ST_ON_CHARGER:
        if direction == "backward":
            return "error: I'm on my charger, I can't back up from here"
        try:
            pre = await body.drive_off_charger() + "; "
        except Exception as e:
            return f"error: {e}"
    mm = _DRIVE_MM[distance] * (1 if direction == "forward" else -1)
    return pre + await _steps([{"step": "drive", "mm": mm, "speed": 80}], f"drove {direction}")


@tool
async def turn(direction: Literal["left", "right"],
               amount: Literal["a little", "quarter", "half", "full circle"] = "quarter") -> str:
    """Turn Vector in place. quarter is about 90 degrees, half about 180, full circle 360.
    Use for "turn around", "look left", "spin". For exact angles use `move`."""
    deg = _TURN_DEG[amount] * (1 if direction == "left" else -1)
    if abs(deg) >= 360:   # one step can't express a full circle reliably: two halves
        steps = [{"step": "turn", "deg": deg / 2}, {"step": "turn", "deg": deg / 2}]
    else:
        steps = [{"step": "turn", "deg": deg}]
    return await _steps(steps, f"turned {direction}")


@tool
async def move_head(direction: Literal["up", "down", "level"]) -> str:
    """Tilt Vector's head. "up" looks up at a standing person, "down" looks at the
    desk in front of him, "level" looks straight ahead. For exact angles use `move`."""
    return await _steps([{"step": "head", "deg": _HEAD_DEG[direction]}], f"head {direction}")


@tool
async def move_lift(direction: Literal["up", "down"]) -> str:
    """Raise or lower Vector's lift (his little forklift arms). Good for waving,
    cheering ("arms up!") or flexing."""
    return await _steps([{"step": "lift", "height": 1.0 if direction == "up" else 0.0}],
                        f"lift {direction}")


# --------------------------------------------------------------- built-in actions
# wire-pod's /api-sdk/cloud_intent sends an AppIntent, and Vector only accepts the app
# intents listed in his user_intent_map.json (checked on the robot 2026-11-22). Anything
# else is silently ignored ("No match for app intent"), so only these can be started here.
_ACTIONS = {
    "dance": "intent_imperative_dance",
    "go to charger": "intent_system_charger",
    "go to sleep": "intent_system_sleep",
    "explore": "explore_start",
    "come to the user": "intent_imperative_come",
    "look at the user": "intent_imperative_lookatme",
    "find cube": "intent_imperative_findcube",
    "fetch cube": "intent_imperative_fetchcube",   # find it, pick it up, bring it over
    "pick up cube": "intent_imperative_fetchcube",
    "recognize the user": "intent_names_ask",
}
# Built-in behaviours that only start as the answer to a voice request (cloud intents).
# Right after "Hey Vector" the agent can still give that answer (turn.claim_intent): the
# server replies {{doIntent||...}} and wire-pod's patched chipper sends the intent. In
# follow-up listening the request is already answered, so the person must say the phrase.
_CLOUD = {
    "fist bump": ("intent_play_fistbump", "fist bump"),
    "do a trick": ("intent_play_anytrick", "show me a trick"),
    "pop a wheelie": ("intent_play_popawheelie", "pop a wheelie"),
    "roll cube": ("intent_play_rollcube", "roll your cube"),
    "take a photo": ("intent_photo_take_extend", "take a photo of me"),
    "play blackjack": ("intent_play_blackjack", "play blackjack"),
    "play keepaway": ("intent_play_keepaway", "keep away"),
}


@tool
async def do_action(action: Literal[
    "dance", "fist bump", "go to charger", "go to sleep", "explore", "come to the user",
    "look at the user", "do a trick", "pop a wheelie", "find cube", "fetch cube", "roll cube",
    "pick up cube", "take a photo", "play blackjack", "play keepaway", "recognize the user",
]) -> str:
    """Start one of Vector's own built-in behaviours (his real personality routines,
    with full animation and sound). It starts right after you reply, so keep your
    spoken reply short when you use this. Your cube: find cube / fetch cube (find it,
    pick it up and bring it), roll cube. You recognise the cube by sight (its markings),
    so you can see it even when its battery is flat; only its lights, taps and shakes
    need the battery. Fist bump, trick, wheelie, roll cube, photo, blackjack and keepaway
    must be your FIRST action, before you say anything."""
    if action in _CLOUD:
        from . import turn
        intent, phrase = _CLOUD[action]
        if turn.claim_intent(intent):
            return (f"starting '{action}' now, as your whole answer: your words this turn "
                    f"won't be spoken, so just finish without saying anything.")
        return (f"can't start '{action}' right now: it only starts at the very beginning of "
                f"a request. Ask them to say \"Hey Vector, {phrase}\". Tell them that, in a few words.")
    await _sdk("cloud_intent", intent=_ACTIONS[action])
    return f"started: {action}"


# ------------------------------------------------------------------ appearance etc
_EYE_HUES = {
    "teal": (0.47, 0.95), "blue": (0.6, 0.95), "purple": (0.78, 0.9), "pink": (0.9, 0.7),
    "red": (0.0, 1.0), "orange": (0.08, 1.0), "yellow": (0.15, 1.0), "green": (0.33, 1.0),
    "lime": (0.25, 1.0), "white": (0.0, 0.0),
}


@tool
async def set_eye_color(color: Literal["teal", "blue", "purple", "pink", "red", "orange",
                                       "yellow", "green", "lime", "white"]) -> str:
    """Change the colour of Vector's eyes (stays until changed again)."""
    hue, sat = _EYE_HUES[color]
    await _sdk("custom_eye_color", hue=hue, sat=sat)
    return f"eyes are now {color}"


@tool
async def battery_status() -> str:
    """Check Vector's own battery level and whether he is on his charger."""
    import json as _json
    d = _json.loads(await _sdk("get_battery"))
    level = {1: "low", 2: "nominal", 3: "full"}.get(d.get("battery_level"), "unknown")
    return (f"battery {level}, {d.get('battery_volts', 0):.2f} volts, "
            f"{'on' if d.get('is_on_charger_platform') else 'off'} the charger")


# ------------------------------------------------------------------------- memory
@tool
def remember(fact: str) -> str:
    """Save a lasting fact about someone in the family or the home to long-term memory,
    as a short third-person sentence that names who it's about (e.g. "Leo's favourite
    game is Animal Crossing", "Alexei's sister is called Mia"). Use when someone tells you
    something worth knowing on another day, or asks you to remember something. (Today's
    chat is kept in your conversation notes anyway; this is for things that should last.)"""
    return memory.add(fact)


@tool
def forget(about: str) -> str:
    """Delete remembered facts containing this word or phrase, when the user asks you
    to forget something or a fact is wrong."""
    return memory.forget(about)


TOOLS = [drive, turn, move_head, move_lift, do_action, set_eye_color, battery_status,
         remember, forget,
         load_skill, play_animation, look, show_picture, show_text, show_last_view, draw_picture,
         show_emoji, learn_face, forget_face, *MUSIC_TOOLS,
         recent_exploration_finds, set_exploration_capture, sleep_routine_status, run_sleep_routine_now,
         where_am_i, exploration_findings, show_map, *PLACE_TOOLS,
         *MANEUVER_TOOLS, *PRACTICE_TOOL_LIST, *ROUTINE_TOOLS, *SCHEDULE_TOOLS, *SKILL_TOOLS,
         *ACTIVITY_TOOLS]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


# ------------------------------------------------------------------- speak at home
@tool
async def say_aloud(text: str) -> str:
    """Say something out loud at home in your own voice. Only for messages from a remote
    agent (A2A), e.g. 'tell Alexei dinner is ready'. In normal voice chat you're already
    speaking, so never use it there."""
    if turn_state.channel != "a2a":
        return "error: you're already talking out loud; just say it in your reply"
    text = " ".join(text.split())[:300]
    if not text:
        return "error: nothing to say"
    if "]==]" in text:
        text = text.replace("]==]", "] ==]")
    # Lua long-bracket string: no escaping needed for arbitrary text
    await robot.lua(f"{robot.control()}sayText([==[{text}]==])")
    return f"said out loud: {text}"


TOOLS.append(say_aloud)
TOOLS_BY_NAME[say_aloud.name] = say_aloud
