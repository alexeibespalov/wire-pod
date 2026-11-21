"""Vector draws his own pictures: the model writes an SVG for the 184x96 face screen,
we sanitise it, render it with CairoSVG, and show it like any other face image.

The SVG is model output, so it is treated as untrusted: no external references
(href/url to files or the web), no scripts, no embedded images or foreign content,
no DOCTYPE/entities, size-capped. CairoSVG also runs with unsafe=False.
"""
import io
import re
from typing import Annotated

import cairosvg
from langchain_core.tools import tool
from PIL import Image, ImageEnhance
from pydantic import Field

from . import config
from .face import _display, _save

W, H = config.FACE_W, config.FACE_H
MAX_SVG = 30_000

_FORBIDDEN_TAGS = re.compile(r"<\s*/?\s*(script|image|foreignObject|use|iframe|object|embed|style|a|feImage)\b[^>]*>",
                             re.I | re.S)
_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*(\[[^\]]*\])?\s*>|<!ENTITY[^>]*>", re.I | re.S)
_EXTERNAL = re.compile(r"""(?:xlink:)?href\s*=\s*["'][^"']*["']|url\(\s*["']?(?!#)[^)]*\)""", re.I)
_EVENT_ATTR = re.compile(r"""\son[a-z]+\s*=\s*["'][^"']*["']""", re.I)
# A full-canvas background rect (width 100%/184 and height 100%/96) -> always black.
_BG_RECT = re.compile(r"<rect\b(?=[^>]*\bwidth\s*=\s*[\"'](?:184|100%)[\"'])"
                      r"(?=[^>]*\bheight\s*=\s*[\"'](?:96|100%)[\"'])[^>]*>", re.I)
_FILL = re.compile(r"""\bfill\s*=\s*["'][^"']*["']""", re.I)


def sanitize(svg: str) -> str:
    svg = svg.strip()
    m = re.search(r"<svg\b.*</svg>", svg, re.I | re.S)
    if not m:
        raise ValueError("no <svg>...</svg> element found")
    svg = m.group(0)
    if len(svg) > MAX_SVG:
        raise ValueError(f"SVG too large ({len(svg)} chars, max {MAX_SVG})")
    svg = _DOCTYPE.sub("", svg)
    svg = _FORBIDDEN_TAGS.sub("", svg)
    svg = _EXTERNAL.sub("", svg)
    svg = _EVENT_ATTR.sub("", svg)
    # House style: bright colours on BLACK, always. Force any full-canvas background black.
    svg = _BG_RECT.sub(lambda m: _FILL.sub('fill="#000"', m.group(0)) if _FILL.search(m.group(0))
                       else m.group(0)[:-1].rstrip("/") + ' fill="#000"/>', svg)
    if "xmlns=" not in svg[:300]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


def _border_luminance(img: Image.Image) -> float:
    g = img.convert("L")
    w, h = g.size
    px = [g.getpixel((x, y)) for x in range(0, w, 4) for y in (0, 1, h - 2, h - 1)]
    px += [g.getpixel((x, y)) for y in range(0, h, 4) for x in (0, 1, w - 2, w - 1)]
    return sum(px) / len(px)


def render(svg: str) -> Image.Image:
    png = cairosvg.svg2png(bytestring=sanitize(svg).encode(), output_width=W, output_height=H,
                           unsafe=False)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))  # transparent -> black (screen off)
    img = Image.alpha_composite(bg, img).convert("RGB")
    return ImageEnhance.Color(img).enhance(1.25)  # make colours pop on the glowing screen


@tool
async def draw_picture(
        svg: Annotated[str, Field(description=
            "A complete SVG document drawn for the 184x96 face screen: "
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 184 96">...</svg>. '
            "Black background, bold bright shapes, thick strokes. No text smaller than 16px, "
            "no images or external links.")],
        seconds: Annotated[float, Field(description="How long to show it, 3 to 12 seconds")] = 5) -> str:
    """Draw your own picture and show it on your face screen. Load the 'drawing' skill
    first. Shown BEFORE you speak."""
    try:
        img = render(svg)
    except Exception as e:
        return f"error: couldn't draw that SVG ({e}). Fix it and try once more, simpler."
    lum = sum(img.convert("L").getdata()) / (W * H)
    if lum < 2:
        return "error: the drawing came out completely black. Use bright fills/strokes and try again."
    if _border_luminance(img) > 60:
        return ("error: the background isn't black. House style is bright colours on a BLACK "
                "background: redraw with the black background rect and nothing light behind "
                "your subject.")
    name = _save(img)
    await _display(name, seconds)
    return "drew it and showed it on your face screen"
