"""Named animations from Vector's own animation library (anim_manifest.json, 1208 clips)."""
import json
import re
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from . import robot

_MANIFEST = {a["name"]: a["length_ms"] for a in
             json.loads((Path(__file__).parent / "data" / "anim_manifest.json").read_text())}
MAX_MS = 26_000

# Curated catalog from the animations skill: "- anim_x (3.6), anim_y (3.2): description"
_CATALOG: list[tuple[str, str]] = []
_skill = Path(__file__).resolve().parent.parent / "skills" / "animations" / "SKILL.md"
if _skill.exists():
    section = ""
    for line in _skill.read_text().splitlines():
        if line.startswith("### "):
            section = line[4:]
        elif line.lstrip().startswith("- anim_") or line.lstrip().startswith("anim_"):
            desc = (line.split(":", 1)[1].strip() if ":" in line else "") + " " + section
            for n in re.findall(r"anim_[a-z0-9_\-]+", line):
                _CATALOG.append((n, desc))


def _suggest(wanted: str, k: int = 5) -> list[str]:
    words = [w for w in re.split(r"[^a-z]+", wanted.lower().replace("anim_", "")) if len(w) > 2]
    hits = [n for n, d in _CATALOG if any(w in n or w in d.lower() for w in words)]
    return list(dict.fromkeys(hits))[:k]


def _resolve(name: str) -> str | None:
    name = name.strip()
    for cand in (name, f"anim_{name}"):
        if cand in _MANIFEST:
            return cand
    return None


@tool
async def play_animation(name: Annotated[str, Field(description=
        "Exact animation name from the animations skill catalog, e.g. anim_greeting_hello_01")]) -> str:
    """Play one of Vector's named animations (face, sound and body together). Load the
    'animations' skill first to pick the right one. It plays BEFORE you speak, and you
    stay quiet while it runs, so keep the reply after a long animation short."""
    real = _resolve(name)
    if real is None:
        sugg = _suggest(name)
        return (f"error: no animation called {name!r}. Never guess names: load the "
                f"'animations' skill and pick from its catalog."
                + (f" Matching catalog entries: {', '.join(sugg)}" if sugg else ""))
    ms = _MANIFEST[real]
    if ms > MAX_MS:
        return f"error: {real} is {ms/1000:.0f}s long, too long to play here"
    await robot.lua(f"assumeBehaviorControl(20) playAnimation({robot.lua_str(real)})")
    return f"played {real} ({ms/1000:.1f}s)"
