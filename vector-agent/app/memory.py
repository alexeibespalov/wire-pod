"""Long-term memory: a small JSON list of facts, injected into every system prompt.

Kept deliberately simple: Vector's memory is a few dozen facts about his people,
not a document store. Survives restarts (lives in the agent data volume).
"""
import json
import re
import threading
from datetime import datetime

from . import config

_PATH = config.DATA_DIR / "memories.json"
_LOCK = threading.Lock()
MAX_FACTS = 100


def _load() -> list[dict]:
    try:
        return json.loads(_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _save(facts: list[dict]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
    tmp.replace(_PATH)


def all_facts() -> list[str]:
    with _LOCK:
        return [f["fact"] for f in _load()]


def add(fact: str) -> str:
    fact = " ".join(fact.split())
    if not fact:
        return "nothing to remember"
    with _LOCK:
        facts = _load()
        if any(f["fact"].lower() == fact.lower() for f in facts):
            return "already remembered"
        facts.append({"fact": fact, "saved": datetime.now(config.TIMEZONE).isoformat(timespec="minutes")})
        _save(facts[-MAX_FACTS:])
    return "remembered"


def forget(match: str) -> str:
    match = match.lower().strip()
    with _LOCK:
        facts = _load()
        keep = [f for f in facts if match not in f["fact"].lower()]
        removed = len(facts) - len(keep)
        if removed:
            _save(keep)
    return f"forgot {removed} fact(s)" if removed else "no matching memory"


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

Today is {today}.

KNOWN FACTS:
{facts}

LATEST EXCHANGE:
{said}
VECTOR: {reply}

Reply as JSON: {{"add": ["..."], "update": [{{"n": 3, "fact": "..."}}], "remove": [5]}} \
(empty lists when nothing changes)."""

_writer = None


def _llm():
    global _writer
    if _writer is None:
        from . import llm as llms
        _writer = llms.chat(temperature=0, timeout=45, max_retries=1, max_tokens=900)
    return _writer


_NOTE = re.compile(r"^\s*\(eyes:([^)]*)\)\s*", re.S)


def speaker_line(said: str) -> str:
    """'(eyes: Leo is right in front of you; Sun 4:15 PM)\nmy birthday...' ->
    'LEO (Sun 4:15 PM): my birthday...' so the model can't mix up who said what."""
    m = _NOTE.match(said)
    if not m:
        return f"SOMEONE: {said.strip()}"
    note, text = m.group(1), said[m.end():].strip()
    who = re.match(r"\s*([A-Z][\w-]*) is right in front of you", note)
    name = who.group(1).upper() if who else "SOMEONE (face not recognised)"
    when = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w* [\d:]+ [AP]M", note)
    return f"{name}{f' ({when.group(0)})' if when else ''}: {text}"


async def reflect(said: str, reply: str) -> list[str]:
    """Background: update memory from one exchange. Returns what changed (for the log)."""
    import json
    import re
    from langchain_core.messages import HumanMessage
    body = re.sub(r"^\(eyes:[^)]*\)\s*", "", said.split("\n", 1)[-1] if "\n" in said else said).strip()
    if len(body.split()) < 4:            # "stop", "yes", "what?" ... nothing to learn
        return []
    with _LOCK:
        facts = _load()
    listing = "\n".join(f"{i + 1}. {f['fact']}" for i, f in enumerate(facts)) or "(none yet)"
    today = datetime.now(config.TIMEZONE).strftime("%A %-d %B %Y")
    from . import llm as llms
    try:
        plan = await llms.ainvoke_json(_llm(), [HumanMessage(_WRITER_PROMPT.format(
            today=today, facts=listing, said=speaker_line(said)[-800:],
            reply=re.sub(r"\{\{[^}]*\}\}", "", reply)[-600:]))])
    except ValueError:
        return []
    return apply_plan(plan)


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def apply_plan(plan: dict, max_add: int = 3) -> list[str]:
    """Apply {"add": [...], "update": [{"n", "fact"}], "remove": [n]} (1-based numbers
    referring to the fact list the model was shown). Returns what changed."""
    changed: list[str] = []
    now = datetime.now(config.TIMEZONE).isoformat(timespec="minutes")
    with _LOCK:
        facts = _load()
        n_old = len(facts)
        for u in plan.get("update") or []:
            try:
                i = int(u["n"]) - 1
                new = " ".join(str(u["fact"]).split())[:200]
            except (KeyError, ValueError, TypeError):
                continue
            new = re.sub(r"^\s*\d+[.)]\s*", "", new)
            if 0 <= i < n_old and new:
                changed.append(f"updated: {facts[i]['fact']} -> {new}")
                facts[i] = {"fact": new, "saved": now}
        drop = set()
        for r in plan.get("remove") or []:
            try:
                i = int(r) - 1
            except (ValueError, TypeError):
                continue
            if 0 <= i < n_old:
                drop.add(i)
                changed.append(f"forgot: {facts[i]['fact']}")
        facts = [f for i, f in enumerate(facts) if i not in drop]
        for a in (plan.get("add") or [])[:max_add]:
            a = re.sub(r"^\s*\d+[.)]\s*", "", " ".join(str(a).split()))[:200]
            if a and not any(_norm(f["fact"]) == _norm(a) for f in facts):
                facts.append({"fact": a, "saved": now})
                changed.append(f"saved: {a}")
        if changed:
            _save(facts[-MAX_FACTS:])
    return changed
