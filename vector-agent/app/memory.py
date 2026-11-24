"""Vector's side of his notes: how a spoken line is labelled with who said it (his face
recognition), overheard talk, and the per-turn writer that jots lasting facts down.

The notes themselves (storage, remember/forget, what is already believed) are shared with
Cortana: agent_core.notes. With the belief store on they are TODAY'S NOTES, filed as
beliefs while he sleeps (app/belief_sleep.py -> agent_core.reconcile).
"""
import re
from datetime import datetime

from . import config
from agent_core import notes
from agent_core.notes import (FORGET_PREFIX, MAX_FACTS, _LOCK, _load, _norm, _save, add,  # noqa: F401
                              all_facts, apply_plan, forget, known_beliefs_block, listing)

_llm = notes.writer_llm


# --------------------------------------------------------------- automatic memory writer
# A small model often won't call `remember` on its own mid-conversation. So after every
# voice turn a background pass (no added reply latency) looks at the exchange and the
# saved facts and decides what to add, update or drop, the way a friend would.
_WRITER_PROMPT = """\
You maintain the long-term memory of Vector, a small home robot who lives with a family \
(grown-ups and kids). Below: the facts he already knows (numbered) and the latest exchange. \
The person's line starts with their name (from his face recognition) or SOMEONE; use it \
unless they said who they are. Attribute every fact to the person who said it.

Decide what to change so his memory stays like a good friend's:
- ADD lasting facts that would still matter next week: names and who's who (siblings, \
friends, pets), ages, birthdays, favourites, hobbies, what they're learning or into, plans \
and upcoming events (with the day/date, e.g. "on Friday 27 November"), fears and dislikes, \
achievements, house rules, names they gave Vector's things.
- UPDATE a fact that changed (e.g. a new favourite game): give its number and the new text.
- REMOVE a fact that is now wrong, or a plan that's clearly over.
- Speech-to-text often garbles words into fake names ("look down at" -> "anna does"): only \
record a person's name if someone clearly introduced or talked about them.
- ONLY what the person actually SAID. Never infer, guess or extrapolate: asking "what's two \
plus two?" does not mean they're learning addition; "my little sister" says nothing about \
brothers; liking one game doesn't mean they own a console.
- Do NOT save: small talk, moods or states that only apply now ("tired", "hungry"), \
questions, things about the robot's own actions, secrets someone wants kept from others, \
or anything already saved (Vector may have just saved it himself; it's in the list).
Each fact: one short third-person sentence that names the person ("Leo's favourite game \
is Minecraft"). If a name is unknown, say "a child" or "someone". Most exchanges need no \
change at all.
{overheard_rule}

Today is {today}.
{beliefs}
KNOWN FACTS:
{facts}

LATEST EXCHANGE:
{said}
VECTOR: {reply}

Reply as JSON: {{"add": ["..."], "update": [{{"n": 3, "fact": "..."}}], "remove": [5]}} \
(empty lists when nothing changes)."""

_writer = None

OVERHEARD_RULE = """\
- A line marked OVERHEARD is a fragment of people talking near Vector (a call, a meeting, \
the TV) that nobody said to him, often misheard. Never turn it into a fact about anyone. \
If, and only if, the topic is clear, you may add ONE note about it that starts with \
"Overheard" and the day, e.g. "Overheard (Mon 23 November): grown-ups talking about a work \
project on machines and unknown languages". Topic only: no names, numbers, quotes or \
details, nothing private (health, money, arguments, someone's secrets), and nothing that \
sounds like TV or film dialogue. Skip it if a similar Overheard note exists."""

# A voice turn he judged "not for me" (wake word misfired on people talking nearby: a call,
# a meeting, the TV). The heard text is kept, tagged, so later readers (Vector himself, the
# memory writers, the nightly summary) know it's a misheard fragment nobody said to him.
OVERHEARD_TAG = "(overheard, not said to you)"


def tag_overheard_in(messages: list) -> list:
    """The turn's (last) human message, tagged as overheard (same id: replaces it)."""
    from langchain_core.messages import HumanMessage
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage):
            if isinstance(m.content, str) and OVERHEARD_TAG not in m.content:
                messages = list(messages)
                messages[i] = HumanMessage(content=tag_overheard(m.content), id=m.id)
            break
    return messages


def tag_overheard(said: str) -> str:
    """'(eyes: ...)\nwords' -> '(eyes: ...)\n(overheard, not said to you) words'."""
    if OVERHEARD_TAG in said:
        return said
    head, sep, body = said.partition("\n")
    if sep and head.lstrip().startswith("(eyes:"):
        return f"{head}\n{OVERHEARD_TAG} {body}"
    return f"{OVERHEARD_TAG} {said}"


_NOTE = re.compile(r"^\s*\(eyes:([^)]*)\)\s*", re.S)


def speaker_line(said: str) -> str:
    """'(eyes: Leo is right in front of you; Sun 4:15 PM)\nmy birthday...' ->
    'LEO (Sun 4:15 PM): my birthday...' so the model can't mix up who said what."""
    m = _NOTE.match(said)
    if not m:
        return f"SOMEONE: {said.strip()}"
    note, text = m.group(1), said[m.end():].strip()
    when = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w* [\d:]+ [AP]M", note)
    if text.startswith(OVERHEARD_TAG):
        return (f"OVERHEARD{f' ({when.group(0)})' if when else ''}, not said to Vector, "
                f"partial and maybe misheard: {text[len(OVERHEARD_TAG):].strip()}")
    who = re.match(r"\s*([A-Z][\w-]*) is right in front of you", note)
    name = who.group(1).upper() if who else "SOMEONE (face not recognised)"
    return f"{name}{f' ({when.group(0)})' if when else ''}: {text}"


async def reflect(said: str, reply: str) -> list[str]:
    """Background: update memory from one exchange. Returns what changed (for the log)."""
    import json
    import re
    from langchain_core.messages import HumanMessage
    body = re.sub(r"^\(eyes:[^)]*\)\s*", "", said.split("\n", 1)[-1] if "\n" in said else said).strip()
    body = body.replace(OVERHEARD_TAG, "").strip()
    if len(body.split()) < 4:            # "stop", "yes", "what?" ... nothing to learn
        return []
    listing = notes.listing()
    today = datetime.now(config.TIMEZONE).strftime("%A %-d %B %Y")
    from . import llm as llms
    try:
        plan = await llms.ainvoke_json(_llm(), [HumanMessage(_WRITER_PROMPT.format(
            overheard_rule=OVERHEARD_RULE, today=today, facts=listing, beliefs=known_beliefs_block(),
            said=speaker_line(said)[-800:],
            reply=re.sub(r"\{\{[^}]*\}\}", "", reply)[-600:]))])
    except ValueError:
        return []
    return apply_plan(plan)


notes.speaker_line = speaker_line       # the reconcile job reads transcripts with his labels
