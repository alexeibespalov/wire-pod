"""The Textual app: one widget the size of the screen, redrawn 8 times a second from the
frame render() makes, and taps on the tiles turned into Home Assistant calls."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.events import Click
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget

from . import sources
from .boost import Boost
from .render import BG, H, W, Frame, Palette, State, render, tile_at

log = logging.getLogger("cortana-dash")

FPS = 12
FLASH_S = 2.0
ENTITIES = {
    "reception": os.environ.get("DASH_RECEPTION_LIGHTS", "light.reception_room"),
    "living": os.environ.get("DASH_LIVING_LIGHTS", "light.living_room"),
    "climate": os.environ.get("DASH_RECEPTION_CLIMATE", "climate.tado_smart_radiator_thermostat_va3789108224"),
    "weather": os.environ.get("DASH_WEATHER", "weather.forecast_home"),
}


_styles: dict = {}


def _style(fg, bg, bold) -> Style:
    key = (fg, bg, bold)
    if key not in _styles:
        _styles[key] = Style(color=f"rgb({fg[0]},{fg[1]},{fg[2]})",
                             bgcolor=f"rgb({bg[0]},{bg[1]},{bg[2]})", bold=bold)
    return _styles[key]


def row_segments(row) -> list[Segment]:
    """One row of the frame as segments, one per run of the same colours."""
    out, run, key = [], "", None
    for ch, fg, bg, bold in row:
        k = (fg, bg, bold)
        if k != key and run:
            out.append(Segment(run, _style(*key)))
            run = ""
        key = k
        run += ch
    if run:
        out.append(Segment(run, _style(*key)))
    return out


def to_text(f: Frame) -> Text:
    """The frame as Rich text (for tests and screenshots)."""
    out = Text(no_wrap=True, overflow="crop", end="")
    for y in range(H):
        for seg in row_segments(f.cells[y]):
            out.append(seg.text, seg.style)
        if y < H - 1:
            out.append("\n")
    return out


class Board(Widget):
    """The whole screen. Each tick draws a frame and repaints only the rows that changed:
    a full repaint 8 times a second cost half a core on the Zero."""
    DEFAULT_CSS = f"Board {{ width: {W}; height: {H}; }}"

    def __init__(self):
        super().__init__()
        self.rows: list = [None] * H
        self.strips: list[Strip] = [Strip.blank(W) for _ in range(H)]

    def tick(self) -> None:
        app: Dashboard = self.app  # type: ignore[assignment]
        f = render(app.state, time.time(), time.monotonic(), app.palette)
        changed = []
        for y in range(H):
            if f.cells[y] != self.rows[y]:
                self.rows[y] = f.cells[y]
                self.strips[y] = Strip(row_segments(f.cells[y]), W)
                changed.append(Region(0, y, W, 1))
        if changed:
            self.refresh(*changed)

    def render_line(self, y: int) -> Strip:
        return self.strips[y] if 0 <= y < H else Strip.blank(self.size.width)

    async def on_click(self, event: Click) -> None:
        await self.app.tap(event.x, event.y)  # type: ignore[attr-defined]


class Dashboard(App):
    CSS = f"Screen {{ background: rgb{BG}; overflow: hidden hidden; }}"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, ha: sources.HA | None = None, boost: Boost | None = None,
                 agents: dict[str, str] | None = None, speaker_mac: str = "",
                 mpd: tuple[str, int] | None = ("127.0.0.1", 6600), live: bool = True):
        super().__init__()
        self.state = State()
        self.palette = Palette.of(os.environ.get("DASH_COLOR", "#6dffd2"))
        self.ha, self.boost = ha, boost
        self.agents, self.speaker_mac, self.mpd, self.live = agents or {}, speaker_mac, mpd, live
        self.board = Board()
        self._busy: set[int] = set()
        if boost is not None:
            self.state.boost_until = boost.until

    def compose(self) -> ComposeResult:
        yield self.board

    def on_mount(self) -> None:
        self.board.tick()
        self.set_interval(1 / FPS, self.board.tick)
        if not self.live:
            return
        s = self.state
        if self.ha is not None:
            self.run_worker(sources.poll_ha(s, self.ha, ENTITIES, self.boost), exclusive=False)
        if self.mpd:
            self.run_worker(sources.watch_mpd(s, *self.mpd))
        self.run_worker(sources.watch_led(s))
        self.run_worker(sources.poll_links(s, self.speaker_mac))
        if self.agents:
            self.run_worker(sources.poll_agents(s, self.agents))

    async def tap(self, x: int, y: int) -> None:
        i = tile_at(x, y)
        if i is None or i == 1 or self.ha is None or not self.state.ha_ok or i in self._busy:
            return                     # the temperature tile only shows; one call per tile at a time
        self._busy.add(i)
        s = self.state
        try:
            if i in (0, 2):
                key = "reception" if i == 0 else "living"
                light = s.lights.get(key)
                if light is not None and light["on"]:
                    s.lights[key] = {**light, "on": False}  # show it at once; the next poll confirms
                    await self.ha.call("light", "turn_off", {"entity_id": ENTITIES[key]})
                else:                                       # on means full brightness, not the last one
                    s.lights[key] = {"on": True, "pct": 100}
                    await self.ha.call("light", "turn_on", {"entity_id": ENTITIES[key], "brightness_pct": 100})
            elif i == 3 and self.boost is not None:
                await self.boost.toggle()
                s.boost_until = self.boost.until
                c = s.climate
                if c is not None:
                    s.climate = {**c, "setpoint": 25.0 if s.boost_until else c.get("setpoint")}
        except Exception as e:
            log.warning("tap on tile %d failed: %s", i, e)
            s.flash[i] = time.monotonic() + FLASH_S
        finally:
            self._busy.discard(i)


def setup_logging() -> None:
    import logging.handlers
    # never print: anything on stdout/stderr would draw over the screen
    try:
        handler: logging.Handler = logging.handlers.SysLogHandler(address="/dev/log")
        handler.setFormatter(logging.Formatter("cortana-dash: %(message)s"))
    except OSError:
        handler = logging.NullHandler()
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    log.setLevel(logging.INFO)
    # httpx logs every request at INFO: ~2,400 lines an hour of polling onto the Zero's SD card
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()

    url, token = os.environ.get("HA_URL", ""), os.environ.get("HA_TOKEN", "")
    ha = sources.HA(url, token) if url and token else None
    state_dir = Path(os.environ.get("DASH_STATE_DIR", "/var/lib/cortana-dash"))
    boost = Boost(ha, ENTITIES["climate"], state_dir / "boost.json") if ha else None
    agents = sources.parse_agents(os.environ.get("DASH_AGENTS", ""))
    Dashboard(ha, boost, agents, os.environ.get("SPEAKER_MAC", "")).run(mouse=True)
