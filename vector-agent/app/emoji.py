"""Show big colour emoji on Vector's face screen (Noto Color Emoji, on black)."""
from typing import Annotated

from langchain_core.tools import tool
from PIL import Image, ImageDraw, ImageFont, features
from pydantic import Field

from . import config
from .face import _display, _save

W, H = config.FACE_W, config.FACE_H
EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
_SIZE = 109  # Noto Color Emoji is a bitmap font: 109px is its native strike
_LAYOUT = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC


_SKIP = {0xFE0E, 0xFE0F, 0x20E3} | set(range(0x1F3FB, 0x1F400))  # style selectors, skin tones
_ZWJ = 0x200D


def _units(text: str) -> list[str]:
    """Split into single renderable emoji. Pillow here has no text shaper (raqm), so
    sequences can't be joined: skin tones/selectors are dropped, ZWJ sequences keep their
    first emoji, regional-indicator flags are skipped."""
    out, skip_next = [], False
    for ch in text:
        cp = ord(ch)
        if skip_next:
            skip_next = False
            continue
        if cp == _ZWJ:
            skip_next = True
            continue
        if cp in _SKIP or ch.isspace() or 0x1F1E6 <= cp <= 0x1F1FF:
            continue
        out.append(ch)
    return out


def _glyph(ch: str, font: ImageFont.FreeTypeFont) -> Image.Image | None:
    canvas = Image.new("RGBA", (_SIZE * 2, _SIZE * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((_SIZE // 2, _SIZE // 2), ch, font=font, embedded_color=True)
    bbox = canvas.getchannel("A").getbbox()
    return canvas.crop(bbox) if bbox else None


def render(emoji: str) -> Image.Image:
    units = _units(emoji)
    if not units:
        raise ValueError("no emoji given (flags and some special sequences aren't supported)")
    if len(units) > 3:
        raise ValueError("too many: show at most 3 emoji")
    font = ImageFont.truetype(EMOJI_FONT, _SIZE, layout_engine=_LAYOUT)
    glyphs = [g for g in (_glyph(u, font) for u in units) if g is not None]
    if not glyphs:
        raise ValueError("those characters didn't render as emoji")
    gap = 14
    strip_w = sum(g.width for g in glyphs) + gap * (len(glyphs) - 1)
    strip_h = max(g.height for g in glyphs)
    strip = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
    x = 0
    for g in glyphs:
        strip.alpha_composite(g, (x, (strip_h - g.height) // 2))
        x += g.width + gap
    scale = min((W - 8) / strip.width, (H - 6) / strip.height)
    strip = strip.resize((max(1, int(strip.width * scale)), max(1, int(strip.height * scale))),
                         Image.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    out.alpha_composite(strip, ((W - strip.width) // 2, (H - strip.height) // 2))
    return out.convert("RGB")


@tool
async def show_emoji(
        emoji: Annotated[str, Field(description="1 to 3 emoji characters, e.g. '🐧' or '☀️🌈' or '🍕❤️'")],
        seconds: Annotated[float, Field(description="How long to show it, 2 to 8 seconds")] = 4) -> str:
    """Show big colour emoji on your face screen (bright on black). Load the 'emoji' skill
    first. Shown BEFORE you speak."""
    try:
        img = render(emoji)
    except Exception as e:
        return f"error: {e}"
    await _display(_save(img), seconds)
    return f"showed {emoji}"
