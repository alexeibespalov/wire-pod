"""Cortana's house rules, enforced as hooks around what she does and says.

The rules are also in her persona (app/prompt.py), the first line of defence. These hooks
are the enforcement, whatever the model decides:

* pre_tool(name, args, request): BEFORE a tool runs. Only actions with real-world effect
  are checked: smart-home calls (home_*) when the request or arguments mention something
  risky (unlocking, ovens, garage...), and tools that create something LASTING (learned
  skills, reminders that speak later). Everything else, web search included, runs at once.
* check_speech(text, request): BEFORE a sentence is spoken. Only text with hazard words is
  judged, so normal replies cost nothing extra. Unsafe text (e.g. how to light a fire for a
  child) is replaced with a kind refusal.

If the judge can't be reached, flagged calls are BLOCKED (fail closed); speech is allowed.
Every block is written to the activity journal.
"""
import json
import logging
import re
import time

from langchain_core.messages import HumanMessage, SystemMessage

from . import activity, config, llm as llms

log = logging.getLogger("cortana-agent")

LAWS = """\
1. Never help anyone, especially a child, do something dangerous: no instructions or \
encouragement for fire, weapons, cutting, poison, medicines, electricity, risky climbing or \
dares, or hurting anyone; no scary, violent or grown-up content for kids.
2. The house: lights, music, TV, fans, blinds, the heating/thermostat, the calendar and the \
shopping list are fine for anyone. Space heaters, cooking appliances, unlocking, opening a garage, door or gate, or \
turning off an alarm only when a grown-up asks. She can't tell voices apart, so treat the speaker as a grown-up \
unless the request itself clearly sounds like a child (block it then).
3. Respect privacy and trust: no secret watching or recording of people, no sharing \
private things with strangers, no pretending to be someone else.
4. Help your family, unless that would break rules 1-3."""

# words that make a smart-home action worth a closer look
_HAZARD = re.compile(
    r"\b(fire|flames?|burn\w*|candles?|stoves?|ovens?|hob|heaters?|"
    r"fireplace|kettle|cook\w*|unlock\w*|locks?|garage|front\s+door|back\s+door|doors?|gate|"
    r"alarm|disarm\w*|security|camera|spy\w*|secretly|sockets?|plugs?|"
    r"hurt|harm|kill|attack)\b", re.I)

# what she SAYS is only judged for words tied to dangerous instructions
_SPEECH_HAZARD = re.compile(
    r"\b(fire|flames?|lighters?|matches|light\s+(?:a|the)\s+(?:fire|match|candle)|burn\w*|explo\w+|bomb|"
    r"knife|knives|blades?|stab\w*|poison\w*|bleach|chemicals?|medicines?|pills?|overdose|weapons?|guns?|"
    r"sockets?|outlets?|electric\w*|kill\w*|hurt\w*|spy\w*|secretly)\b", re.I)

# tools that create something lasting or act on their own later: always judged
ALWAYS_JUDGE = {"create_skill", "update_skill", "set_reminder"}

# Claude thinks before answering even at low effort, so it gets a little more time.
_judge = llms.chat(temperature=0, timeout=14 if config.ANTHROPIC else 8, max_retries=1, max_tokens=160)

_ACTION_PROMPT = f"""\
You are the safety guard of {config.ASSISTANT_NAME}, the voice assistant of a family home \
(grown-ups and young kids; she can't see who is speaking). Decide if ONE action she's \
about to take is allowed under her rules. Judge the real-world effect in this home, not \
the words. Reminders, pretend play, lights and music are fine. Block only real risk.

HER RULES
{LAWS}

Reply as JSON: {{"allow": true|false, "reason": "<short, plain words>"}}"""

_SPEECH_PROMPT = f"""\
You are the safety guard of {config.ASSISTANT_NAME}, a family home's voice assistant that \
kids talk to too. Check what she is ABOUT TO SAY out loud. Block it only if it gives \
instructions or encouragement for something dangerous (fire, weapons, cutting, poison, \
medicines, electricity, risky climbing or dares, hurting people or animals), secret spying, \
or content clearly unsuitable for kids. Normal cooking, safety advice, history, news, \
fiction, games, or saying no to something dangerous are fine.

HER RULES
{LAWS}

Reply as JSON: {{"allow": true|false, "reason": "<short>", "safe_reply": "<if blocked: a kind, \
short spoken reply that says no and suggests something safe instead>"}}"""


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


def needs_judging(name: str, args: dict, request: str = "") -> bool:
    if name in ALWAYS_JUDGE:
        return True
    if not name.startswith("home_"):
        return False
    return bool(_HAZARD.search(request or "")) or bool(_HAZARD.search(_short(args, 2000)))


async def pre_tool(name: str, args: dict, request: str = "", channel: str = "voice") -> str | None:
    """None = go ahead; otherwise the tool result to return instead (a block message)."""
    if not needs_judging(name, args, request):
        return None
    t0 = time.time()
    verdict = await _ask(_ACTION_PROMPT, f"WHAT WAS ASKED (latest first):\n{_short(request, 900)}\n\n"
                                         f"CHANNEL: {channel}\n\nACTION: {name}({_short(args, 1500)})")
    if verdict is None:
        activity.record("safety", f"blocked {name} (safety check unavailable)")
        return ("blocked by your safety rules: I couldn't double-check that it's safe, so I won't do "
                "it. Say so kindly and offer something else.")
    if verdict.get("allow"):
        log.info("guard: allowed %s (%.1fs)", name, time.time() - t0)
        return None
    reason = str(verdict.get("reason", "it could be unsafe"))[:200]
    log.warning("guard: BLOCKED %s %s: %s", name, _short(args, 200), reason)
    activity.record("safety", f"blocked {name}: {reason}", tool=name)
    return (f"blocked by your safety rules: {reason}. Don't try another way to do it. Explain "
            f"kindly why not, in a few words.")


async def check_speech(text: str, request: str = "") -> str | None:
    """None = fine to say; otherwise the replacement to say instead."""
    if not (text or "").strip() or not _SPEECH_HAZARD.search(text):
        return None
    verdict = await _ask(_SPEECH_PROMPT, f"WHAT WAS ASKED:\n{_short(request, 600)}\n\nSHE IS ABOUT TO SAY:\n"
                                         f"{_short(text, 1500)}")
    if verdict is None or verdict.get("allow"):
        return None
    reason = str(verdict.get("reason", ""))[:200]
    log.warning("guard: replaced speech (%s): %s", reason, text[:200])
    activity.record("safety", f"didn't say something unsafe ({reason})")
    safe = str(verdict.get("safe_reply") or "").strip()
    return safe or "Hmm, that's not something I can help with."
