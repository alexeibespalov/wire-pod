"""The Reception Room heat boost: 25 °C for an hour, then back to what it was.

The thermostat (Tado, through Home Assistant) has no timed boost of its own here, so the
dashboard keeps the promise: the setpoint and mode from before go to a file, and when the
hour is up (checked every few seconds, and again at start after a restart or reboot) they
are put back. Only if the boost is still in place: if someone changed the setpoint in the
meantime, theirs stands. Tapping the tile again ends the boost early.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("cortana-dash")

BOOST_TEMP = 25.0
BOOST_S = 3600


class Boost:
    def __init__(self, ha, entity: str, path: Path, clock: Callable[[], float] = time.time,
                 temp: float = BOOST_TEMP, seconds: float = BOOST_S):
        self.ha, self.entity, self.path, self.clock = ha, entity, Path(path), clock
        self.temp, self.seconds = temp, seconds
        self.data: dict | None = self._load()

    def _load(self) -> dict | None:
        try:
            d = json.loads(self.path.read_text())
            return d if d.get("entity") == self.entity and "until" in d else None
        except (OSError, ValueError):
            return None

    def _save(self) -> None:
        if self.data is None:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)

    @property
    def until(self) -> float | None:
        return self.data["until"] if self.data else None

    async def start(self) -> None:
        st = await self.ha.state(self.entity)
        prev = st.get("attributes", {}).get("temperature")
        mode = st.get("state")
        # remember first: a crash after the call still brings the setpoint back
        self.data = {"entity": self.entity, "prev": prev, "prev_mode": mode, "until": self.clock() + self.seconds}
        self._save()
        await self.ha.call("climate", "set_temperature",
                           {"entity_id": self.entity, "temperature": self.temp, "hvac_mode": "heat"})
        log.info("boost: %s -> %.1f for %ds (was %s, %s)", self.entity, self.temp, self.seconds, prev, mode)

    async def end(self) -> None:
        """Put back what was there, if the boost is still what's set. Raises if HA can't be
        reached, leaving the file so the next tick tries again."""
        if not self.data:
            return
        st = await self.ha.state(self.entity)
        now_sp = st.get("attributes", {}).get("temperature")
        still_boosted = now_sp is not None and abs(float(now_sp) - self.temp) < 0.05
        prev, mode = self.data.get("prev"), self.data.get("prev_mode")
        if still_boosted:
            if prev is not None:
                await self.ha.call("climate", "set_temperature", {"entity_id": self.entity, "temperature": prev})
            if mode == "off":
                await self.ha.call("climate", "set_hvac_mode", {"entity_id": self.entity, "hvac_mode": "off"})
            log.info("boost over: %s back to %s (%s)", self.entity, prev, mode)
        else:
            log.info("boost over: %s was changed to %s meanwhile, leaving it", self.entity, now_sp)
        self.data = None
        self._save()

    async def toggle(self) -> None:
        if self.data and self.clock() < self.data["until"]:
            await self.end()
            return
        if self.data:              # over but not put back yet: do that first, or 25 would be "before"
            await self.end()
        await self.start()

    async def tick(self) -> None:
        if self.data and self.clock() >= self.data["until"]:
            await self.end()
