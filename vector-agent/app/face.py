"""Vector's face screen (Vector 1.0: 184x96) as a display.

Images are written to FACE_DIR (shared with the wire-pod container at /face) and shown
with wire-pod's Lua showImage(), which needs exact screen-size images (it doesn't scale).

Web pictures come from Tavily image search, then get scored for how well they'll read
on a tiny 2:1 screen: sensible aspect ratio, decent resolution, a single clear subject,
and no panoramas, maps, charts, collages or text-heavy images.
"""
import asyncio
import contextlib
import io
import math
import re
import time
import uuid
from typing import Annotated

import httpx
from langchain_core.tools import tool
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from pydantic import Field

from . import config, robot

W, H = config.FACE_W, config.FACE_H
TARGET = W / H  # ~1.92
_http = httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3), follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (vector-agent face display)"})

_BAD = re.compile(r"panoram|360|map\b|maps\b|diagram|chart|infographic|graph\b|table\b|"
                  r"screenshot|poster|logo|collage|comparison|\bvs\b|text|quote|meme|"
                  r"satellite|aerial view of the|cross.?section|illustration of the process", re.I)
_GOOD = re.compile(r"close.?up|portrait|single|isolated|in the wild|photo of a|cute", re.I)


# --------------------------------------------------------------------------- display
def _prepare(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, (W, H), Image.LANCZOS, centering=(0.5, 0.45))
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Color(img).enhance(1.25)
    return ImageEnhance.Sharpness(img).enhance(1.3)


def _save(img: Image.Image) -> str:
    # JPEG, not PNG: wire-pod's binary only registers Go's JPEG decoder, so Lua
    # showImage() fails on PNG with "image: unknown format" (and silently shows nothing).
    config.FACE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"face-{int(time.time())}-{uuid.uuid4().hex[:6]}.jpg"
    img.convert("RGB").save(config.FACE_DIR / name, "JPEG", quality=95, subsampling=0)
    old = sorted(config.FACE_DIR.glob("face-*.jpg"))
    for p in old[:-30]:  # keep the last 30
        p.unlink(missing_ok=True)
    for p in config.FACE_DIR.glob("*.png"):  # leftovers from the PNG era
        p.unlink(missing_ok=True)
    return name


async def _display(name: str, seconds: float) -> None:
    ms = int(max(1.5, min(seconds, 15)) * 1000)
    path = robot.lua_str(f"{config.FACE_DIR_IN_WIREPOD}/{name}")
    await robot.lua(f"assumeBehaviorControl(20) showImage({path}, {ms}) sleep({ms})")


# ------------------------------------------------------------------------ web search
async def _tavily_images(query: str) -> list[dict]:
    if not config.TAVILY_API_KEY:
        raise RuntimeError("no Tavily key configured")
    r = await _http.post("https://api.tavily.com/search", timeout=15,
                         headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
                         json={"query": query, "search_depth": "basic", "max_results": 3,
                               "include_images": True, "include_image_descriptions": True})
    r.raise_for_status()
    out = []
    for i in r.json().get("images", []):
        out.append(i if isinstance(i, dict) else {"url": i, "description": ""})
    return out


async def _fetch(url: str) -> Image.Image | None:
    if re.search(r"\.(svg|gif)(\?|$)", url, re.I):
        return None
    try:
        r = await _http.get(url)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            return None
        if len(r.content) > 8_000_000:
            return None
        img = Image.open(io.BytesIO(r.content))
        img.load()
        return img
    except Exception:
        return None


def _score(img: Image.Image, desc: str, url: str, rank: int) -> float | None:
    w, h = img.size
    if w < 200 or h < 110:
        return None
    ratio = w / h
    if ratio > 3.0 or ratio < 0.75:  # panoramas / tall portraits crop badly
        return None
    score = -2.0 * abs(math.log(ratio / TARGET)) - 0.15 * rank
    text = f"{desc} {url}"
    if _BAD.search(text):
        score -= 2.5
    if _GOOD.search(text):
        score += 0.5
    if min(w, h) >= 400:
        score += 0.3
    return score


@tool
async def show_picture(
        subject: Annotated[str, Field(description=
            "What to show, phrased as a short image search for ONE clear subject, e.g. "
            "'emperor penguin close up photo', 'Antarctica icebergs photo', 'Eiffel Tower photo'")],
        seconds: Annotated[float, Field(description="How long to show it, 3 to 12 seconds")] = 5) -> str:
    """Find a picture on the web and show it on Vector's face screen (a tiny 184x96 display).
    Load the 'face-display' skill first. The picture is shown BEFORE you speak."""
    if config.SHOW_PICTURE_SUFFIX and "photo" not in subject.lower():
        subject = f"{subject} {config.SHOW_PICTURE_SUFFIX}"
    # Look busy while searching (he isn't speaking yet, so he can use his face/body).
    thinking = asyncio.create_task(robot.lua(
        'assumeBehaviorControl(20) playAnimation("anim_knowledgegraph_searching_01")'))
    try:
        cands = (await _tavily_images(subject))[:8]
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(thinking, timeout=5)
    if not cands:
        return f"error: no pictures found for {subject!r}"
    imgs = await asyncio.gather(*(_fetch(c["url"]) for c in cands))
    scored = []
    for rank, (c, img) in enumerate(zip(cands, imgs)):
        if img is not None and (s := _score(img, c.get("description", ""), c["url"], rank)) is not None:
            scored.append((s, rank, img, c))
    if not scored:
        return f"error: found pictures for {subject!r} but none would look good on my small screen"
    s, rank, img, c = max(scored, key=lambda x: x[0])
    name = _save(_prepare(img))
    await _display(name, seconds)
    desc = (c.get("description") or "").strip()
    return f"showed a picture of {subject!r}" + (f" ({desc[:160]})" if desc else "")


# ------------------------------------------------------------------------------ text
def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(config.FACE_FONT, size)
    except OSError:
        return ImageFont.load_default()


def _render_text(text: str, color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    words = text.split()
    for size in range(56, 11, -2):  # biggest font that fits in <= 3 lines
        f = _font(size)
        lines, cur = [], ""
        for w in words:
            t = f"{cur} {w}".strip()
            if d.textlength(t, font=f) <= W - 8:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        lh = f.getbbox("Ag")[3] + 2
        if len(lines) <= 3 and all(d.textlength(l, font=f) <= W - 8 for l in lines) and lh * len(lines) <= H - 4:
            y = (H - lh * len(lines)) // 2
            for l in lines:
                d.text(((W - d.textlength(l, font=f)) / 2, y), l, font=f, fill=color)
                y += lh
            return img
    d.text((4, 4), text[:40], font=_font(12), fill=color)
    return img


_COLORS = {"white": (255, 255, 255), "teal": (0, 230, 200), "yellow": (255, 220, 0),
           "red": (255, 60, 60), "green": (60, 255, 90), "blue": (80, 160, 255),
           "pink": (255, 120, 200), "orange": (255, 150, 0)}


@tool
async def show_text(
        text: Annotated[str, Field(description="A few words or a number, max ~25 characters reads best")],
        seconds: Annotated[float, Field(description="How long to show it, 2 to 10 seconds")] = 4,
        color: Annotated[str, Field(description="white, teal, yellow, red, green, blue, pink or orange")] = "teal") -> str:
    """Show big text on Vector's face screen: a number, a word, a short answer, a spelling,
    a score. Load the 'face-display' skill first. Shown BEFORE you speak."""
    name = _save(_render_text(text.strip()[:60], _COLORS.get(color, _COLORS["teal"])))
    await _display(name, seconds)
    return f"showed text {text!r}"


@tool
async def show_last_view(seconds: Annotated[float, Field(description="2 to 10 seconds")] = 4) -> str:
    """Show the last picture Vector's camera took (from the look tool) on his face screen.
    Use when the user asks 'show me what you see'. Call look first if nothing was taken
    in this conversation."""
    src = config.FACE_DIR / "last_view.jpg"
    if not src.exists():
        return "error: no camera picture yet, call look first"
    name = _save(_prepare(Image.open(src)))
    await _display(name, seconds)
    return "showed the last camera view"
