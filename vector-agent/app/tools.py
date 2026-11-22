"""Tools the agent can call. Robot tools go through wire-pod's local SDK web API
(http://127.0.0.1:8080/api-sdk/...), which already holds an authenticated gRPC
connection to Vector.

Movement safety: every motion is speed- and time-capped and always ends with a
stop command, even on error. Vector's own cliff/pickup reflexes stay active
because we only ever take behaviour control at DEFAULT priority.
"""
import asyncio
import contextlib
from typing import Literal

from langchain_core.tools import tool

from . import memory, robot
from . import turn as turn_state  # NB: `turn` is also a tool name below
from .animations import play_animation
from .draw import draw_picture
from .emoji import show_emoji
from .faces import forget_face, learn_face
from .music_tools import MUSIC_TOOLS
from .face import show_last_view, show_picture, show_text
from .robot import sdk as _sdk
from .skills import load_skill
from .vision import look


@contextlib.asynccontextmanager
async def _in_control():
    """Pause Vector's autonomous behaviours while we drive his motors. If wire-pod already
    holds control this turn, use it (asking again would cut off his speech)."""
    if turn_state.wirepod_has_control:
        yield
        return
    await _sdk("assume_behavior_control", priority="default")
    await asyncio.sleep(0.4)  # let the control grant land
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await _sdk("release_behavior_control")


async def _wheels(lw: int, rw: int, seconds: float) -> None:
    try:
        await _sdk("move_wheels", lw=lw, rw=rw)
        await asyncio.sleep(seconds)
    finally:
        with contextlib.suppress(Exception):
            await _sdk("move_wheels", lw=0, rw=0)


# --------------------------------------------------------------------- movement
_DRIVE_SECONDS = {"tiny": 0.5, "short": 1.0, "medium": 2.0}
_DRIVE_SPEED = 80  # mm/s  -> tiny ~4cm, short ~8cm, medium ~16cm


@tool
async def drive(direction: Literal["forward", "backward"],
                distance: Literal["tiny", "short", "medium"] = "short") -> str:
    """Drive Vector's wheels straight forward or backward.
    tiny is about 4 cm, short about 8 cm, medium about 16 cm. Use for requests like
    "come closer", "back up a bit", "scoot forward". He will drive off his charger if on it."""
    v = _DRIVE_SPEED if direction == "forward" else -_DRIVE_SPEED
    async with _in_control():
        await _wheels(v, v, _DRIVE_SECONDS[distance])
    return f"drove {direction} ({distance})"


_TURN_SECONDS = {"a little": 0.25, "quarter": 0.75, "half": 1.5, "full circle": 3.0}
_TURN_SPEED = 50  # mm/s per wheel, opposite directions -> ~120 deg/s


@tool
async def turn(direction: Literal["left", "right"],
               amount: Literal["a little", "quarter", "half", "full circle"] = "quarter") -> str:
    """Turn Vector in place. quarter is about 90 degrees, half about 180, full circle 360.
    Use for "turn around", "look left", "spin"."""
    s = _TURN_SPEED
    lw, rw = (-s, s) if direction == "left" else (s, -s)
    async with _in_control():
        await _wheels(lw, rw, _TURN_SECONDS[amount])
    return f"turned {direction} ({amount})"


@tool
async def move_head(direction: Literal["up", "down", "level"]) -> str:
    """Tilt Vector's head. "up" looks up at a standing person, "down" looks at the
    desk in front of him, "level" looks straight ahead."""
    async with _in_control():
        if direction == "level":
            await _sdk("move_head", speed=-3)
            await asyncio.sleep(0.8)
            await _sdk("move_head", speed=2)
            await asyncio.sleep(0.35)
        else:
            await _sdk("move_head", speed=3 if direction == "up" else -3)
            await asyncio.sleep(0.8)
        await _sdk("move_head", speed=0)
    return f"head {direction}"


@tool
async def move_lift(direction: Literal["up", "down"]) -> str:
    """Raise or lower Vector's lift (his little forklift arms). Good for waving,
    cheering ("arms up!") or flexing."""
    async with _in_control():
        await _sdk("move_lift", speed=4 if direction == "up" else -4)
        await asyncio.sleep(0.8)
        await _sdk("move_lift", speed=0)
    return f"lift {direction}"


# --------------------------------------------------------------- built-in actions
_ACTIONS = {
    "dance": "intent_imperative_dance",
    "fist bump": "intent_play_fistbump",
    "go to charger": "intent_system_charger",
    "go to sleep": "intent_system_sleep",
    "explore": "intent_explore_start",
    "come to the user": "intent_imperative_come",
    "look at the user": "intent_imperative_lookatme",
    "do a trick": "intent_play_anytrick",
    "pop a wheelie": "intent_play_popawheelie",
    "find cube": "intent_imperative_findcube",
    "roll cube": "intent_play_rollcube",
    "pick up cube": "intent_play_pickupcube",
    "take a photo": "intent_photo_take_extend",
    "play blackjack": "intent_play_blackjack",
    "play keepaway": "intent_play_keepaway",
    "recognize the user": "intent_names_ask",
}


@tool
async def do_action(action: Literal[
    "dance", "fist bump", "go to charger", "go to sleep", "explore", "come to the user",
    "look at the user", "do a trick", "pop a wheelie", "find cube", "roll cube",
    "pick up cube", "take a photo", "play blackjack", "play keepaway", "recognize the user",
]) -> str:
    """Start one of Vector's own built-in behaviours (his real personality routines,
    with full animation and sound). It starts right after you reply, so keep your
    spoken reply short when you use this."""
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
    """Save a fact about the user or household to long-term memory, written as a short
    third-person sentence (e.g. "The user's sister is called Mia"). Use when the user
    tells you something worth knowing later, or asks you to remember something."""
    return memory.add(fact)


@tool
def forget(about: str) -> str:
    """Delete remembered facts containing this word or phrase, when the user asks you
    to forget something or a fact is wrong."""
    return memory.forget(about)


TOOLS = [drive, turn, move_head, move_lift, do_action, set_eye_color, battery_status,
         remember, forget,
         load_skill, play_animation, look, show_picture, show_text, show_last_view, draw_picture,
         show_emoji, learn_face, forget_face, *MUSIC_TOOLS]
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
