"""Long-term memory: a small JSON list of facts, injected into every system prompt.

Kept deliberately simple: Vector's memory is a few dozen facts about his people,
not a document store. Survives restarts (lives in the agent data volume).
"""
import json
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
