"""Long conversations: a rolling summary instead of forgetting.

The agent sees: system prompt (+ "the conversation so far" summary) + the recent messages.
When a thread's history grows past HISTORY_TOKENS, a background task (after the turn,
so nobody waits for it) folds the oldest messages into the summary until about
KEEP_TOKENS of recent conversation is left. The summary lives in DATA_DIR/summaries.json
as {thread_id: {"summary", "upto", "updated"}}; "upto" is the id of the last message it
covers, so the next turn drops everything up to it from the history it loads.

If summarising fails or lags, a hard cap (2 x HISTORY_TOKENS) still trims the oldest
messages so the model's context never overflows.

Also a sleep job that prunes old LangGraph checkpoints: the saver stores the whole
message list at every step, and we only ever need the latest one per thread.
"""
import asyncio
import contextvars
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from . import config, llm as llms, sleep_routine, sleepbatch

log = logging.getLogger("vector-agent")

_STORE = config.DATA_DIR / "summaries.json"
_LOCK = threading.Lock()
_running: set[str] = set()
_tasks: set[asyncio.Task] = set()

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
or two. Most recent things last. At most {words} words. No preamble."""


def _text(m: AnyMessage) -> str:
    # spoken text only: Claude's content blocks carry thinking signatures that aren't
    # conversation (and would make the history look far longer than it is)
    c = llms.text(m) if isinstance(m, AIMessage) else (
        m.content if isinstance(m.content, str) else json.dumps(m.content))
    if isinstance(m, AIMessage) and m.tool_calls:
        c += " " + json.dumps([{"name": t["name"], "args": t["args"]} for t in m.tool_calls])
    return c


def tokens(msgs: list[AnyMessage]) -> int:
    return sum(len(_text(m)) for m in msgs) // 4 + 4 * len(msgs)


def _load() -> dict:
    try:
        return json.loads(_STORE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_entry(thread: str, entry: dict) -> None:
    with _LOCK:
        data = _load()
        data[thread] = entry
        tmp = _STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        tmp.replace(_STORE)


def _start_on_human(msgs: list[AnyMessage]) -> list[AnyMessage]:
    for i, m in enumerate(msgs):
        if isinstance(m, HumanMessage):
            return msgs[i:]
    return []


def prepare(thread: str, previous: list[AnyMessage]) -> tuple[str, list[AnyMessage]]:
    """(summary text or "", the history the model should see)."""
    entry = _load().get(thread) or {}
    summary, upto = entry.get("summary", ""), entry.get("upto")
    msgs = previous
    if upto:
        ids = [m.id for m in msgs]
        if upto in ids:
            msgs = _start_on_human(msgs[ids.index(upto) + 1:])
    if tokens(msgs) > 2 * config.HISTORY_TOKENS:      # summariser behind or failing
        while msgs and tokens(msgs) > config.HISTORY_TOKENS:
            msgs = _start_on_human(msgs[1:])
        log.warning("[%s] history over the hard cap: dropped old messages unsummarised", thread)
    return summary, msgs


def maybe_compact(thread: str, messages: list[AnyMessage], llm) -> None:
    """Called at the end of a turn with the history about to be saved."""
    if tokens(messages) <= config.HISTORY_TOKENS or thread in _running:
        return
    _running.add(thread)
    # a fresh context: the summary call must not stream into the finished turn's output
    t = asyncio.get_running_loop().create_task(_compact(thread, list(messages), llm),
                                               context=contextvars.Context())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _render(msgs: list[AnyMessage]) -> str:
    out = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            out.append(f"HUMAN {llms.text(m)}")
        elif isinstance(m, AIMessage):
            if said := llms.text(m):
                out.append(f"VECTOR: {said}")
            for t in m.tool_calls or []:
                out.append(f"  [Vector used {t['name']} {json.dumps(t['args'], ensure_ascii=False)[:200]}]")
        elif isinstance(m, ToolMessage):
            out.append(f"  [result: {str(m.content)[:300]}]")
    return "\n".join(out)


async def _compact(thread: str, messages: list[AnyMessage], llm, keep: int | None = None) -> int:
    """Fold all but about `keep` tokens of recent conversation into the summary.
    Returns how many messages were folded."""
    keep = config.KEEP_TOKENS if keep is None else keep
    try:
        # keep about `keep` tokens of the most recent conversation, starting on a human turn
        cut, kept = len(messages), 0
        while cut > 0 and kept < keep:
            cut -= 1
            kept += tokens([messages[cut]])
        while cut < len(messages) and not isinstance(messages[cut], HumanMessage):
            cut += 1
        old = messages[:cut]
        if not old:
            return 0
        prev = (_load().get(thread) or {})
        # only fold in what the current summary doesn't already cover
        if prev.get("upto") in [m.id for m in old]:
            ids = [m.id for m in old]
            old = old[ids.index(prev["upto"]) + 1:]
            if not old:
                return 0
        notes = prev.get("summary") or "(no notes yet)"
        t0 = asyncio.get_running_loop().time()
        res = await llm.ainvoke([
            SystemMessage(SUMMARY_PROMPT.format(words=config.SUMMARY_WORDS)),
            HumanMessage(f"NOTES SO FAR:\n{notes}\n\nNEXT PART OF THE TRANSCRIPT:\n{_render(old)}"),
        ])
        summary = llms.text(res).strip()
        if not summary:
            raise ValueError("empty summary")
        _save_entry(thread, {"summary": summary, "upto": old[-1].id,
                             "updated": datetime.now(config.TIMEZONE).isoformat(timespec="seconds")})
        log.info("[%s] compacted %d old messages into the summary (%.1fs, %d words)", thread,
                 len(old), asyncio.get_running_loop().time() - t0, len(summary.split()))
        return len(old)
    except sleepbatch.Pending:
        log.info("[%s] summary queued for the sleep batch", thread)
        return 0
    except Exception as e:
        log.warning("[%s] couldn't summarise old messages (%s); will retry later", thread, e)
        return 0
    finally:
        _running.discard(thread)


# ------------------------------------------------------------------- while asleep
async def _recent_threads(days: float = 2) -> list[tuple[str, list[AnyMessage]]]:
    """(thread, saved history) for threads active in the last few days."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    db = str(config.DATA_DIR / "checkpoints.sqlite")
    ids = await asyncio.to_thread(lambda: [r[0] for r in sqlite3.connect(db, timeout=5).execute(
        "select distinct thread_id from checkpoints").fetchall()])
    out, now = [], datetime.now().astimezone()
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        for t in ids:
            tup = await saver.aget_tuple({"configurable": {"thread_id": t}})
            if not tup:
                continue
            try:
                age = (now - datetime.fromisoformat(tup.checkpoint["ts"])).total_seconds()
            except (KeyError, ValueError):
                continue
            msgs = tup.checkpoint.get("channel_values", {}).get("__previous__")
            if age <= days * 86400 and isinstance(msgs, list) and msgs:
                out.append((t, msgs))
    return out


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


@sleep_routine.job("harvest_memories", order=70, budget_s=240, has_work=_harvest_has_work)
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
                today=today, facts=listing(), notes="\n\n".join(notes)[-3000:] or "(none)",
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
def _prune_sync() -> dict:
    """Keep only each thread's newest checkpoint (we never time-travel)."""
    db = sqlite3.connect(str(config.DATA_DIR / "checkpoints.sqlite"), timeout=15)
    try:
        before = db.execute("select count(*) from checkpoints").fetchone()[0]
        keep = db.execute("""select thread_id, checkpoint_ns, max(checkpoint_id) from checkpoints
                             group by thread_id, checkpoint_ns""").fetchall()
        db.execute("create temp table keep(thread_id, checkpoint_ns, checkpoint_id)")
        db.executemany("insert into keep values (?,?,?)", keep)
        db.execute("""delete from checkpoints where not exists (select 1 from keep k where
                      k.thread_id=checkpoints.thread_id and k.checkpoint_ns=checkpoints.checkpoint_ns
                      and k.checkpoint_id=checkpoints.checkpoint_id)""")
        db.execute("""delete from writes where not exists (select 1 from keep k where
                      k.thread_id=writes.thread_id and k.checkpoint_ns=writes.checkpoint_ns
                      and k.checkpoint_id=writes.checkpoint_id)""")
        db.commit()
        after = db.execute("select count(*) from checkpoints").fetchone()[0]
        try:
            db.execute("vacuum")
        except sqlite3.OperationalError as e:       # busy: fine, next night
            log.info("checkpoints: vacuum skipped (%s)", e)
        return {"checkpoints_before": before, "checkpoints_after": after}
    finally:
        db.close()


def _prunable() -> bool:
    try:
        db = sqlite3.connect(str(config.DATA_DIR / "checkpoints.sqlite"), timeout=2)
        try:
            n, k = db.execute("select count(*), count(distinct thread_id || '/' || checkpoint_ns) "
                              "from checkpoints").fetchone()
        finally:
            db.close()
        return n - k > 50
    except sqlite3.Error:
        return False


@sleep_routine.job("prune_checkpoints", order=90, budget_s=120, has_work=_prunable)
async def prune_checkpoints(ctx) -> dict:
    res = await asyncio.to_thread(_prune_sync)
    ctx.log.info("sleep: pruned conversation checkpoints %s", res)
    return res
