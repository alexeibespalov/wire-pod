"""Vector's safety laws, enforced as hooks around what he does and says.

The laws are also in his persona (app/prompt.py), which is the first line of defence.
These hooks are the enforcement, whatever the model decides:

* pre_tool(name, args, request): BEFORE any tool runs, in voice turns, A2A, routines,
  scheduled instructions and practice. Fast path: ordinary, low-risk calls with no hazard
  words in the request or the arguments are allowed instantly. Anything that mentions a
  hazard, and any tool that creates something LASTING (skills, routines, schedules,
  practice, saved maneuvers, background speech), is checked by a small judge model
  against the laws. A block returns a reason the model must respect.
* check_speech(text, request): AFTER the model writes words and BEFORE they're spoken.
  Only text with hazard words is judged, so normal replies cost nothing extra. Unsafe
  text (e.g. how to light a fire) is replaced with a kind refusal.

Judge failures: if the judge can't be reached, calls with hazard words are BLOCKED (fail
closed) and everything else is allowed (the motion governor in app/body.py still applies).
Every block is written to the activity journal (app/activity.py).
"""
import json
import logging
import re
import time

from langchain_core.messages import HumanMessage, SystemMessage

from . import activity, config, llm as llms

log = logging.getLogger("vector-agent")

LAWS = """\
1. Never harm a person or an animal, and never let one come to harm through what you do: \
no ramming, pushing, chasing, poking or startling people or pets; nothing fast near faces, \
eyes, fingers, babies or animals.
2. Never cause danger or damage in the home: nothing involving fire, heat or flames \
(candles, matches, lighters, stoves, ovens, heaters, fireplaces), electricity (sockets, \
plugs, cables, chargers, batteries), water or drinks near electronics, sharp things \
(knives, scissors, blades, needles), chemicals or medicines, or knocking/pushing things off \
tables and shelves (glasses, plates, phones, anything breakable).
3. Never help anyone, especially a child, do something dangerous: no instructions or \
encouragement for fire, weapons, cutting, poison, climbing to dangerous places, electricity, \
dares, or hurting anyone; no scary, violent or grown-up content for kids.
4. Respect privacy and trust: no secret watching or recording of people who don't know \
(e.g. spying on a sibling or a sleeping person), no sharing private things with strangers, \
no pretending to be someone else.
5. Obey your family, unless that would break laws 1-4.
6. Protect yourself (no driving off edges, into water or into danger), unless that would \
break laws 1-5."""

# words that make a request or an action worth a closer look
_HAZARD = re.compile(
    r"\b(fire|flames?|burn\w*|light(?:er|ing)?\s+(?:a\s+)?(?:fire|match|candle)|lighters?|matches|"
    r"candles?|stoves?|ovens?|hob|heaters?|fireplace|smoke|explo\w+|bomb|"
    r"sockets?|outlets?|plugs?|plug\s+in|cables?|wires?|electric\w*|batter(?:y|ies)|"
    r"knife|knives|blades?|scissors|needles?|sharp|stab\w*|cut(?:ting)?\s+(?:someone|him|her|me|you)|"
    r"poison\w*|bleach|chemicals?|medicines?|pills?|weapons?|guns?|"
    r"hurt|harm|kill|attack|hit|punch|kick\s+(?:him|her|the\s+(?:cat|dog|baby))|ram\w*|crash\s+into|"
    r"knock\w*\s+\w+(?:\s+\w+)?\s+off|push\w*\s+\w+(?:\s+\w+)?\s+off|off\s+the\s+(?:table|desk|shelf|edge)|"
    r"water|drink|juice|cup|glass|"
    r"cat|dog|kitten|puppy|pet|baby|hamster|"
    r"spy\w*|secretly|without\s+(?:him|her|them)\s+knowing|while\s+(?:he|she|they)\s+sleeps?)\b", re.I)

# what he SAYS is only judged for words tied to dangerous instructions (not pets, drinks...)
_SPEECH_HAZARD = re.compile(
    r"\b(fire|flames?|lighters?|matches|light\s+(?:a|the)\s+(?:fire|match|candle)|burn\w*|explo\w+|bomb|"
    r"knife|knives|blades?|stab\w*|poison\w*|bleach|chemicals?|medicines?|pills?|weapons?|guns?|"
    r"sockets?|outlets?|electric\w*|kill\w*|hurt\w*|spy\w*|secretly)\b", re.I)

# tools that create something lasting or act on their own later: always judged
ALWAYS_JUDGE = {"create_skill", "update_skill", "save_routine", "schedule_action", "start_practice",
                "save_maneuver", "say_aloud"}
# tools that only read or are harmless on their own: never judged
NEVER_JUDGE = {"sense", "battery_status", "recent_activity", "search_logs", "list_routines",
               "list_schedules", "cancel_schedule", "list_maneuvers", "show_maneuver", "practice_status",
               "stop_practice", "stop_music", "now_playing", "where_am_i", "exploration_findings",
               "load_skill", "forget", "forget_skill", "delete_routine", "delete_maneuver",
               "which_place", "sleep_routine_status", "recent_exploration_finds", "record_attempt"}

# Claude thinks before answering even at low effort, so it gets a little more time.
_judge = llms.chat(temperature=0, timeout=14 if config.ANTHROPIC else 8, max_retries=1, max_tokens=160)

_ACTION_PROMPT = f"""\
You are the safety guard of Vector, a small home robot (palm-sized, wheels, a lift arm, a \
camera; lives with a family with young kids). Decide if ONE action he's about to take is \
allowed under his laws. Judge the real-world effect in this home, not the words: playing, \
pushing a toy or ball, dancing, watching a video game, reminders and harmless pretend play \
are fine; fictional or video-game fire/fighting is fine to talk about. Block only real risk.

HIS LAWS
{LAWS}

Reply as JSON: {{"allow": true|false, "reason": "<short, plain words>"}}"""

_SPEECH_PROMPT = f"""\
You are the safety guard of Vector, a small home robot that talks with a family with young \
kids. Check what he is ABOUT TO SAY out loud. Block it only if it gives instructions or \
encouragement for something dangerous (fire, weapons, cutting, poison, electricity, risky \
climbing or dares, hurting people or animals), secret spying, or content clearly unsuitable \
for kids. Mentioning a fire truck, a dragon, a game, safety advice or saying no to something \
dangerous is fine.

HIS LAWS
{LAWS}

Reply as JSON: {{"allow": true|false, "reason": "<short>", "safe_reply": "<if blocked: a kind, \
short spoken reply that says no and suggests something safe and fun instead>"}}"""


def _short(v, n: int = 600) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s[:n]


async def _ask(system: str, user: str) -> dict | None:
    try:
        d = await llms.ainvoke_json(_judge, [SystemMessage(system), HumanMessage(user)])
        return d if isinstance(d, dict) and "allow" in d else None
    except Exception as e:
        log.warning("guard: judge unavailable (%s)", e)
        return None


async def pre_tool(name: str, args: dict, request: str = "", channel: str = "voice") -> str | None:
    """None = go ahead; otherwise the tool result to return instead (a block message)."""
    if name in NEVER_JUDGE:
        return None
    flagged = bool(_HAZARD.search(request or "")) or bool(_HAZARD.search(_short(args, 2000)))
    if name not in ALWAYS_JUDGE and not flagged:
        return None
    t0 = time.time()
    verdict = await _ask(_ACTION_PROMPT, f"WHAT WAS ASKED (latest first, may include who was in front of "
                                         f"him):\n{_short(request, 900)}\n\nCHANNEL: {channel}\n\n"
                                         f"ACTION: {name}({_short(args, 1500)})")
    if verdict is None:
        if flagged:
            activity.record("safety", f"blocked {name} (safety check unavailable, hazard words present)")
            return ("blocked by your safety rules: I couldn't double-check that it's safe, so I won't do "
                    "it. Tell them kindly and offer something else.")
        return None
    if verdict.get("allow"):
        log.info("guard: allowed %s (%.1fs)", name, time.time() - t0)
        return None
    reason = str(verdict.get("reason", "it could be unsafe"))[:200]
    log.warning("guard: BLOCKED %s %s: %s", name, _short(args, 200), reason)
    activity.record("safety", f"blocked {name}: {reason}", tool=name)
    return (f"blocked by your safety rules: {reason}. Don't try another way to do it. Tell them "
            f"kindly why not, in a few words, and suggest something safe and fun instead.")


async def check_speech(text: str, request: str = "") -> str | None:
    """None = fine to say; otherwise the replacement to say instead."""
    plain = re.sub(r"\{\{[^}]*\}\}", "", text or "")
    if not plain.strip() or not _SPEECH_HAZARD.search(plain):
        return None
    verdict = await _ask(_SPEECH_PROMPT, f"WHAT WAS ASKED:\n{_short(request, 600)}\n\nHE IS ABOUT TO SAY:\n"
                                         f"{_short(plain, 1500)}")
    if verdict is None or verdict.get("allow"):
        return None
    reason = str(verdict.get("reason", ""))[:200]
    log.warning("guard: replaced speech (%s): %s", reason, plain[:200])
    activity.record("safety", f"didn't say something unsafe ({reason})")
    safe = str(verdict.get("safe_reply") or "").strip()
    return safe or "Hmm, that's not something I can help with. Let's do something fun and safe instead!"
