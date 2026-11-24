"""Cortana's nightly chores (config.NIGHTLY_AT, see nightly()).

The folding itself (rolling summary, reading threads back, pruning checkpoints) is shared
with Vector: agent_core.compaction. Here: WHEN she does it, and what happens to her notes:
with the belief store on they are filed as beliefs (agent_core.reconcile); without it the
old harvest-and-tidy pass keeps the notes file in shape.
"""
import asyncio
import json
import time
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage

from . import beliefs, config, llm as llms
from agent_core import reconcile, sleepbatch
from agent_core.compaction import (_compact, _load, _prunable, _prune_sync, _recent_threads,  # noqa: F401
                                   _running, log, maybe_compact, prepare, tokens)

_RECONCILE_STATE = config.DATA_DIR / "reconcile_state.json"


async def compact_conversations() -> dict:
    """Overnight: fold each recent conversation down to a short tail (NIGHT_KEEP_TOKENS),
    so the next day starts with lean, fast turns and the day's story is in her notes."""
    model = llms.chat(timeout=120, max_retries=1, max_tokens=2000, background=True)
    done = {}
    for thread, saved in await _recent_threads():
        _summary, history = prepare(thread, saved)
        if tokens(history) <= config.NIGHT_KEEP_TOKENS + 500 or thread in _running:
            continue
        _running.add(thread)
        done[thread] = await _compact(thread, history, model, keep=config.NIGHT_KEEP_TOKENS)
    return {"folded_messages": done}


# ---------------------------------------------------------- memory harvest (nightly)
_HARVEST_PROMPT = """\
You harvest long-term memories for {name}, the voice assistant of a family home \
(grown-ups and kids), from today's conversations. She can't see who is speaking: only \
attribute a fact to a person when the speaker said who they are or talked about that \
person by name; otherwise "someone in the family". Ages and birthdays: be exact about whose.

List lasting facts that people actually SAID and that are NOT already known: names and \
who's who (family, friends, pets), ages, birthdays, favourites, hobbies, what they're into, \
plans and events (with dates; today is {today}), routines, house rules, how they want \
{name} to behave. If a known fact is now different (e.g. someone is older now), \
update it by its number instead of adding a new one. Only what was said: never infer or \
guess. Skip moods, small talk and questions, and never save what {name} read out or \
looked up (calendar events, shopping list, web answers): those live elsewhere. Each fact: \
one short sentence naming the person.

KNOWN FACTS:
{facts}

TODAY'S NOTES:
{notes}

TRANSCRIPT:
{transcript}

Reply as JSON: {{"add": ["..."], "update": [{{"n": 3, "fact": "..."}}]}}"""

_TIDY_PROMPT = """\
You tidy the long-term memory of {name}, a family home's voice assistant. Today \
is {today}. Look at the numbered facts and fix them:
- DUPLICATES (same thing said twice, even in different words): keep one (update it to the \
best wording) and remove the others.
- CONTRADICTIONS: keep the most recent / most specific, remove the other (e.g. "Maya is \
three years old" + "Maya turned four" -> keep one fact "Maya is four years old"; ages and \
favourites change, so the newer one wins).
- PLANS THAT ARE OVER (their date is before today): rewrite as a past fact ("went to \
Spain in November 2026") or remove if it no longer matters.
Change nothing else. If everything is fine, return empty lists.

FACTS:
{facts}

Reply as JSON: {{"update": [{{"n": 1, "fact": "..."}}], "remove": [2]}}"""


def _harvest_state() -> dict:
    try:
        return json.loads((config.DATA_DIR / "memory_harvest.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _harvest_has_work() -> bool:
    try:
        last_talk = json.loads((config.DATA_DIR / "thread.json").read_text()).get("last", 0)
    except (FileNotFoundError, ValueError):
        return False
    return last_talk > _harvest_state().get("at", 0)


async def harvest_memories() -> dict:
    """Before the day's conversations are folded away (compact_conversations runs after):
    pick up lasting facts the per-turn memory writer missed, and tidy the memory."""
    from . import activity, memory
    st = _harvest_state()
    seen = set(st.get("seen_ids", []))
    lines, new_ids = [], []
    notes = []
    for thread, saved in await _recent_threads(days=1.5):
        if (e := _load().get(thread)) and e.get("summary"):
            notes.append(e["summary"])
        for m in saved:
            if m.id in seen or not isinstance(m, (HumanMessage, AIMessage)):
                continue
            new_ids.append(m.id)
            if said := llms.text(m).strip():
                lines.append(memory.speaker_line(said)[:500] if isinstance(m, HumanMessage)
                             else f"{config.ASSISTANT_NAME.upper()}: {said[:400]}")
    today = datetime.now(config.TIMEZONE).strftime("%A %-d %B %Y")

    listing = memory.listing
    model = llms.chat(temperature=0, timeout=90, max_retries=1, max_tokens=900, background=True)

    async def ask(prompt: str) -> dict:
        try:
            d = await llms.ainvoke_json(model, [HumanMessage(prompt)])
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def save(at: float, ids: list, summary: str, harvested: bool) -> None:
        (config.DATA_DIR / "memory_harvest.json").write_text(json.dumps(
            {"at": at, "seen_ids": ids[-4000:], "last_summary": summary,
             "harvested_at": time.time() if harvested else st.get("harvested_at", 0)}))

    name = config.ASSISTANT_NAME
    changed = []
    if lines or notes:                                   # 1. harvest new facts
        plan = await ask(_HARVEST_PROMPT.format(
            name=name, today=today, facts=listing(), notes="\n\n".join(notes)[-3000:] or "(none)",
            transcript="\n".join(lines)[-14000:] or "(nothing new)"))
        changed += memory.apply_plan({"add": plan.get("add"), "update": plan.get("update")}, max_add=10)
    if memory.all_facts():                               # 2. tidy the whole memory
        plan = await ask(_TIDY_PROMPT.format(name=name, today=today, facts=listing()))
        changed += memory.apply_plan({"update": plan.get("update"), "remove": plan.get("remove")})
    summary = "; ".join(changed)[:300]
    for c in changed:
        activity.record("memory", f"(nightly) {c}")
    save(time.time(), st.get("seen_ids", []) + new_ids, summary, True)
    log.info("nightly: memory harvest: %s", changed or "no change")
    return {"changed": changed, "summary": summary}


async def prune_checkpoints() -> dict:
    if not _prunable():
        return {}
    res = await asyncio.to_thread(_prune_sync)
    log.info("nightly: pruned conversation checkpoints %s", res)
    return res


# ------------------------------------------------------------ notes -> beliefs
class _Ctx:
    """What agent_core.reconcile needs from its host: a state dict that survives between
    runs, a time budget, and a log. (Vector passes his sleep job's context instead.)"""

    def __init__(self, budget_s: float = 300):
        try:
            self.state = json.loads(_RECONCILE_STATE.read_text())
        except (OSError, ValueError):
            self.state = {}
        self._deadline = time.time() + budget_s
        self.log = log

    def alive(self) -> bool:
        return time.time() < self._deadline

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(min(s, max(0.0, self._deadline - time.time())))

    def save(self) -> None:
        tmp = _RECONCILE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(_RECONCILE_STATE)


def reconcile_has_work() -> bool:
    return reconcile.has_work(_Ctx(0).state)


async def reconcile_beliefs() -> dict:
    """File her notes and the day's talk as beliefs, then rule on what is unsettled. A
    result with "waiting" means the batch answer or the NUC wasn't ready: run it again."""
    ctx = _Ctx()
    try:
        return await reconcile.run(ctx)
    finally:
        ctx.save()
        try:
            await sleepbatch.flush()            # send what was queued for the batch model
        except Exception as e:
            log.warning("nightly: batch submit failed: %s", e)


async def nightly() -> dict:
    """The night's chores, in order (only if there was any conversation since last night)."""
    from . import activity
    out = {}
    memory_job = (("reconcile_beliefs", reconcile_beliefs) if beliefs.ENABLED
                  else ("harvest_memories", harvest_memories))
    if _harvest_has_work() or (beliefs.ENABLED and reconcile_has_work()):
        for name, job in (memory_job, ("compact_conversations", compact_conversations)):
            try:
                out[name] = await job()
            except Exception as e:
                log.exception("nightly: %s failed", name)
                out[name] = f"error: {e}"
    try:
        out["prune_checkpoints"] = await prune_checkpoints()
    except Exception as e:
        log.warning("nightly: prune failed: %s", e)
    activity.record("nightly", f"nightly chores: {', '.join(out) or 'nothing to do'}")
    return out
