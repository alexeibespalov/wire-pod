"""Vector's own activity journal + read access to his logs, so he can answer
"hey, what are you doing?", "why did you do that?" or "why didn't you answer me?".

* record(kind, text): one short line per thing he DID: tools used in a conversation
  (who asked, what for), background schedule runs, practice tries, safety blocks, motion
  aborts, sleep jobs. Kept in DATA_DIR/logs/activity.jsonl (trimmed to the newest lines).
* recent_activity(minutes): the journal, newest last, as plain lines.
* search_logs(text, minutes): grep his technical log (agent.log) for details; secrets are
  redacted before the model sees anything.
"""
import json
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import config

_PATH = config.DATA_DIR / "logs" / "activity.jsonl"
_LOCK = threading.Lock()
_MAX_LINES = 3000
_count = 0

_SECRET = re.compile(r"(tvly-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|api[_-]?key=\S+|token=\S+)", re.I)


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def record(kind: str, text: str, **extra) -> None:
    """Append one journal line. Never raises (logging must not break an action)."""
    global _count
    try:
        row = {"t": time.time(), "kind": kind, "text": redact(" ".join(str(text).split()))[:300], **extra}
        with _LOCK:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            with _PATH.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            _count += 1
            if _count % 200 == 0:
                lines = _PATH.read_text().splitlines()
                if len(lines) > _MAX_LINES:
                    _PATH.write_text("\n".join(lines[-_MAX_LINES:]) + "\n")
    except Exception:
        pass


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.TIMEZONE).strftime("%-I:%M:%S %p").lower()


def lines(minutes: float = 15, kind: str = "") -> list[str]:
    cutoff = time.time() - minutes * 60
    out: deque[str] = deque(maxlen=60)
    try:
        with _PATH.open() as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if r.get("t", 0) < cutoff or (kind and r.get("kind") != kind):
                    continue
                out.append(f"{_clock(r['t'])} [{r.get('kind')}] {r.get('text')}")
    except FileNotFoundError:
        pass
    return list(out)


@tool
def recent_activity(
        minutes: Annotated[float, Field(description="How far back, default 15")] = 15,
        kind: Annotated[str, Field(description=(
            "Optional filter: conversation, tool, schedule, practice, safety, body, sleep"))] = "") -> str:
    """What YOU have actually been doing recently, from your own journal: tools you used and
    why, background schedules, practice, safety blocks, movements that stopped. Use it
    BEFORE answering "what are you doing?", "why did you do that?", "what happened?",
    "were you practising?" so you answer from facts, not guesses."""
    ls = lines(min(max(minutes, 1), 24 * 60), kind.strip())
    if not ls:
        return f"nothing recorded in the last {minutes:g} minutes" + (f" of kind {kind!r}" if kind else "")
    return "\n".join(ls)


@tool
def search_logs(
        text: Annotated[str, Field(description="Words to look for, e.g. 'error', 'practice', 'schedule', 'wake'")],
        minutes: Annotated[float, Field(description="How far back, default 30")] = 30) -> str:
    """Search your technical log for details (errors, what a tool returned, timing). Use
    when recent_activity isn't detailed enough, e.g. "why didn't you answer me?"."""
    log_path = config.DATA_DIR / "logs" / "agent.log"
    needle = text.lower().strip()
    if not needle:
        return "error: say what to look for"
    cutoff = datetime.fromtimestamp(time.time() - min(max(minutes, 1), 24 * 60) * 60).strftime("%Y-%m-%d %H:%M:%S")
    hits: deque[str] = deque(maxlen=25)
    try:
        with log_path.open(errors="replace") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 3_000_000))
            for ln in f:
                if ln[:19] >= cutoff and needle in ln.lower() and "HTTP Request:" not in ln:
                    hits.append(redact(ln.strip())[:300])
    except FileNotFoundError:
        return "no log file"
    return "\n".join(hits) if hits else f"no log lines mentioning {text!r} in the last {minutes:g} minutes"


ACTIVITY_TOOLS = [recent_activity, search_logs]
