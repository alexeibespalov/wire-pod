"""Cortana's side of her notes: how a spoken line is labelled (she can't see who speaks)
and the per-turn writer that jots lasting facts down.

The notes themselves (storage, remember/forget, what is already believed) are shared with
Vector: agent_core.notes. With the belief store on they are TODAY'S NOTES, filed as beliefs
in her nightly run (app/compaction.py -> agent_core.reconcile).
"""
import logging
import re
from datetime import datetime

from . import config
from agent_core import notes
from agent_core.notes import (FORGET_PREFIX, MAX_FACTS, _LOCK, _load, _norm, _save, add,  # noqa: F401
                              all_facts, apply_plan, forget, known_beliefs_block, listing)

log = logging.getLogger("cortana-agent")
_llm = notes.writer_llm


# --------------------------------------------------------------- automatic memory writer
# After every voice turn a background pass (no added reply latency) looks at the exchange
# and the saved facts and decides what to add, update or drop.
_WRITER_PROMPT = """\
You maintain the long-term memory of {name}, the voice assistant of a family home \
(grown-ups and kids). Below: the facts already known (numbered) and the latest exchange. \
{name} can't see who is speaking, so only attribute a fact to a person when the speaker \
said who they are or talked about that person by name; otherwise write "someone in the \
family".

Decide what to change so the memory stays useful:
- ADD lasting facts that would still matter next week: names and who's who (family, \
friends, pets), ages, birthdays, favourites, hobbies, routines (school, clubs, work), plans \
and upcoming events (with the day/date, e.g. "on Friday 27 November"), dislikes, house \
rules, preferences about how {name} should behave.
- UPDATE a fact that changed: give its number and the new text.
- REMOVE a fact that is now wrong, or a plan that's clearly over.
- Speech-to-text sometimes garbles words into fake names: only record a name if someone \
clearly introduced or talked about that person.
- ONLY what was actually SAID. Never infer or guess.
- Only what the PEOPLE said about themselves and the home. Never save what {name} read \
out or looked up: calendar events, shopping-list items, web or weather answers, device \
states. Those live in the calendar / list / internet already.
- Do NOT save: small talk, moods, questions, timers/reminders (they're kept elsewhere), \
secrets someone wants kept from others, or anything already saved.
Each fact: one short third-person sentence. Most exchanges need no change at all.

Today is {today}.
{beliefs}
KNOWN FACTS:
{facts}

LATEST EXCHANGE:
{said}
{upper}: {reply}

Reply as JSON: {{"add": ["..."], "update": [{{"n": 3, "fact": "..."}}], "remove": [5]}} \
(empty lists when nothing changes)."""

_NOTE = re.compile(r"^\s*\(heard ([^)]*)\)\s*", re.S)


def speaker_line(said: str) -> str:
    """'(heard Sun 4:15 PM)\\nmy birthday...' -> 'SOMEONE (Sun 4:15 PM): my birthday...'"""
    m = _NOTE.match(said)
    if not m:
        return f"SOMEONE: {said.strip()}"
    when = m.group(1).split(";")[0].strip()
    return f"SOMEONE ({when}): {said[m.end():].strip()}"


async def reflect(said: str, reply: str) -> list[str]:
    """Background: update memory from one exchange. Returns what changed (for the log)."""
    from langchain_core.messages import HumanMessage

    from . import llm as llms
    body = _NOTE.sub("", said).strip()
    if len(body.split()) < 4:            # "stop", "yes", "what?" ... nothing to learn
        return []
    today = datetime.now(config.TIMEZONE).strftime("%A %-d %B %Y")
    name = config.ASSISTANT_NAME
    try:
        plan = await llms.ainvoke_json(_llm(), [HumanMessage(_WRITER_PROMPT.format(
            name=name, upper=name.upper(), today=today, facts=listing(), beliefs=known_beliefs_block(),
            said=speaker_line(said)[-800:], reply=reply[-600:]))])
    except ValueError:
        return []
    return apply_plan(plan) if isinstance(plan, dict) else []


notes.speaker_line = speaker_line       # the reconcile job reads transcripts with her labels
