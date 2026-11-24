"""Vector's overnight chores on his conversations, as sleep jobs.

The folding itself (rolling summary, reading threads back, pruning checkpoints) is shared
with Cortana: agent_core.compaction. Here: WHEN he does it (asleep, on Sonnet batches) and
the old notes-only memory harvest, which runs only when the belief store is off.
"""
import asyncio
import json
import time
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage

from . import config, llm as llms, sleep_routine, sleepbatch
from agent_core.compaction import (_compact, _load, _prunable, _prune_sync, _recent_threads,  # noqa: F401
                                   _running, log, maybe_compact, prepare, tokens)


@sleep_routine.job("compact_conversations", order=80, budget_s=300, has_work=lambda: _sleep_has_work())
async def compact_conversations(ctx) -> dict:
    """Overnight: fold each recent conversation down to a short tail (SLEEP_KEEP_TOKENS),
    so the next day starts with lean, fast turns and the day's story is in his notes."""
    from .agent import llm
    done = {}
    for thread, saved in await _recent_threads():
        if not ctx.alive():
            break
        _summary, history = prepare(thread, saved)
        if tokens(history) <= config.SLEEP_KEEP_TOKENS + 500 or thread in _running:
            continue
        _running.add(thread)
        done[thread] = await _compact(thread, history, sleepbatch.model(max_tokens=2000, direct=llm),
                                      keep=config.SLEEP_KEEP_TOKENS)
    _state["last_sleep_compact"] = datetime.now().timestamp()
    return {"folded_messages": done}


_state: dict = {}


def _sleep_has_work() -> bool:
    """Something to fold since the last overnight pass? (the voice thread's file says when
    he last talked)"""
    try:
        last_talk = json.loads((config.DATA_DIR / "thread.json").read_text()).get("last", 0)
    except (FileNotFoundError, ValueError):
        return False
    return last_talk > _state.get("last_sleep_compact", 0)


# ------------------------------------------------------------- memory harvest (sleep)
_HARVEST_PROMPT = """\
You harvest long-term memories for Vector, a small home robot who lives with a family \
(grown-ups and kids), from today's conversations. Each human line starts with the \
speaker's NAME (from his face recognition) and the time; attribute every fact to the person \
on THAT line (unless they said who they are). Ages and birthdays: be exact about whose.

List lasting facts that people actually SAID and that are NOT already known: names and \
who's who (family, friends, pets), ages, birthdays, favourites, hobbies, what they're into, \
plans and events (with dates; today is {today}), fears, achievements, house rules, names \
they gave Vector's things. If a known fact is now different (e.g. someone is older now), \
update it by its number instead of adding a new one. Only what was said: never infer or \
guess. Skip moods, small talk and questions. Each fact: one short sentence naming the person.
{overheard_rule}

KNOWN FACTS:
{facts}

TODAY'S NOTES:
{notes}

TRANSCRIPT:
{transcript}

Reply as JSON: {{"add": ["..."], "update": [{{"n": 3, "fact": "..."}}]}}"""

_TIDY_PROMPT = """\
You tidy the long-term memory of Vector, a small home robot who lives with a family. Today \
is {today}. Look at the numbered facts and fix them:
- DUPLICATES (same thing said twice, even in different words): keep one (update it to the \
best wording) and remove the others.
- CONTRADICTIONS: keep the most recent / most specific, remove the other (e.g. "Maya is \
three years old" + "Maya turned four" -> keep one fact "Maya is four years old"; ages and \
favourites change, so the newer one wins).
- PLANS THAT ARE OVER (their date is before today): rewrite as a past fact ("went to \
Spain in November 2026") or remove if it no longer matters.
- "Overheard" notes (bits of other people's conversations) older than a week: remove.
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


async def harvest_memories(ctx) -> dict:
    """Before the day's conversations are folded away (compact_conversations, order 80):
    pick up lasting facts the per-turn memory writer missed, and tidy the memory."""
    from . import activity, memory
    st = _harvest_state()
    seen = set(st.get("seen_ids", []))
    lines, new_ids = [], []
    notes = []
    for thread, saved in await _recent_threads(days=1.5):
        if thread.startswith(("a2a-", "sched-")):
            continue
        if (e := _load().get(thread)) and e.get("summary"):
            notes.append(e["summary"])
        for m in saved:
            if m.id in seen or not isinstance(m, (HumanMessage, AIMessage)):
                continue
            new_ids.append(m.id)
            if said := llms.text(m).strip():
                lines.append(memory.speaker_line(said)[:500] if isinstance(m, HumanMessage)
                             else f"VECTOR: {said[:400]}")
    today = datetime.now(config.TIMEZONE).strftime("%A %-d %B %Y")

    def listing():
        return "\n".join(f"{i + 1}. {f['fact']}" for i, f in enumerate(memory._load())) or "(none yet)"

    model = sleepbatch.model(max_tokens=900, direct=memory._llm())    # Sonnet via batches on Claude

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

    changed = []
    # With batches the two passes can land in different sleep sessions: once the harvest
    # pass is applied it isn't asked again until there's new conversation.
    harvested = st.get("harvested_at", 0) > st.get("at", 0)
    try:
        if (lines or notes) and not harvested:           # 1. harvest new facts
            plan = await ask(_HARVEST_PROMPT.format(
                overheard_rule=memory.OVERHEARD_RULE, today=today, facts=listing(), notes="\n\n".join(notes)[-3000:] or "(none)",
                transcript="\n".join(lines)[-14000:] or "(nothing new)"))
            changed += memory.apply_plan({"add": plan.get("add"), "update": plan.get("update")}, max_add=10)
            harvested = True
            new_ids, seen_list = [], st.get("seen_ids", []) + new_ids
            save(st.get("at", 0), seen_list, "; ".join(changed)[:300], True)
            st.update(json.loads((config.DATA_DIR / "memory_harvest.json").read_text()))
        if ctx.alive() and memory._load():               # 2. tidy the whole memory
            plan = await ask(_TIDY_PROMPT.format(today=today, facts=listing()))
            changed += memory.apply_plan({"update": plan.get("update"), "remove": plan.get("remove")})
    except sleepbatch.Pending:
        for c in changed:
            activity.record("memory", f"(sleep) {c}")
        return {"changed": changed, "waiting": "queued for the sleep batch"}
    plan = {"summary": "; ".join(changed)[:300]}
    for c in changed:
        activity.record("memory", f"(sleep) {c}")
    save(time.time(), st.get("seen_ids", []) + new_ids, plan.get("summary", ""), False)
    ctx.log.info("sleep: memory harvest: %s", changed or "no change")
    return {"changed": changed, "summary": plan.get("summary", "")}


# ------------------------------------------------------------ checkpoint housekeeping
# Notes-only memory (no belief store): this job is his long-term memory upkeep. With the
# belief store on, app/belief_sleep.py's reconcile_beliefs takes this slot instead.
from . import beliefs as _beliefs  # noqa: E402

if not _beliefs.ENABLED:
    sleep_routine.job("harvest_memories", order=70, budget_s=240, has_work=_harvest_has_work)(harvest_memories)


@sleep_routine.job("prune_checkpoints", order=90, budget_s=120, has_work=_prunable)
async def prune_checkpoints(ctx) -> dict:
    res = await asyncio.to_thread(_prune_sync)
    ctx.log.info("sleep: pruned conversation checkpoints %s", res)
    return res
