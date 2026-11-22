"""Agent Skills: folders with a SKILL.md (YAML-ish frontmatter: name, description).

Only names + descriptions go into the system prompt; the model calls load_skill(name)
to pull the full instructions into the conversation when a request needs them
(progressive disclosure keeps the per-turn prompt small and fast).

Two sources:
  * built-in skills shipped with the agent (vector-agent/skills/)
  * LEARNED skills Vector writes himself (DATA_DIR/skills/, survive rebuilds): when someone
    says "now you've learned to watch video games", he saves the recipe as a skill with
    create_skill. Learned skills are instructions only: they can't add tools or bypass
    any limit, and can't replace a built-in skill.
"""
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import config

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
LEARNED_DIR = config.DATA_DIR / "skills"
MAX_LEARNED = 50
_LOCK = threading.Lock()


def _parse(path: Path) -> dict | None:
    text = path.read_text()
    if not text.startswith("---"):
        return None
    try:
        _, front, body = text.split("---", 2)
    except ValueError:
        return None
    meta = {}
    for line in front.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    if "name" not in meta or "description" not in meta:
        return None
    meta["body"] = body.strip()
    meta["tools"] = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
    return meta


def tools_for(skill_names) -> list[str]:
    """Tool names unlocked by these skills, in skill order (stable for prompt caching)."""
    out: list[str] = []
    for n in skill_names:
        for t in (SKILLS.get(n) or {}).get("tools", []):
            if t not in out:
                out.append(t)
    return out


def load_all() -> dict[str, dict]:
    skills = {}
    for p in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        if (s := _parse(p)) is not None:
            skills[s["name"]] = s
    for p in sorted(LEARNED_DIR.glob("*/SKILL.md")):
        if (s := _parse(p)) is not None and s["name"] not in skills:
            s["learned"] = "true"
            skills[s["name"]] = s
    return skills


SKILLS = load_all()


def reload() -> None:
    with _LOCK:
        fresh = load_all()
        SKILLS.clear()
        SKILLS.update(fresh)


def catalog() -> str:
    out = []
    for s in SKILLS.values():
        tag = " (learned)" if s.get("learned") else ""
        out.append(f"- {s['name']}{tag}: {s['description']}")
    return "\n".join(out)


@tool
def load_skill(name: Annotated[str, Field(description="Skill name from the SKILLS list")]) -> str:
    """Load a skill's full instructions. Do this before the first use of that skill's
    tools in a conversation; once loaded it stays in the conversation."""
    s = SKILLS.get(name.strip())
    if s is None:
        return f"error: no skill {name!r}; available: {', '.join(SKILLS)}"
    tools = f"Tools now available: {', '.join(s['tools'])}.\n" if s.get("tools") else ""
    return f"SKILL {s['name']} loaded. {tools}Follow these instructions:\n\n{s['body']}"


# ----------------------------------------------------------------- learned skills
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.replace('"', "'").split())[:limit]


def _known_tools() -> set[str]:
    from .tools import TOOLS_BY_NAME
    return set(TOOLS_BY_NAME)


def _check_tools(tools: list[str] | None) -> tuple[list[str], str | None]:
    tools = [t.strip() for t in (tools or []) if t and t.strip()]
    bad = [t for t in tools if t not in _known_tools()]
    if bad:
        return [], f"error: no such tool(s): {', '.join(bad)}"
    return tools[:12], None


def _write_learned(name: str, description: str, instructions: str, taught_by: str, created: str,
                   tools: list[str] | None = None) -> None:
    d = LEARNED_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    body = instructions.strip().replace("\r", "")
    tl = f"tools: {', '.join(tools)}\n" if tools else ""
    text = (f"---\nname: {name}\ndescription: {description}\n{tl}learned: true\n"
            f"taught_by: {taught_by}\ncreated: {created}\n"
            f"updated: {datetime.now(config.TIMEZONE).isoformat(timespec='minutes')}\n---\n{body}\n")
    tmp = d / "SKILL.md.tmp"
    tmp.write_text(text)
    tmp.replace(d / "SKILL.md")


@tool
def create_skill(
        name: Annotated[str, Field(description="Short name, e.g. 'watch-video-games'")],
        description: Annotated[str, Field(description=(
            "One line: what it's for, with the phrases people will use, e.g. "
            "'Watch someone play a video game on the TV and react (\"watch me play\", \"watch my game\")'"))],
        instructions: Annotated[str, Field(description=(
            "The recipe, written to your future self: when to use it, exact steps, which "
            "routines/maneuvers/schedule settings to use with the values that worked, and gotchas"))],
        tools: Annotated[list[str], Field(description=(
            "Tool names the recipe uses, so loading this skill makes them available, "
            "e.g. ['schedule_action', 'look']"))] = []) -> str:
    """Save something you just learned as a new SKILL of your own, so next time you can
    do it straight away. Use when someone says you've learned something ("now you know
    how to ...") or after you worked out something new that went well. Load the
    'skill-making' skill first."""
    n = _slug(name)
    desc = _one_line(description, 300)
    if not n or not desc or len(instructions.strip()) < 20:
        return "error: give a name, a one-line description and real instructions"
    if len(instructions) > 4000:
        return "error: keep the instructions under 4000 characters"
    if n in SKILLS and not SKILLS[n].get("learned"):
        return f"error: {n} is one of your built-in skills; pick another name or use update_skill on a learned one"
    if n in SKILLS:
        return f"error: you already have a learned skill called {n}; use update_skill"
    if len([s for s in SKILLS.values() if s.get("learned")]) >= MAX_LEARNED:
        return f"error: you know {MAX_LEARNED} learned skills already; forget one first"
    tl, err = _check_tools(tools)
    if err:
        return err
    from . import faces
    _write_learned(n, desc, instructions, faces.front_name() or "someone",
                   datetime.now(config.TIMEZONE).isoformat(timespec="minutes"), tl)
    reload()
    return f"learned a new skill: {n}. It's in your skills list now."


@tool
def update_skill(
        name: str,
        description: Annotated[str, Field(description="New one-line description, or empty to keep it")] = "",
        instructions: Annotated[str, Field(description="Full new instructions, or empty to keep them")] = "",
        tools: Annotated[list[str], Field(description="New full list of tool names, or empty to keep them")] = []) -> str:
    """Improve one of your learned skills (built-in skills can't be changed)."""
    s = SKILLS.get(_slug(name))
    if s is None or not s.get("learned"):
        return f"error: no learned skill called {name!r}"
    if instructions and len(instructions) > 4000:
        return "error: keep the instructions under 4000 characters"
    tl, err = _check_tools(tools) if tools else (s.get("tools", []), None)
    if err:
        return err
    _write_learned(s["name"], _one_line(description, 300) or s["description"], instructions or s["body"],
                   s.get("taught_by", "someone"), s.get("created", ""), tl)
    reload()
    return f"updated skill {s['name']}"


@tool
def forget_skill(name: str) -> str:
    """Forget one of your learned skills (built-in skills can't be removed)."""
    s = SKILLS.get(_slug(name))
    if s is None or not s.get("learned"):
        return f"error: no learned skill called {name!r}"
    d = LEARNED_DIR / s["name"]
    for p in d.glob("*"):
        p.unlink()
    d.rmdir()
    reload()
    return f"forgot the skill {s['name']}"


SKILL_TOOLS = [create_skill, update_skill, forget_skill]
