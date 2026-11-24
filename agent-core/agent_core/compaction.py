"""Long conversations: a rolling summary instead of forgetting.

The agent sees: system prompt (+ "the conversation so far" summary) + the recent messages.
When a thread's history grows past HISTORY_TOKENS, a background task (after the turn,
so nobody waits for it) folds the oldest messages into the summary until about
KEEP_TOKENS of recent conversation is left. The summary lives in DATA_DIR/summaries.json
as {thread_id: {"summary", "upto", "updated"}}; "upto" is the id of the last message it
covers, so the next turn drops everything up to it from the history it loads.

If summarising fails or lags, a hard cap (2 x HISTORY_TOKENS) still trims the oldest
messages so the model's context never overflows.

Also here: reading the recent threads back from the checkpoint store (for the nightly /
sleep jobs) and pruning old LangGraph checkpoints (the saver stores the whole message list
at every step, and only the latest one per thread is ever needed).

Per agent: config.SUMMARY_PROMPT (the note-keeper's instructions, with {words} and
optionally {name}). WHEN the folding and pruning run is the agent's business: Vector runs
them as sleep jobs, Cortana in her nightly run.
"""
import asyncio
import contextvars
import json
import logging
import sqlite3
import threading
from datetime import datetime

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from .settings import cfg as config
from . import llm as llms, sleepbatch

log = logging.getLogger(config.LOGGER_NAME)

_STORE = config.DATA_DIR / "summaries.json"
_LOCK = threading.Lock()
_running: set[str] = set()
_tasks: set[asyncio.Task] = set()


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
                out.append(f"{config.ASSISTANT_NAME.upper()}: {said}")
            for t in m.tool_calls or []:
                out.append(f"  [{config.ASSISTANT_NAME} used {t['name']} {json.dumps(t['args'], ensure_ascii=False)[:200]}]")
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
            SystemMessage(config.SUMMARY_PROMPT.format(name=config.ASSISTANT_NAME, words=config.SUMMARY_WORDS)),
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
