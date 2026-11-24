"""The reconcile job: cement an agent's notes and talk into long-term beliefs.

Like a person's nap: awake the agent only jots notes; here they are turned into beliefs,
each with WHO said it, WHY it is believed and WHAT it rests on (the store refuses a belief
without a reason and evidence), and disagreements are looked at calmly.

WHEN it runs is the agent's business: Vector calls run(ctx) from a sleep job, Cortana from
her nightly loop. `ctx` only needs: state (a dict that persists between runs), save(),
alive(), sleep(seconds) and log.

Stages, in order. Every stage can be cut short (the agent is needed, the Sonnet batch isn't
back, the NUC is down) and is picked up again in a later run without writing anything
twice: the prompts and the times are frozen in ctx.state, and belief ids are deterministic.

  1. extract   notes + new transcript lines -> statements (LLM) -> POST /beliefs
               "Asked to forget" notes -> the belief is closed with that reason
  2. maintain  conflict detection, embeddings, similar-pair suggestions (a job on the NUC)
  3. rule      similar pairs, open conflicts and plans that are over (LLM) -> rulings
  4. refresh   the snapshot the prompt reads

"Overheard" notes are never closed here. They stay current beliefs that recall can find;
after BELIEFS_OVERHEARD_DAYS they are simply no longer listed in the prompt (beliefs.py).

Nothing is ever deleted. A belief that stops being true is superseded or closed, and the
reason is kept, so `belief_history` can tell the story later.

Per agent (config): ASSISTANT_NAME, ASSISTANT_DESC, PRONOUNS, SELF_NODE, SEES_SPEAKERS (can
it tell who is talking?), HEARS_OVERHEARD (does it catch talk not meant for it?).
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage

from .settings import cfg as config
from . import activity, beliefs, compaction, llm as llms, notes as memory, sleepbatch

JOB = "reconcile_beliefs"
MODEL_TAG = "sleep"

FIELDS_HELP = """\
age, birthday, role (who they are: "Leo's little sister"), school, job, lives_in,
favourite_<thing> (favourite_game, favourite_food, favourite_colour, ...),
likes_<thing>, dislikes_<thing>, fear_<thing>, hobby_<thing>, learning_<thing>,
friend_<name>, sibling_<name>, parent_<name>, partner, pet_<name>, achievement_<short>,
plan_<short> (a plan or event with its date in the value), rule_<short> (house rules),
name_for_<thing> (names given to {name}'s things), wants_<short> (something they asked {name} for)"""

_EXTRACT = """\
You are the background mind of {name}, {desc}. While {he} rests you turn what {he} jotted \
down and heard today into {his} long-term beliefs. Today is {today}.

Each belief is ONE fact filed under a node and a field, with who said it:
- node: person:<first name, lower case> | household:family | {self_node} | pet:<name> | place:<slug>
- field: lower_snake_case. REUSE a field that already exists below whenever it fits (a \
different spelling makes a second, unconnected belief). Otherwise choose from:
{fields}
  One value per field: for list-like things use one field per item (hobby_lego, hobby_chess).
- value: short and canonical ("Minecraft", "eight", "Saturday 28 November 2026, twelve to two").
- said_by: the first name (lower case) of the person who SAID it, "{self_slug}" for something \
{name} saw or worked out, or "unknown" if the speaker wasn't recognised.{speaker_rule}
- class: FACT (someone said it), OBSERVATION ({name} noticed it), RULE (a house rule).
- reason: one short sentence on WHY {name} believes it, concrete enough to retell later ("Leo \
said it himself while talking about his games", "Aria corrected what Leo had said"). Required.
- from: the ids of the lines/notes it comes from ([L..] transcript lines, [N..] notes). Required.

Rules:
- ONLY what a person actually SAID (or {name} noticed). Never infer, guess or extrapolate.
- Lasting things only: who's who, ages, birthdays, favourites, hobbies, what they're into or \
learning, routines, plans and events WITH their date, fears and dislikes, achievements, house \
rules, names given to {his} things. No small talk, moods, questions, or things only true right \
now, and never what {name} read out or looked up (calendar, lists, web answers, device states).
- A fact someone states about THEMSELVES and a different thing someone ELSE says about them \
are both kept, each under its own said_by: do not choose between them.
- If a fact changed (a new favourite, a new age), file the NEW value under the SAME node \
and field: it replaces the old one and the old one stays in {his} history.
- Speech-to-text garbles words into fake names: only use a person's name if they were \
clearly introduced or talked about.
- Something another assistant told {name} (a note like "Vector says ...") is filed with \
said_by = that assistant's name.
- Anything worth keeping that has no clean node/field goes in "notes" as one short \
third-person sentence. Facts from an unrecognised speaker ("unknown") also go in "notes".{overheard_rule}
- A note that starts "Asked to forget" is a request to let go of something: list the \
matching existing belief(s) under "retract" (by their [B..] id) with the request as the reason.
- Every [N..] note must end up in "statements", "notes", "retract" or "skip" (skip = it \
is small talk or already believed exactly as it is).

ALREADY BELIEVED (current long-term beliefs; do not repeat one unless it changed):
{known}

TODAY'S NOTES:
{notes}

TRANSCRIPT (new since the last run):
{transcript}

Reply as JSON:
{{"statements": [{{"node": "person:leo", "field": "favourite_game", "value": "Minecraft", \
"said_by": "leo", "class": "FACT", "reason": "...", "from": ["L3"]}}],
 "notes": [{{"text": "...", "reason": "...", "from": ["N2"]}}],
 "retract": [{{"belief": "B4", "reason": "...", "from": ["N5"]}}],
 "skip": ["N1"]}}"""

_SPEAKER_RULE = """
  {name} cannot see who is speaking: said_by is a name ONLY when the speaker said who they \
are (in that line or just before it); otherwise "unknown"."""

_OVERHEARD_RULE = """
- OVERHEARD lines were not said to {name}: never a fact about anyone. At most ONE note per \
topic starting "Overheard (<day> <date>):", topic only, no names, numbers or anything private."""

_RULE = """\
You are the background mind of {name}, {desc}. Today is {today}. Look calmly at the open \
questions in {his} long-term memory and rule on each one. Give a short reason every time: \
{he} may be asked later why {he} believes something.

A. PAIRS that look alike but are filed separately. For each, one verdict:
- "duplicate": the same fact filed twice. "keep" = the better-filed side (A or B). If one \
side is the general fact and the other a detail of it ("plays Minecraft" / "likes mining for \
diamonds in Minecraft"), they are NOT duplicates: answer "unrelated" so both are kept.
- "changed": the same person said both and one is newer/replaces the other. "keep" = the current one.
- "disagree": different people say different things about the same thing. "keep" = the \
side whose node+field both belong under; "value" = the other side's value in a few words.
- "unrelated": different things. Nothing changes.
{pairs}

B. CONFLICTS: two people said different things about one field. For each, one verdict:
- "same_value": they mean the same thing in different words. "winner" = the better wording.
- "both_true": both can be true at once.
- "own_statement_wins": one side is the person speaking about their OWN preference, feeling \
or plan, the other is someone else speaking about them. "winner" = the person themselves.
- "open": a real disagreement between equals. Leave it; {he} will ask when it matters.
Never decide by which is newer when different people said them.
{conflicts}

C. CURRENT BELIEFS. List any PLAN or EVENT (a field starting plan_, or a note about a \
dated plan) whose date is clearly BEFORE today under "expired": "past" = the same fact \
rewritten as a past fact ("went to Spain in November 2026"), or null if it no longer matters \
at all. Leave everything else alone: facts about people, and "Overheard" notes, never expire.
{beliefs}

Reply as JSON:
{{"pairs": [{{"n": 1, "verdict": "duplicate", "keep": "A", "value": null, "reason": "..."}}],
 "conflicts": [{{"n": 1, "verdict": "open", "winner": null, "reason": "..."}}],
 "expired": [{{"belief": "B3", "past": "...", "reason": "..."}}]}}"""


def _self_slug() -> str:
    return re.sub(r"[^a-z0-9]+", "_", config.ASSISTANT_NAME.lower()).strip("_")


def _self_actor() -> str:
    return f"agent:{_self_slug().replace('_', '-')}"


def _who() -> dict:
    """The words every prompt needs about the agent it is thinking for."""
    he, him, his = config.PRONOUNS
    name = config.ASSISTANT_NAME
    return {"name": name, "desc": config.ASSISTANT_DESC, "he": he, "him": him, "his": his,
            "self_node": config.SELF_NODE, "self_slug": _self_slug()}


# ------------------------------------------------------------------ small helpers
def has_work(st: dict) -> bool:
    """Anything to do? `st` is the job's persisted state (what run() gets as ctx.state)."""
    if not beliefs.ENABLED:
        return False
    if st.get("extract") or st.get("rule"):
        return True                       # something is half done (waiting for a batch or the NUC)
    if memory._load():
        return True                       # notes to cement
    try:
        last_talk = json.loads((config.DATA_DIR / "thread.json").read_text()).get("last", 0)
    except (OSError, ValueError):
        last_talk = 0
    if last_talk > st.get("at", 0):
        return True
    return st.get("last_rule_day") != datetime.now(config.TIMEZONE).strftime("%Y-%m-%d") and bool(beliefs.current())


_WHEN = re.compile(r"\((Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w* (\d{1,2}):(\d{2}) ([AP]M)")
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def line_time(line: str, now: datetime) -> datetime | None:
    """'LEO (Sat 10:39 AM): ...' -> the most recent Saturday 10:39 that isn't in the future."""
    m = _WHEN.search(line)
    if not m:
        return None
    hour = int(m.group(2)) % 12 + (12 if m.group(4) == "PM" else 0)
    for back in range(8):
        d = (now - timedelta(days=back)).replace(hour=hour, minute=int(m.group(3)), second=0, microsecond=0)
        if d.weekday() == _DAYS.index(m.group(1)) and d <= now + timedelta(minutes=1):
            return d
    return None


def _iso(d: datetime) -> str:
    return d.astimezone(config.TIMEZONE).isoformat(timespec="milliseconds")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:40]


def _node(raw: str) -> str | None:
    kind, _, name = str(raw or "").strip().lower().removeprefix("home/").partition(":")
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return f"home/{kind}:{name}" if kind in ("person", "household", "robot", "assistant", "pet", "place") and name else None


def _source(said_by: str) -> str:
    s = _slug(said_by)
    if s in (_self_slug(), "robot", "assistant", "self", ""):
        return _self_actor()
    peer = _slug(getattr(config, "PEER_NAME", ""))
    if peer and s == peer:                       # what the other assistant told this one
        return f"agent:{s.replace('_', '-')}"
    return f"human:{s.replace('_', '-')}"


def _known_listing(current: list[dict]) -> tuple[str, dict]:
    """The current beliefs as numbered lines for a prompt, and {B-number: belief}."""
    lines, refs = [], {}
    for i, b in enumerate(current, 1):
        refs[f"B{i}"] = b
        if b.get("node"):
            lines.append(f"[B{i}] {b['node'].removeprefix('home/')} | {b['field']} | {b.get('value') or b.get('claim')}"
                         f" | said by {beliefs.who(b.get('source'))}")
        else:
            lines.append(f"[B{i}] note | {b.get('claim')}")
    return "\n".join(lines) or "(nothing yet)", refs


async def _ask(prompt: str, max_tokens: int) -> dict:
    model = sleepbatch.model(max_tokens=max_tokens, direct=memory.writer_llm())   # Sonnet via batches on Claude
    try:
        d = await llms.ainvoke_json(model, [HumanMessage(prompt)])
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


async def _post(path: str, body: dict, timeout: float = 30):
    return await beliefs.request("POST", path, body=body, timeout=timeout)


async def _invalidate(bid: str, reason: str, triggered_by: str | None = None) -> bool:
    body = {"actor": _self_actor(), "reason": reason,
            "provenance": {"source": "sleep", "method": "sleep-rule", "confidence": 0.8}}
    if triggered_by:
        body["triggered_by"] = triggered_by
    r = await _post(f"/beliefs/{bid}/invalidate", body)
    return r.status_code in (200, 409)     # 409 = already closed (a repeat of this session)


async def _assert(statements: list[dict], fallback: datetime) -> tuple[list[dict], list[str]]:
    """POST statements in batches. A statement refused as backdated (the belief already has
    a newer head) is retried once at `fallback`, a time after everything he knew before."""
    results, problems = [], []
    for i in range(0, len(statements), 100):
        chunk = statements[i:i + 100]
        r = await _post("/beliefs", {"statements": chunk}, timeout=60)
        if r.status_code != 200:
            problems.append(f"batch refused: {r.status_code} {r.text[:160]}")
            results += [{} for _ in chunk]
            continue
        for s, res in zip(chunk, r.json().get("results") or []):
            err = res.get("error")
            if err and "backdated" in err.get("message", ""):
                again = await _post("/beliefs", {**s, "valid_from": _iso(fallback + timedelta(milliseconds=len(results)))})
                res = again.json() if again.status_code == 200 else {"error": again.json().get("error", {})}
                err = res.get("error")
            if err:
                problems.append(f"{s.get('node') or s.get('subject')}: {err.get('message', '')[:140]}")
            results.append(res)
    return results, problems


# ------------------------------------------------------------------- 1. extract
async def _build_extract(st: dict) -> dict | None:
    seen = set(st.get("seen_ids", []))
    now = datetime.now(config.TIMEZONE)
    lines, meta, new_ids, heard = [], {}, [], 0
    for thread, saved in await compaction._recent_threads(days=1.5):
        if thread.startswith(("a2a-", "sched-")):
            continue
        for m in saved:
            if m.id in seen or not isinstance(m, (HumanMessage, AIMessage)):
                continue
            new_ids.append(m.id)
            said = llms.text(m).strip()
            if not said:
                continue
            ref = f"L{len(meta) + 1}"
            if isinstance(m, HumanMessage):
                text = memory.speaker_line(said)[:500]
                t = line_time(text, now) or now
                heard += 1
            else:
                text, t = f"{config.ASSISTANT_NAME.upper()}: {said[:400]}", now
            meta[ref] = {"id": m.id, "thread": thread, "t": _iso(t + timedelta(milliseconds=len(meta)))}
            lines.append(f"[{ref}] {text}")
    notes = memory._load()
    if not notes and not heard:              # nobody said anything and nothing was jotted down
        return {"new_ids": new_ids, "empty": True}
    for i, n in enumerate(notes, 1):
        try:
            t = datetime.fromisoformat(n["saved"])
        except (KeyError, ValueError):
            t = now
        meta[f"N{i}"] = {"note": n["fact"], "saved": n.get("saved", ""), "t": _iso(t + timedelta(milliseconds=500 + i))}
    await beliefs.refresh()
    known, brefs = _known_listing(beliefs.current())
    w = _who()
    prompt = _EXTRACT.format(
        **w, today=now.strftime("%A %-d %B %Y"), fields=FIELDS_HELP.format(name=w["name"]), known=known,
        speaker_rule="" if config.SEES_SPEAKERS else _SPEAKER_RULE.format(name=w["name"]),
        overheard_rule=_OVERHEARD_RULE.format(name=w["name"]) if config.HEARS_OVERHEARD else "",
        notes="\n".join(f"[N{i}] (noted {n.get('saved', '')[:16].replace('T', ' ')}) {n['fact']}"
                        for i, n in enumerate(notes, 1)) or "(none)",
        transcript="\n".join(lines)[-16000:] or "(nothing new)")
    return {"prompt": prompt, "meta": meta, "new_ids": new_ids, "now": _iso(now),
            "brefs": {k: b["id"] for k, b in brefs.items()}}


def _evidence(refs, meta: dict) -> tuple[list[str], str | None, str | None]:
    """(evidence record ids, origin thread, latest time) for a statement's `from` list."""
    records, origin, t = [], None, None
    for r in refs if isinstance(refs, list) else []:
        m = meta.get(str(r).strip("[]"))
        if not m:
            continue
        if "id" in m:
            records.append(f"msg:{m['id']}")
            origin = origin or f"thread:{m['thread']}"
        else:
            records.append(f"note:{m['saved']}:{hashlib.sha256(m['note'].encode()).hexdigest()[:8]}")
        t = max(t, m["t"]) if t else m["t"]
    return records, origin, t


def _statements(plan: dict, ex: dict) -> tuple[list[dict], set]:
    """The model's plan as API statements (every one with a reason and evidence)."""
    meta, out, used = ex["meta"], [], set()

    def note(text, reason, refs, said_by=""):
        text = " ".join(str(text).split())[:240]
        if not text:
            return
        records, origin, t = _evidence(refs, meta)
        used.update(str(r).strip("[]") for r in refs or [])
        out.append({"store": "dialogue", "subject": "note-" + hashlib.sha256(memory._norm(text).encode()).hexdigest()[:10],
                    "claim": text, "class": "OBSERVATION", "actor": _source(said_by),
                    "valid_from": t or ex["now"], "origin": origin,
                    "reason": str(reason or "noted during conversation")[:300],
                    "evidence": {"records": records or [f"sleep:{ex['now'][:10]}"]},
                    "provenance": {"source": "conversation", "method": "sleep-extract", "confidence": 0.7}})

    for s in plan.get("statements") or []:
        if not isinstance(s, dict):
            continue
        node, field, value = _node(s.get("node")), _slug(s.get("field")), " ".join(str(s.get("value") or "").split())
        said_by, refs = _slug(s.get("said_by") or "unknown"), s.get("from") or []
        if not node or not field or not value:
            continue
        from_notes_only = bool(refs) and all(str(r).strip("[]").startswith("N") for r in refs)
        if said_by == "unknown" and from_notes_only:
            # the agent's own note, which names nobody as the speaker: it is the agent's
            # belief (filed under itself as the source), not "someone I didn't recognise"
            said_by = _self_slug()
            s = {**s, "reason": s.get("reason") or "from my own notes"}
        if said_by == "unknown":          # an unrecognised speaker never gets a field of their own
            note(f"Someone I didn't recognise said this about {beliefs.node_label(node)}: "
                 f"{field.replace('_', ' ')}: {value}",
                 s.get("reason"), refs)
            continue
        records, origin, t = _evidence(refs, meta)
        used.update(str(r).strip("[]") for r in refs)
        cls = str(s.get("class") or "FACT").upper()
        src = _source(said_by)
        who = beliefs.who(src)
        out.append({"node": node, "field": field, "value": value[:200], "source": src, "actor": src,
                    "class": cls if cls in ("FACT", "OBSERVATION", "RULE") else "FACT",
                    "valid_from": t or ex["now"], "origin": origin,
                    "reason": str(s.get("reason") or ("I noticed it myself" if who == "you" else f"{who} said so"))[:300],
                    "evidence": {"records": records or [f"sleep:{ex['now'][:10]}"]},
                    "provenance": {"source": "conversation", "method": "sleep-extract", "confidence": 0.8}})
    for n in plan.get("notes") or []:
        if isinstance(n, dict):
            note(n.get("text"), n.get("reason"), n.get("from") or [])
    for r in plan.get("retract") or []:
        if isinstance(r, dict):
            used.update(str(x).strip("[]") for x in r.get("from") or [])
    used.update(str(x).strip("[]") for x in plan.get("skip") or [])
    # a note the model didn't account for is never lost: it is kept as it was written
    for ref, m in meta.items():
        if ref.startswith("N") and ref not in used and not m["note"].startswith(memory.FORGET_PREFIX):
            note(m["note"], "I jotted this down while awake", [ref])
    out.sort(key=lambda s: s["valid_from"])
    return out, used


async def _extract(ctx, changed: list[str]) -> None:
    st = ctx.state
    ex = st.get("extract") or await _build_extract(st)
    if ex is None:
        return
    if ex.get("empty"):
        st["seen_ids"] = (st.get("seen_ids", []) + ex["new_ids"])[-4000:]
        return
    st["extract"] = ex
    ctx.save()                                  # the frozen prompt: the same request next session
    plan = await _ask(ex["prompt"], 4000)       # may raise sleepbatch.Pending
    if not plan and st.get("extract_tries", 0) < 2:
        # not JSON: ask afresh next session (a new prompt, so not the same cached answer);
        # after two tries the notes are filed as they are rather than held back for ever
        st["extract_tries"] = st.get("extract_tries", 0) + 1
        st.pop("extract", None)
        ctx.save()
        return
    st.pop("extract_tries", None)
    statements, _used = _statements(plan, ex)
    fallback = datetime.fromisoformat(ex["now"])
    results, problems = await _assert(statements, fallback)
    for s, res in zip(statements, results):
        if res.get("action") in ("created", "revised"):
            what = (f"{beliefs.node_label(s['node'])}: {s['field'].replace('_', ' ')}: {s['value']}"
                    if s.get("node") else s["claim"])
            changed.append(("now believes: " if res["action"] == "created" else "changed belief: ") + what)
    for r in plan.get("retract") or []:
        bid = ex["brefs"].get(str((r or {}).get("belief", "")).strip("[]")) if isinstance(r, dict) else None
        if bid and await _invalidate(bid, str(r.get("reason") or "asked to forget it")[:300]):
            changed.append(f"let go of a belief ({str(r.get('reason') or 'asked to forget it')[:80]})")
    for p in problems:
        ctx.log.warning("sleep: beliefs: %s", p)
    # cemented: today's notes that were in this prompt are done (later ones stay)
    done = {(m["note"], m["saved"]) for ref, m in ex["meta"].items() if ref.startswith("N")}
    with memory._LOCK:
        memory._save([n for n in memory._load() if (n["fact"], n.get("saved", "")) not in done])
    st["seen_ids"] = (st.get("seen_ids", []) + ex["new_ids"])[-4000:]
    st.pop("extract", None)
    ctx.save()


# ------------------------------------------------------------------ 2. maintain
async def _maintain(ctx) -> dict:
    r = await _post("/jobs", {"steps": ["detect", "embed", "suggest_pairs"],
                              "suggest_pairs": {"min_sim": 0.8, "limit": 30}})
    if r.status_code != 202:
        return {"error": r.text[:160]}
    job, deadline = r.json()["job"], time.time() + 120
    while ctx.alive() and time.time() < deadline:
        j = (await beliefs.request("GET", f"/jobs/{job}")).json()
        if j.get("state") in ("done", "failed"):
            return {"state": j["state"]}
        await ctx.sleep(2)
    return {"state": "still running"}


# ---------------------------------------------------------------------- 4. rule
def _side(b: dict) -> str:
    return f"{beliefs.sentence(b)} [since {beliefs._when(b.get('valid_from'))}]"


async def _build_rule(st: dict) -> dict | None:
    cur = {b["id"]: b for b in beliefs.current()}
    pairs, conflicts = [], []
    for rv in (await beliefs.request("GET", "/reviews")).json().get("reviews") or []:
        cand = rv.get("candidate") or {}
        ids = cand.get("pair") or []
        if rv.get("reason") != "similar_pair" or len(ids) != 2:
            continue
        if not all(i in cur for i in ids):          # one side is no longer believed: nothing to rule on
            await _post(f"/reviews/{rv['review']}/resolve", {"resolution": {"verdict": "stale"}})
            continue
        pairs.append({"review": rv["review"], "a": ids[0], "b": ids[1]})
    for node in (await beliefs.request("GET", "/conflicts")).json().get("nodes") or []:
        for rec in node.get("records") or []:
            ids = [s.get("belief") for s in rec.get("sides") or []]
            if len(ids) == 2 and all(i in cur for i in ids):
                conflicts.append({"record": rec["record"], "a": ids[0], "b": ids[1]})
    today = datetime.now(config.TIMEZONE)
    day = today.strftime("%Y-%m-%d")
    if not pairs and not conflicts and (st.get("last_rule_day") == day or not cur):
        return None
    known, brefs = _known_listing(list(cur.values()))
    prompt = _RULE.format(
        **_who(), today=today.strftime("%A %-d %B %Y"),
        pairs="\n".join(f"{i}. A: {_side(cur[p['a']])}\n   B: {_side(cur[p['b']])}"
                        for i, p in enumerate(pairs, 1)) or "(none)",
        conflicts="\n".join(f"{i}. A: {_side(cur[c['a']])}\n   B: {_side(cur[c['b']])}"
                            for i, c in enumerate(conflicts, 1)) or "(none)",
        beliefs=known)
    return {"prompt": prompt, "pairs": pairs, "conflicts": conflicts, "day": day, "now": _iso(today),
            "brefs": {k: b["id"] for k, b in brefs.items()}}


def _can_expire(b: dict) -> bool:
    """Only a plan or dated event can be over. A fact about a person never expires, and an
    "Overheard" note is kept (it just leaves the prompt after a while)."""
    if b.get("node"):
        return str(b.get("field", "")).startswith("plan_")
    return not str(b.get("claim", "")).startswith("Overheard")


def _restate(b: dict, value: str, reason: str, at: str, records: list, node=None, field=None, links=None) -> dict:
    """A statement that re-files belief `b`'s side with a new value (or under another field)."""
    s = {"node": node or b["node"], "field": field or b["field"], "value": value,
         "source": b.get("source") or b.get("actor"), "actor": b.get("actor") or _self_actor(),
         "class": b.get("class") or "FACT", "valid_from": at, "reason": reason[:300],
         "evidence": {"records": records},
         "provenance": {"source": "sleep", "method": "sleep-rule", "confidence": 0.7}}
    if links:
        s["evidence"]["beliefs"] = links
    return s


async def _rule(ctx, changed: list[str]) -> None:
    st = ctx.state
    await beliefs.refresh()
    rule = st.get("rule") or await _build_rule(st)
    if rule is None:
        return
    st["rule"] = rule
    ctx.save()
    plan = await _ask(rule["prompt"], 3000)     # may raise sleepbatch.Pending
    await beliefs.refresh()
    cur = {b["id"]: b for b in beliefs.current()}
    now = datetime.fromisoformat(rule["now"])
    tick = iter(range(1, 10_000))
    at = lambda: _iso(now + timedelta(milliseconds=next(tick)))   # noqa: E731

    for v in plan.get("pairs") or []:
        try:
            p = rule["pairs"][int(v["n"]) - 1]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        verdict, reason = str(v.get("verdict") or "unrelated"), str(v.get("reason") or "")[:300]
        keep, drop = (p["a"], p["b"]) if str(v.get("keep") or "A").upper() != "B" else (p["b"], p["a"])
        if keep in cur and drop in cur:
            k, d = cur[keep], cur[drop]
            if verdict in ("duplicate", "changed"):
                why = (f"the same fact as \"{beliefs.sentence(k)}\"" if verdict == "duplicate"
                       else f"replaced by \"{beliefs.sentence(k)}\"") + (f": {reason}" if reason else "")
                if await _invalidate(drop, why, triggered_by=keep):
                    changed.append(f"merged two memories into one: {beliefs.sentence(k)[:80]}")
            elif verdict == "disagree" and k.get("node") and v.get("value"):
                why = f"re-filed next to \"{beliefs.sentence(k)}\", which is about the same thing" + (
                    f": {reason}" if reason else "")
                moved = _restate({**d, "source": d.get("source") or d.get("actor")}, str(v["value"])[:200], why, at(),
                                 [f"review:{p['review']}"], node=k["node"], field=k["field"],
                                 links=[{"belief": drop, "kind": "refines"}])
                res, problems = await _assert([moved], now)
                if not problems and await _invalidate(drop, why, triggered_by=(res[0] or {}).get("belief")):
                    changed.append(f"noticed two people disagree: {beliefs.node_label(k['node'])} "
                                   f"{k['field'].replace('_', ' ')}")
        await _post(f"/reviews/{p['review']}/resolve",
                    {"resolution": {"verdict": verdict, "reason": reason, "model": MODEL_TAG}})

    for v in plan.get("conflicts") or []:
        try:
            c = rule["conflicts"][int(v["n"]) - 1]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        verdict, reason = str(v.get("verdict") or "open"), str(v.get("reason") or "")[:300]
        win, lose = (c["a"], c["b"]) if str(v.get("winner") or "A").upper() != "B" else (c["b"], c["a"])
        if win not in cur or lose not in cur or verdict == "open":
            continue
        w, l = cur[win], cur[lose]
        if verdict == "same_value" and l.get("node"):
            await _assert([_restate(l, w.get("value") or w.get("claim"),
                                    "the same thing in other words" + (f": {reason}" if reason else ""), at(),
                                    [f"conflict:{c['record']}"])], now)
            changed.append(f"realised two people meant the same thing: {beliefs.sentence(w)[:80]}")
        elif verdict == "both_true":
            await _post(f"/conflicts/{c['record']}/resolve",
                        {"resolution": {"verdict": "both_stand", "reason": reason, "model": MODEL_TAG}})
            changed.append(f"decided both can be true: {beliefs.sentence(w)[:60]} / {beliefs.sentence(l)[:60]}")
        elif verdict == "own_statement_wins":
            why = f"{beliefs.who(w.get('source'))} said it about themselves" + (f": {reason}" if reason else "")
            if await _invalidate(lose, why, triggered_by=win):
                await _post(f"/conflicts/{c['record']}/resolve",
                            {"resolution": {"verdict": "winner", "rule": "own_statement", "winner": win,
                                            "reason": reason, "model": MODEL_TAG}})
                changed.append(f"settled a disagreement: {beliefs.sentence(w)[:80]}")

    for e in plan.get("expired") or []:
        bid = rule["brefs"].get(str((e or {}).get("belief", "")).strip("[]")) if isinstance(e, dict) else None
        b = cur.get(bid)
        if not b or not _can_expire(b):
            continue                       # only plans expire, whatever the model listed
        reason = str(e.get("reason") or "its date has passed")[:300]
        past = " ".join(str(e.get("past") or "").split())[:200]
        if past and b.get("node"):
            await _assert([_restate(b, past, reason, at(), [f"expiry:{rule['day']}"])], now)
        elif past:
            note = {"store": "dialogue", "subject": "note-" + hashlib.sha256(memory._norm(past).encode()).hexdigest()[:10],
                    "claim": past, "class": "OBSERVATION", "actor": b.get("actor") or _self_actor(),
                    "valid_from": at(), "reason": reason,
                    "evidence": {"records": [f"expiry:{rule['day']}"], "beliefs": [{"belief": bid, "kind": "refines"}]},
                    "provenance": {"source": "sleep", "method": "sleep-rule", "confidence": 0.7}}
            res, problems = await _assert([note], now)
            if not problems:
                await _invalidate(bid, reason, triggered_by=(res[0] or {}).get("belief"))
        else:
            await _invalidate(bid, reason)
        changed.append(f"a plan is over: {beliefs.sentence(b)[:80]}")

    st["last_rule_day"] = rule["day"]
    st.pop("rule", None)
    ctx.save()


# -------------------------------------------------------------------------- job
async def reconcile_beliefs(ctx) -> dict:
    """Before the day's conversations are folded away (compact_conversations, order 80):
    cement notes and talk into beliefs, then rule on what is unsettled."""
    changed: list[str] = []
    waiting = []
    try:
        try:
            await _extract(ctx, changed)
        except sleepbatch.Pending:
            waiting.append("notes queued for the sleep batch")
        if ctx.alive():
            ctx.state["maintenance"] = await _maintain(ctx)
        if ctx.alive():
            try:
                await _rule(ctx, changed)
            except sleepbatch.Pending:
                waiting.append("rulings queued for the sleep batch")
        if changed and ctx.alive():
            # beliefs written after the first pass (rulings, re-filed facts) need embeddings too,
            # or recall's meaning search would not find them until tomorrow
            await _post("/jobs", {"steps": ["detect", "embed"]})
        if not waiting:
            ctx.state["at"] = time.time()
    except beliefs.Unavailable as e:
        waiting.append(f"belief store unreachable ({e}); notes are kept for the next sleep")
    waiting = "; ".join(waiting)
    await beliefs.refresh()
    for c in changed:
        activity.record("memory", f"({getattr(config, 'RECONCILE_TAG', 'sleep')}) {c}")
    ctx.log.info("sleep: beliefs: %s%s", changed or "no change", f" [{waiting}]" if waiting else "")
    out = {"changed": changed, "beliefs": len(beliefs.current())}
    if waiting:
        out["waiting"] = waiting
    return out



run = reconcile_beliefs
