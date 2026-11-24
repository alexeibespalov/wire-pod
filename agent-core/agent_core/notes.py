"""Notes: a small JSON list of plain sentences, injected into every system prompt.

With the belief store on (agent_core/beliefs.py) these are TODAY'S NOTES: what the agent
jotted down since its last reconcile run, which files them as beliefs and clears them.
Without it they are its whole long-term memory. Survives restarts (DATA_DIR/memories.json).

Each note records when it was saved and which agent wrote it (`by`).

What stays with each agent (app/memory.py): the per-turn writer's prompt, and how a spoken
line is labelled with its speaker (`speaker_line`: Vector sees faces, Cortana does not).
An agent registers its speaker_line here so the reconcile job can read transcripts.
"""
import json
import re
import threading
from datetime import datetime

from .settings import cfg as config

_PATH = config.DATA_DIR / "memories.json"
_LOCK = threading.Lock()
MAX_FACTS = 100
FORGET_PREFIX = "Asked to forget"

speaker_line = lambda said: f"SOMEONE: {said.strip()}"      # noqa: E731  (the agent replaces this)
_writer = None


def _load() -> list[dict]:
    try:
        return json.loads(_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _save(facts: list[dict]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(facts[-MAX_FACTS:], indent=2, ensure_ascii=False))
    tmp.replace(_PATH)


def _row(fact: str) -> dict:
    return {"fact": fact, "saved": datetime.now(config.TIMEZONE).isoformat(timespec="minutes"),
            "by": config.ASSISTANT_NAME.lower()}


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def _clean(t) -> str:
    return re.sub(r"^\s*\d+[.)]\s*", "", " ".join(str(t).split()))[:200]


def all_facts() -> list[str]:
    with _LOCK:
        return [f["fact"] for f in _load()]


def listing() -> str:
    return "\n".join(f"{i + 1}. {f}" for i, f in enumerate(all_facts())) or "(none yet)"


def add(fact: str) -> str:
    fact = " ".join(fact.split())
    if not fact:
        return "nothing to remember"
    with _LOCK:
        facts = _load()
        if any(_norm(f["fact"]) == _norm(fact) for f in facts):
            return "already remembered"
        facts.append(_row(fact))
        _save(facts)
    return "remembered"


def forget(match: str) -> str:
    """Drop the notes that mention `match`. With the belief store on, also leave a request
    for the reconcile job: a long-term belief is never deleted, it is closed there with
    this request as the reason (and stays in the belief's history)."""
    from . import beliefs
    match = " ".join(match.split())
    low = match.lower()
    if not low:
        return "say what to forget"
    with _LOCK:
        facts = _load()
        keep = [f for f in facts if low not in f["fact"].lower() or f["fact"].startswith(FORGET_PREFIX)]
        removed = len(facts) - len(keep)
        if beliefs.ENABLED:
            today = datetime.now(config.TIMEZONE).strftime("%a %-d %B")
            keep.append(_row(f"{FORGET_PREFIX} ({today}): {match}"[:200]))
        if removed or beliefs.ENABLED:
            _save(keep)
    if beliefs.ENABLED:
        return (f"dropped {removed} of today's notes about that, and noted to let go of it in your long-term "
                "memory at your next sleep or night run (until then it may still be listed)")
    return f"forgot {removed} fact(s)" if removed else "no matching memory"


def apply_plan(plan: dict, max_add: int = 3) -> list[str]:
    """Apply {"add": [...], "update": [{"n", "fact"}], "remove": [n]} (1-based numbers
    referring to the list the model was shown). Returns what changed."""
    changed: list[str] = []
    with _LOCK:
        facts = _load()
        n_old = len(facts)
        for u in plan.get("update") or []:
            try:
                i, new = int(u["n"]) - 1, _clean(u["fact"])
            except (KeyError, ValueError, TypeError):
                continue
            if 0 <= i < n_old and new:
                changed.append(f"updated: {facts[i]['fact']} -> {new}")
                facts[i] = _row(new)
        drop = set()
        for r in plan.get("remove") or []:
            try:
                i = int(r) - 1
            except (ValueError, TypeError):
                continue
            if 0 <= i < n_old and i not in drop:
                drop.add(i)
                changed.append(f"forgot: {facts[i]['fact']}")
        facts = [f for i, f in enumerate(facts) if i not in drop]
        for a in (plan.get("add") or [])[:max_add]:
            a = _clean(a)
            if a and not any(_norm(f["fact"]) == _norm(a) for f in facts):
                facts.append(_row(a))
                changed.append(f"saved: {a}")
        if changed:
            _save(facts)
    return changed


def known_beliefs_block() -> str:
    """For the note writers: what is already in long-term memory (not numbered: it can't be
    edited from a note; a changed fact is simply ADDED and settled by the reconcile job)."""
    from . import beliefs
    if not beliefs.ENABLED or not beliefs.current():
        return ""
    lines = "\n".join(f"- {beliefs.sentence(b)}" for b in beliefs.current()[:beliefs.PROMPT_MAX])
    return ("\nALREADY IN LONG-TERM MEMORY (don't add these again; if one has CHANGED, ADD the new "
            "fact as a new sentence, it will replace the old one in the next sleep or night run):\n" + lines + "\n")


def writer_llm():
    """The small, fast model the per-turn note writer uses."""
    global _writer
    if _writer is None:
        from . import llm as llms
        _writer = llms.chat(temperature=0, timeout=45, max_retries=1, max_tokens=900)
    return _writer
