"""An agent's long-term memory: the belief store on the NUC (beliefs-api).

Two kinds of memory, like a person's:

* NOTES (app/memory.py, memories.json): what he jotted down today, in plain sentences.
  Written while awake by `remember` and the per-turn writer. Not yet "cemented".
* BELIEFS (this module): what he holds to be true. One current belief per subject and
  source; when something changes the old belief is superseded, never deleted, so he can
  later say why he believes something and what he believed before. Notes become beliefs
  while he sleeps (app/belief_sleep.py).

Awake he only READS the store:
  - the current beliefs are cached here (snapshot.json) and shown in his prompt;
  - `recall(question)` searches them;
  - `belief_history(about)` fetches the story behind one belief, when someone asks.
The NUC being down never breaks a conversation: reads fall back to the cached snapshot.

Enabled when BELIEFS_URL and BELIEFS_TOKEN are set (the Pi's .env); otherwise the agent
behaves exactly as before (notes only, old sleep harvest).
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime

import httpx
from langchain_core.tools import tool

from .settings import cfg as config

log = logging.getLogger(config.LOGGER_NAME)

ENABLED = bool(config.BELIEFS_URL and config.BELIEFS_TOKEN)
_DIR = config.DATA_DIR / "beliefs"
_SNAPSHOT = _DIR / "snapshot.json"
_REFRESH_S = 300
PROMPT_MAX = 80            # beliefs shown in the prompt; the rest are reached with recall

_snap: dict = {"version": "", "beliefs": [], "fetched": 0.0}
_client: httpx.AsyncClient | None = None


class Unavailable(Exception):
    """The belief store can't be reached (or refused the request)."""


# ------------------------------------------------------------------------- HTTP
def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=f"{config.BELIEFS_URL}/v1/t/{config.BELIEFS_TENANT}",
            headers={"Authorization": f"Bearer {config.BELIEFS_TOKEN}"},
            timeout=httpx.Timeout(config.BELIEFS_TIMEOUT_S, connect=3))
    return _client


async def request(method: str, path: str, *, body: dict | None = None, params: dict | None = None,
                  headers: dict | None = None, timeout: float | None = None) -> httpx.Response:
    """One call to the belief store. Raises Unavailable when it can't be reached or
    answers 5xx; 4xx answers are returned (the caller decides what a refusal means)."""
    if not ENABLED:
        raise Unavailable("belief store not configured")
    try:
        r = await _http().request(method, path, json=body, params=params, headers=headers,
                                  **({"timeout": timeout} if timeout else {}))
    except (httpx.HTTPError, OSError) as e:
        raise Unavailable(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 500:
        raise Unavailable(f"HTTP {r.status_code}")
    return r


# --------------------------------------------------------------------- snapshot
def _load() -> None:
    global _snap
    try:
        _snap = json.loads(_SNAPSHOT.read_text())
    except (OSError, ValueError):
        pass


def _save() -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(_snap, ensure_ascii=False))
        tmp.replace(_SNAPSHOT)
    except OSError as e:
        log.warning("beliefs: couldn't save the snapshot (%s)", e)


def current() -> list[dict]:
    """His current beliefs, from the cache (never blocks, never raises)."""
    return _snap.get("beliefs") or []


async def refresh() -> bool:
    """Bring the cached current beliefs up to date. True if they changed."""
    global _snap
    try:
        r = await request("GET", "/beliefs", headers={"If-None-Match": f'"{_snap.get("version", "")}"'})
    except Unavailable as e:
        log.info("beliefs: store unreachable, keeping the cached snapshot (%s)", e)
        return False
    if r.status_code == 304:
        _snap["fetched"] = time.time()
        return False
    if r.status_code != 200:
        log.warning("beliefs: snapshot refused: %s %s", r.status_code, r.text[:200])
        return False
    d = r.json()
    _snap = {"version": d.get("version", ""), "beliefs": d.get("beliefs") or [], "fetched": time.time()}
    _save()
    log.info("beliefs: snapshot updated, %d current beliefs", len(_snap["beliefs"]))
    return True


async def _refresher() -> None:
    while True:
        await refresh()
        await asyncio.sleep(_REFRESH_S)


def start() -> None:
    """From the FastAPI lifespan: load the cached snapshot, keep it fresh in the background."""
    if not ENABLED:
        log.info("beliefs: not configured (BELIEFS_URL / BELIEFS_TOKEN): notes-only memory")
        return
    _load()
    asyncio.get_running_loop().create_task(_refresher())
    log.info("beliefs: enabled, store %s tenant %s, %d cached beliefs",
             config.BELIEFS_URL, config.BELIEFS_TENANT, len(current()))


# ------------------------------------------------------------------- wording
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def node_label(node: str) -> str:
    """'home/person:leo' -> 'Leo', 'home/household:family' -> 'The family',
    'home/robot:vector' -> 'You (Vector)', 'home/place:coffee-table' -> 'The coffee table (place)'."""
    kind, _, name = node.rsplit("/", 1)[-1].partition(":")
    words = name.replace("-", " ").replace("_", " ").strip()
    if kind == "person":
        return words.title()
    if kind == "household":
        return "The family"
    if kind in ("robot", "assistant"):
        return f"You ({words.title()})" if _slug(words) == _slug(config.ASSISTANT_NAME) else words.title()
    if kind == "place":
        return f"The {words} (place)"
    if kind == "pet":
        return f"{words.title()} (pet)"
    return f"{words} ({kind})"


def who(source: str | None) -> str:
    """'human:leo' -> 'Leo', 'agent:vector' -> 'you', 'human:unknown' -> 'someone'."""
    kind, _, name = (source or "").partition(":")
    if kind == "agent" and _slug(name) == _slug(config.ASSISTANT_NAME):
        return "you"                     # another agent keeps its name: "Vector said"
    if not name or name == "unknown":
        return "someone"
    return name.replace("-", " ").replace("_", " ").title()


def _when(iso: str | None) -> str:
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(config.TIMEZONE)
    except ValueError:
        return ""
    return d.strftime("%a %-d %b %Y")


def sentence(b: dict) -> str:
    """One current belief as a plain line: 'Leo: favourite game: Minecraft (Leo said)'."""
    if b.get("node"):
        src = who(b.get("source"))
        told = "you noticed" if src == "you" else f"{src} said"
        return f"{node_label(b['node'])}: {b['field'].replace('_', ' ')}: {b.get('value') or b.get('claim')} ({told})"
    return str(b.get("claim") or "")


def prompt_section() -> str:
    """The beliefs block of the system prompt (stable: changes only when beliefs change)."""
    beliefs = current()
    if not beliefs:
        return "- (nothing cemented yet)"
    by_id = {b["id"]: b for b in beliefs}
    settled, unsettled, seen = [], [], set()
    for b in beliefs:
        others = [by_id[i] for i in b.get("contested_with") or [] if i in by_id]
        if others:
            key = tuple(sorted([b["id"], *[o["id"] for o in others]]))
            if key in seen:
                continue
            seen.add(key)
            sides = [b, *others]
            what = (f"{node_label(b['node'])}: {b['field'].replace('_', ' ')}" if b.get("node") else "one thing")
            unsettled.append(f"- {what}: " + " BUT ".join(
                f"{s.get('value') or s.get('claim')} ({who(s.get('source') or s.get('actor'))} said, "
                f"{_when(s.get('valid_from'))})" for s in sides))
        else:
            settled.append(b)
    settled.sort(key=lambda b: (0 if b.get("node") else 1, b.get("node") or "", b.get("field") or ""))
    lines = [f"- {sentence(b)}" for b in settled[:PROMPT_MAX]]
    if len(settled) > PROMPT_MAX:
        lines.append(f"- (and {len(settled) - PROMPT_MAX} more: use recall to find them)")
    out = "\n".join(lines) if lines else "- (nothing settled yet)"
    if unsettled:
        out += ("\nUNSETTLED (you were told different things and haven't worked out which is right):\n"
                + "\n".join(unsettled))
    return out


# ---------------------------------------------------------------------- tools
_WORD = re.compile(r"[a-z0-9']+")
_STOP = {"the", "a", "an", "is", "are", "was", "do", "does", "did", "you", "your", "i", "my", "me", "of", "to",
         "what", "who", "when", "where", "why", "how", "about", "that", "this", "it", "s", "think", "believe",
         "know", "remember", "have", "has", "and", "or", "in", "on", "for"}


def _local_search(question: str, k: int) -> list[dict]:
    """Word-overlap search over the cached beliefs, for when the store can't be reached."""
    words = {w for w in _WORD.findall(question.lower()) if w not in _STOP}
    scored = []
    for b in current():
        text = f"{b.get('subject', '')} {b.get('claim', '')}".lower().replace("_", " ").replace("-", " ")
        hits = sum(1 for w in words if w in text)
        if hits:
            scored.append((hits, b))
    scored.sort(key=lambda x: -x[0])
    return [b for _, b in scored[:k]]


async def search(question: str, k: int = 6) -> tuple[list[dict], bool]:
    """(matching current beliefs, True if they came from the live store)."""
    try:
        r = await request("GET", "/beliefs", params={"q": question, "k": k})
        if r.status_code == 200:
            return r.json().get("beliefs") or [], True
        log.warning("beliefs: search refused: %s %s", r.status_code, r.text[:200])
    except Unavailable as e:
        log.info("beliefs: search fell back to the cached snapshot (%s)", e)
    return _local_search(question, k), False


@tool
async def recall(question: str) -> str:
    """Search your long-term memory for what you currently believe about something or
    someone ("what is Leo's favourite game", "Aria birthday", "the coffee table"). Use it
    whenever someone asks what you remember or know about a person, the family or your
    home and the answer isn't already in WHAT YOU KNOW, and before you say you don't know
    or don't remember. Returns only what you believe NOW (never old, replaced beliefs)."""
    question = " ".join(question.split())[:200]
    if not ENABLED:
        return "long-term memory search isn't set up; you only have what's in your prompt"
    if not question:
        return "error: ask about something"
    found, live = await search(question)
    if not found:
        return ("nothing in your long-term memory matches that"
                + ("" if live else " (the memory store is unreachable right now; this was a quick look "
                                   "through what you have cached)"))
    lines = []
    for b in found:
        line = f"- {sentence(b)} [since {_when(b.get('valid_from'))}]"
        if b.get("contested_with"):
            line += " (UNSETTLED: you were also told something different)"
        lines.append(line)
    head = "You currently believe:" if live else "From your cached memory (store unreachable):"
    return head + "\n" + "\n".join(lines)


def _story(h: dict) -> str:
    """A belief's history as plain lines the model can retell."""
    chain = h.get("chain") or []
    out = []
    for i, c in enumerate(chain):
        said = who(c.get("source") or c.get("actor"))
        line = (f"{i + 1}. {'NOW' if c.get('current') else 'BEFORE'}: {c.get('claim')} "
                f"(from {_when(c.get('valid_from'))}; {'you noticed' if said == 'you' else said + ' said'})")
        if c.get("because"):
            line += f". Why: {c['because']}"
        ev = c.get("evidence") or {}
        for s in ev.get("beliefs") or []:
            subj = s.get("subject") or ""
            if "#" in subj and "/" in subj:
                told = who(subj.rsplit("@", 1)[-1]) if "@" in subj else "someone"
                about = f"about {node_label(subj.split('#')[0])}, " + ("you noticed" if told == "you" else f"{told} said")
            else:
                about = "a note"
            other = f"\"{s.get('claim')}\" ({about})"
            out_words = {"depends_on": "It rests on", "refines": "It refines", "validates": "It backs up",
                         "invalidates": "It ruled out", "constrains": "It limits", "root": "It started from"}
            in_words = {"depends_on": "relies on it", "refines": "was re-filed from it", "validates": "backs it up",
                        "invalidates": "ruled it out", "constrains": "limits it", "root": "started from it"}
            kind = s.get("kind")
            line += (f". {out_words.get(kind, 'It is linked to')} {other}" if s.get("direction") == "out"
                     else f". {other} {in_words.get(kind, 'is linked to it')}")
            if not s.get("current"):
                line += " [no longer believed]"
        if ev.get("records"):
            line += f". Based on {len(ev['records'])} record(s) (e.g. {ev['records'][0]})"
        e = c.get("ended")
        if e:
            if e.get("how") == "superseded":
                line += f". Replaced on {_when(e.get('at'))}" + (f" because: {e['reason']}" if e.get("reason") else "")
            elif e.get("how") == "invalidated":
                line += f". Dropped on {_when(e.get('at'))}" + (f" because: {e['reason']}" if e.get("reason") else "")
        out.append(line)
    for cf in h.get("conflicts") or []:
        sides = "; ".join(f"{who(s.get('actor'))}: {s.get('claim')}" for s in cf.get("sides") or [])
        res = cf.get("resolution")
        if res:
            how = {"both_stand": "both can be true", "winner": "one side was right"}.get(res.get("verdict"), res.get("verdict"))
            out.append(f"There was a disagreement ({sides}); you settled it: {how}"
                       + (f", because {res['reason']}" if res.get("reason") else ""))
        else:
            out.append(f"There is a disagreement you haven't settled ({sides})")
    return "\n".join(out)


@tool
async def belief_history(about: str) -> str:
    """The story behind ONE thing you believe: why you believe it, who told you or what you
    saw, and what you believed before. Use it ONLY when someone asks for it ("why do you
    think that?", "who told you?", "did you always think that?", "what did you think
    before?"). `about` = the belief in a few words ("Leo's favourite game", "the coffee
    table"). Don't use it for ordinary remembering: that's recall."""
    about = " ".join(about.split())[:200]
    if not ENABLED:
        return "you have no belief history yet (long-term memory isn't set up)"
    try:
        r = await request("GET", "/beliefs", params={"q": about, "k": 3})
        if r.status_code != 200:
            return "error: your memory store refused the question"
        found = r.json().get("beliefs") or []
        if not found:
            return "you don't currently believe anything about that, so there's no history to tell"
        b = found[0]
        r = await request("GET", f"/beliefs/{b['id']}/history")
        if r.status_code != 200:
            return "error: couldn't read the history of that belief"
    except Unavailable:
        return ("error: your memory store can't be reached right now, so you can't look up the history. "
                "Say so honestly; you still know what you currently believe.")
    story = _story(r.json())
    others = "; ".join(sentence(x) for x in found[1:])
    return (f"History of: {sentence(b)}\n{story}\n"
            "(Oldest first. Tell it in your own words, briefly; only the NOW line is what you believe.)"
            + (f"\nOther beliefs that matched, in case they meant one of these: {others}" if others else ""))


BELIEF_TOOLS = [recall, belief_history]
