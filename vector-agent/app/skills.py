"""Agent Skills: folders with a SKILL.md (YAML-ish frontmatter: name, description).

Only names + descriptions go into the system prompt; the model calls load_skill(name)
to pull the full instructions into the conversation when a request needs them
(progressive disclosure keeps the per-turn prompt small and fast).
"""
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _parse(path: Path) -> dict | None:
    text = path.read_text()
    if not text.startswith("---"):
        return None
    _, front, body = text.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    if "name" not in meta or "description" not in meta:
        return None
    meta["body"] = body.strip()
    return meta


def load_all() -> dict[str, dict]:
    skills = {}
    for p in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        if (s := _parse(p)) is not None:
            skills[s["name"]] = s
    return skills


SKILLS = load_all()


def catalog() -> str:
    return "\n".join(f"- {s['name']}: {s['description']}" for s in SKILLS.values())


@tool
def load_skill(name: Annotated[str, Field(description="Skill name from the SKILLS list")]) -> str:
    """Load a skill's full instructions. Do this before the first use of that skill's
    tools in a conversation; once loaded it stays in the conversation."""
    s = SKILLS.get(name.strip())
    if s is None:
        return f"error: no skill {name!r}; available: {', '.join(SKILLS)}"
    return f"SKILL {s['name']} loaded. Follow these instructions:\n\n{s['body']}"
