"""Draw one frame of the dashboard: the state in, a 40x14 grid of cells out.

Pure (no I/O): `render(state, wall, t)` takes the wall clock for the time shown and a
monotonic `t` for the animations, so tests can freeze both. The layout follows the
approved design (the "Cortana VFD Dashboard" canvas):

    row 0       Wi-Fi bars + network, BT + speaker name
    rows 1-4    7-segment clock (half-block "pixels") | date, weather glyph, °C, sky, RH
    row 5       now playing
    row 6       BELIEFS CORTANA: n VECTOR: n | A2A
    rows 7-12   four tiles: Reception lights, Reception temp / Living Room lights, Heat boost
    row 13      Cortana's bar: dark; grows out from the centre and pulses while listening;
                THINKING / TOOL CALL: x / SKILL CALL: y
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

W, H = 40, 14
Rgb = tuple[int, int, int]


def hex_rgb(h: str) -> Rgb:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a: Rgb, b: Rgb, t: float) -> Rgb:
    """t of a over b (t=1: a)."""
    t = max(0.0, min(1.0, t))
    return tuple(round(x * t + y * (1 - t)) for x, y in zip(a, b))  # type: ignore[return-value]


BG: Rgb = (2, 8, 6)


@dataclass(frozen=True)
class Palette:
    lit: Rgb
    mid: Rgb
    dim: Rgb
    ghost: Rgb

    @classmethod
    def of(cls, lit_hex: str = "#6dffd2") -> "Palette":
        lit = hex_rgb(lit_hex)
        return cls(lit, mix(lit, BG, 0.62), mix(lit, BG, 0.36), mix(lit, BG, 0.13))


def wave(t: float, period: float, low: float, steps: int = 6) -> float:
    """1 at t=0, easing down to `low` at half the period and back, in `steps` levels (a
    stepped fade repeats frames, so the screen redraws far less often on the Zero)."""
    v = 0.5 + 0.5 * math.cos(2 * math.pi * t / period)
    return low + (1 - low) * round(v * steps) / steps


# --------------------------------------------------------------------------- the state
@dataclass
class State:
    wifi_ssid: str | None = None
    wifi_quality: int | None = None            # 0-100
    bt_name: str | None = None
    bt_connected: bool | None = None
    weather: dict | None = None                # condition, temperature, humidity
    playing: dict | None = None                # state (play/pause/stop), title, artist, elapsed, at
    beliefs: dict = field(default_factory=lambda: {"cortana": None, "vector": None})
    a2a: bool = False
    agent: str = "idle"                        # idle / listening / thinking / tool / skill
    agent_detail: str = ""
    agent_since: float = 0.0                   # monotonic
    lights: dict = field(default_factory=dict)  # "reception"/"living" -> {on, pct}
    climate: dict | None = None                # current, setpoint, action, mode
    boost_until: float | None = None           # wall clock
    ha_ok: bool = False
    flash: dict = field(default_factory=dict)  # tile index -> monotonic time the error flash ends


# --------------------------------------------------------------------------- the frame
Cell = tuple[str, Rgb, Rgb, bool]              # char, fg, bg, bold


class Frame:
    def __init__(self, pal: Palette):
        self.pal = pal
        self.cells: list[list[Cell]] = [[(" ", pal.lit, BG, False) for _ in range(W)] for _ in range(H)]

    def put(self, x: int, y: int, text: str, fg: Rgb, bg: Rgb = BG, bold: bool = False) -> int:
        """Write text from (x, y), clipped to the row; returns the column after it."""
        if not 0 <= y < H:
            return x
        for ch in text:
            if 0 <= x < W:
                self.cells[y][x] = (ch, fg, bg, bold)
            x += 1
        return x

    def put_right(self, right: int, y: int, text: str, fg: Rgb, bg: Rgb = BG, bold: bool = False) -> int:
        """Write text ending at column `right` (inclusive); returns its first column."""
        x = right - len(text) + 1
        self.put(x, y, text, fg, bg, bold)
        return x

    def row_text(self, y: int) -> str:
        return "".join(c[0] for c in self.cells[y])

    def text(self) -> str:
        return "\n".join(self.row_text(y) for y in range(H))


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


# --------------------------------------------------------------------------- the clock
# 7-segment digits on a 4x7 grid of "pixels"; two pixel rows per text row (▀ / ▄).
SEG = {"a": [(1, 0), (2, 0)], "b": [(3, 1), (3, 2)], "c": [(3, 4), (3, 5)], "d": [(1, 6), (2, 6)],
       "e": [(0, 4), (0, 5)], "f": [(0, 1), (0, 2)], "g": [(1, 3), (2, 3)]}
DIGITS = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
          "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg"}
CLOCK_W, CLOCK_PX_H = 21, 8
OFF, GHOST, LIT, COLON = 0, 1, 2, 3


def clock_pixels(hhmm: str) -> list[list[int]]:
    pix = [[OFF] * CLOCK_W for _ in range(CLOCK_PX_H)]
    for ox, ch in zip((0, 5, 12, 17), hhmm):
        on = DIGITS.get(ch, "")
        for seg, pts in SEG.items():
            for x, y in pts:
                pix[y][ox + x] = LIT if seg in on else GHOST
    pix[2][10] = pix[4][10] = COLON
    return pix


def draw_clock(f: Frame, hhmm: str, x0: int, y0: int, t: float, dark: bool) -> None:
    p = f.pal
    colon = mix(p.lit, BG, wave(t, 2.0, 0.18))
    color = {OFF: None, GHOST: p.ghost, LIT: p.dim if dark else p.lit, COLON: p.dim if dark else colon}
    pix = clock_pixels(hhmm)
    for r in range(CLOCK_PX_H // 2):
        for x in range(CLOCK_W):
            top, bot = color[pix[2 * r][x]], color[pix[2 * r + 1][x]]
            if top and bot:
                f.put(x0 + x, y0 + r, "▀", top, bot)
            elif top:
                f.put(x0 + x, y0 + r, "▀", top)
            elif bot:
                f.put(x0 + x, y0 + r, "▄", bot)


# --------------------------------------------------------------------------- the weather
# 10 columns x 3 rows. A line is a list of (text, tone, effect); a second frame (b) flips
# with the first every 0.6 s. Effects: "breathe" (fades), drift (whole glyph, 1 column).
_CLOUD = [[("   .--.   ", "mid", "")], [(" (______) ", "lit", "")]]
GLYPHS: dict[str, dict] = {
    "cloudy": {"drift": True, "lines": [
        {"a": [("   .--.   ", "mid", "")]}, {"a": [(" .(    ). ", "lit", "")]}, {"a": [("(___.___) ", "lit", "")]}]},
    "sunny": {"lines": [
        {"a": [("   \\ | /  ", "mid", "breathe")]},
        {"a": [(" ─ ", "mid", "breathe"), (" (●) ", "lit", ""), ("─ ", "mid", "breathe")]},
        {"a": [("   / | \\  ", "mid", "breathe")]}]},
    "partlycloudy": {"lines": [
        {"a": [("   \\ |    ", "mid", "breathe")]},
        {"a": [(" ─ ", "mid", "breathe"), ("(.--.  ", "lit", "")]},
        {"a": [("  .(____).", "lit", "")]}]},
    "rainy": {"lines": [{"a": _CLOUD[0]}, {"a": _CLOUD[1]},
                        {"a": [("  ' ' ' ' ", "lit", "")], "b": [(" ' ' ' '  ", "lit", "")]}]},
    "snowy": {"lines": [{"a": _CLOUD[0]}, {"a": _CLOUD[1]},
                        {"a": [("  * * * * ", "lit", "")], "b": [(" * * * *  ", "lit", "")]}]},
    "clear-night": {"lines": [
        {"a": [("  .    *  ", "mid", "")], "b": [("  *    .  ", "mid", "")]},
        {"a": [("    ◐     ", "lit", "")]},
        {"a": [(" *     .  ", "mid", "")], "b": [(" .     *  ", "mid", "")]}]},
}
# Home Assistant's conditions -> (glyph, label of at most 7 characters)
CONDITIONS = {
    "sunny": ("sunny", "SUNNY"), "clear-night": ("clear-night", "CLEAR"),
    "partlycloudy": ("partlycloudy", "P.CLOUD"), "cloudy": ("cloudy", "CLOUDY"),
    "fog": ("cloudy", "FOG"), "windy": ("cloudy", "WINDY"), "windy-variant": ("cloudy", "WINDY"),
    "rainy": ("rainy", "RAIN"), "pouring": ("rainy", "POURING"), "lightning": ("rainy", "STORM"),
    "lightning-rainy": ("rainy", "STORM"), "hail": ("rainy", "HAIL"),
    "snowy": ("snowy", "SNOW"), "snowy-rainy": ("snowy", "SLEET"), "exceptional": ("cloudy", "ALERT"),
}


def draw_weather(f: Frame, weather: dict | None, x0: int, y0: int, t: float) -> None:
    p = f.pal
    cond = (weather or {}).get("condition", "")
    name, label = CONDITIONS.get(cond, ("cloudy", cond.upper()[:7] or "--"))
    g = GLYPHS[name]
    dark = weather is None
    tone = {"lit": p.lit, "mid": p.mid}
    shift = 1 if g.get("drift") and not dark and int(t / 2.0) % 2 else 0
    flip_b = not dark and int(t / 0.6) % 2 == 1
    breathe = mix(p.mid, BG, wave(t, 3.0, 0.45))
    for r, line in enumerate(g["lines"]):
        segs = line["b"] if flip_b and "b" in line else line["a"]
        x = x0
        for text, tn, fx in segs:
            fg = p.ghost if dark else (breathe if fx == "breathe" else tone[tn])
            x = f.put(x, y0 + r, text, fg)
        if shift:   # drift: the glyph moves one column right, inside its 10 columns
            row = f.cells[y0 + r]
            row[x0:x0 + 10] = [(" ", p.lit, BG, False)] + row[x0:x0 + 9]
    ix = x0 + 11
    temp = (weather or {}).get("temperature")
    hum = (weather or {}).get("humidity")
    f.put(ix, y0, "--.-°C" if temp is None else f"{float(temp):4.1f}°C".rjust(6), p.dim if temp is None else p.lit,
          bold=True)
    f.put(ix, y0 + 1, "NO DATA" if dark else label, p.dim if dark else p.mid)
    f.put(ix, y0 + 2, "RH --%" if hum is None else f"RH {round(float(hum))}%", p.dim if hum is None else p.mid)


# --------------------------------------------------------------------------- the rows
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def draw_status(f: Frame, s: State) -> None:
    p = f.pal
    q = s.wifi_quality
    lit_bars = 0 if q is None else sum(q >= th for th in (1, 30, 55, 80))
    for i, ch in enumerate("▂▄▆█"):
        f.put(1 + i, 0, ch, p.lit if i < lit_bars else p.ghost)
    f.put(5, 0, " " + fit((s.wifi_ssid or "NO WI-FI").upper(), 14), p.mid if s.wifi_ssid else p.dim)
    name = fit((s.bt_name or "NO SPEAKER").upper(), 16)
    x = f.put_right(38, 0, name, p.lit if s.bt_connected else p.dim)
    f.put_right(x - 1, 0, "BT ", p.mid if s.bt_connected else p.dim)


def _mmss(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def draw_playing(f: Frame, s: State, wall: float, t: float) -> None:
    p = f.pal
    np = s.playing or {}
    state = np.get("state", "stop")
    if state not in ("play", "pause") or not (np.get("title") or np.get("artist")):
        f.put(1, 5, "♪", p.dim)
        f.put(3, 5, "NOTHING PLAYING", p.dim)
        return
    elapsed = float(np.get("elapsed") or 0) + (wall - np.get("at", wall) if state == "play" else 0)
    right = _mmss(elapsed)
    rx = f.put_right(38, 5, right, p.mid)
    icon_color = mix(p.lit, BG, wave(t, 3.0, 0.45)) if state == "play" else p.mid
    f.put(1, 5, "▶" if state == "play" else "‖", icon_color)
    text = " · ".join(x for x in (np.get("title"), np.get("artist")) if x).upper()
    f.put(3, 5, fit(text, rx - 1 - 3), p.lit if state == "play" else p.mid)


def draw_beliefs(f: Frame, s: State) -> None:
    p = f.pal
    x = f.put(1, 6, "BELIEFS ", p.mid)
    for label, key in (("CORTANA:", "cortana"), (" VECTOR:", "vector")):
        x = f.put(x, 6, label, p.mid)
        n = s.beliefs.get(key)
        x = f.put(x, 6, " --" if n is None else f" {n}", p.dim if n is None else p.lit, bold=True)
    f.put(33, 6, "│", p.ghost)
    if s.a2a:
        f.put(34, 6, " A2A ", BG, p.lit, bold=True)
    else:
        f.put(34, 6, " A2A ", p.dim, bold=True)


TILES = [("RECEPTION", 0, 7), ("RECEPTION TEMP", 20, 7), ("LIVING ROOM", 0, 10), ("HEAT BOOST", 20, 10)]
BOOST_TEMP = 25.0


def tile_at(x: int, y: int) -> int | None:
    for i, (_, x0, y0) in enumerate(TILES):
        if x0 <= x < x0 + 20 and y0 <= y < y0 + 3:
            return i
    return None


def _tile(f: Frame, i: int, segs: list[tuple[str, Rgb]], border: Rgb, title_color: Rgb) -> None:
    title, x0, y0 = TILES[i]
    f.put(x0, y0, "╭" + "─" * 18 + "╮", border)
    f.put(x0 + 2, y0, f" {title} ", title_color)
    f.put(x0, y0 + 1, "│", border)
    f.put(x0 + 19, y0 + 1, "│", border)
    f.put(x0, y0 + 2, "╰" + "─" * 18 + "╯", border)
    x = x0 + 2
    for text, fg in segs:
        x = f.put(x, y0 + 1, text, fg)


def draw_tiles(f: Frame, s: State, wall: float, t: float) -> None:
    p = f.pal

    def frame_colors(i: int, active: bool) -> tuple[Rgb, Rgb]:
        if s.flash.get(i, 0) > t:                        # the tap failed: blink the box
            return (p.lit if int(t / 0.25) % 2 else p.ghost), p.lit
        return (p.mid if active else p.dim), (p.lit if active else p.mid)

    if not s.ha_ok:
        for i in range(4):
            _tile(f, i, [("-- OFFLINE --", p.dim)], p.ghost, p.dim)
        return

    for i, key in ((0, "reception"), (2, "living")):
        light = s.lights.get(key)
        if light is None:
            segs, on = [("--", p.dim)], False
        elif light.get("on"):
            pct = light.get("pct")
            segs, on = [("━━━●", p.lit), ("  ON", p.lit), (f" {pct}%" if pct is not None else "", p.mid)], True
        else:
            segs, on = [("●", p.mid), ("━━━", p.ghost), ("  OFF", p.mid)], False
        _tile(f, i, segs, *frame_colors(i, on))

    c = s.climate
    boosting = s.boost_until is not None and s.boost_until > wall
    if c is None:
        _tile(f, 1, [("--", p.dim)], *frame_colors(1, False))
    else:
        cur, sp = c.get("current"), c.get("setpoint")
        heat = mix(p.lit, BG, wave(t, 2.0, 0.18)) if c.get("action") == "heating" else p.ghost
        segs = [(f"{cur:.1f}°" if cur is not None else "--.-°", p.lit), (" SET ", p.mid),
                (f"{sp:.1f}°" if sp is not None else "OFF", p.lit), (" ≋", heat)]
        _tile(f, 1, segs, *frame_colors(1, True))

    if boosting:
        left = _mmss(s.boost_until - wall).rjust(5)
        glow = mix(p.lit, BG, wave(t, 3.0, 0.45))
        border, title = frame_colors(3, True)
        if s.flash.get(3, 0) <= t:
            border = glow
        _tile(f, 3, [(f"▲ {BOOST_TEMP:.0f}°", glow), ("  " + left, p.lit), ("  END", p.mid)], border, title)
    else:
        _tile(f, 3, [("▲ ", p.mid), (f"{BOOST_TEMP:.0f}° FOR 1 HOUR", p.mid)], *frame_colors(3, False))


GROW_S, PULSE_S = 0.5, 1.6


def pretty_tool(name: str) -> str:
    """home_intent__HassTurnOn -> HassTurnOn; ask_vector stays."""
    return name.rsplit("__", 1)[-1] if "__" in name else name


def draw_bar(f: Frame, s: State, t: float) -> None:
    p = f.pal
    y = H - 1
    age = t - s.agent_since
    a = s.agent
    fill = [p.ghost] * W
    label, sub, text_fg = "CORTANA", "", p.dim
    if a == "listening":
        grow = min(1.0, age / GROW_S)
        half = grow * W / 2
        level = mix(p.lit, p.ghost, wave(age - GROW_S, PULSE_S, 0.28)) if age >= GROW_S else p.lit
        fill = [level if abs(x + 0.5 - W / 2) <= half else p.ghost for x in range(W)]
        label, text_fg = ("LISTENING" if age >= GROW_S else ""), BG
    elif a in ("thinking", "tool", "skill"):
        fill = [p.lit] * W
        text_fg = BG
        if a == "thinking":
            label = "THINKING"
            pos = int(((age % 1.8) / 1.8) * (W + 4)) - 4      # a darker band sweeps across
            for x in range(pos, pos + 4):
                if 0 <= x < W:
                    fill[x] = p.mid
        else:
            label = "TOOL CALL:" if a == "tool" else "SKILL CALL:"
            sub = pretty_tool(s.agent_detail) if a == "tool" else s.agent_detail
    for x in range(W):
        f.put(x, y, " ", text_fg, fill[x])
    line = f"{label}  {sub}" if sub else label
    line = fit(line, W - 2)
    x0 = (W - len(line)) // 2
    for i, ch in enumerate(line):
        x = x0 + i
        bold = i < len(label)
        f.put(x, y, ch, text_fg, fill[x], bold=bold)


def render(s: State, wall: float, t: float, pal: Palette | None = None) -> Frame:
    import time as _time
    f = Frame(pal or Palette.of())
    lt = _time.localtime(wall)
    draw_status(f, s)
    draw_clock(f, _time.strftime("%H%M", lt), 1, 1, t, dark=False)
    f.put(22, 1, f"{DAYS[lt.tm_wday]} {lt.tm_mday} {MONTHS[lt.tm_mon - 1]} {lt.tm_year}", f.pal.lit)
    draw_weather(f, s.weather, 22, 2, t)
    draw_playing(f, s, wall, t)
    draw_beliefs(f, s)
    draw_tiles(f, s, wall, t)
    draw_bar(f, s, t)
    return f
