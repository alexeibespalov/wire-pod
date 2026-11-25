"""Where the dashboard's numbers come from. Each source fails on its own: what it feeds
goes dim ("--") and the rest carries on.

    Home Assistant  REST on the Pi 5: the two light groups, the thermostat, the weather
    MPD             on this Zero: what's playing, and the `cortana` channel, where
                    cortana-agent sends think / tool <name> / skill <name> / done / a2a on|off
    cortana-led     /run/cortana-led/state, written by the satellite's hooks: listen / think / off
    Wi-Fi           /proc/net/wireless (signal) and nmcli (network name)
    Bluetooth       BlueZ over busctl: is the speaker connected, and its name
    the agents      GET /dash on each (current belief count)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Callable

import httpx

from .render import State

log = logging.getLogger("cortana-dash")


# --------------------------------------------------------------------------- Home Assistant
class HA:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.c = httpx.AsyncClient(base_url=url.rstrip("/") + "/api", timeout=8, transport=transport,
                                   headers={"Authorization": f"Bearer {token}"})

    async def state(self, entity: str) -> dict:
        r = await self.c.get(f"/states/{entity}")
        r.raise_for_status()
        return r.json()

    async def call(self, domain: str, service: str, data: dict) -> None:
        r = await self.c.post(f"/services/{domain}/{service}", json=data)
        r.raise_for_status()


def light_view(st: dict) -> dict | None:
    if st.get("state") not in ("on", "off"):
        return None
    b = st.get("attributes", {}).get("brightness")
    return {"on": st["state"] == "on", "pct": round(b * 100 / 255) if b is not None else None}


def climate_view(st: dict) -> dict | None:
    if st.get("state") in (None, "unavailable", "unknown"):
        return None
    a = st.get("attributes", {})
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    return {"current": num(a.get("current_temperature")),
            "setpoint": num(a.get("temperature")) if st["state"] != "off" else None,
            "action": a.get("hvac_action"), "mode": st["state"]}


def weather_view(st: dict) -> dict | None:
    if st.get("state") in (None, "unavailable", "unknown"):
        return None
    a = st.get("attributes", {})
    return {"condition": st["state"], "temperature": a.get("temperature"), "humidity": a.get("humidity")}


async def poll_ha(s: State, ha: HA, entities: dict, boost=None, every: float = 5.0,
                  weather_every: float = 120.0) -> None:
    last_weather = 0.0
    while True:
        try:
            s.lights["reception"] = light_view(await ha.state(entities["reception"]))
            s.lights["living"] = light_view(await ha.state(entities["living"]))
            s.climate = climate_view(await ha.state(entities["climate"]))
            if time.monotonic() - last_weather > weather_every:
                s.weather = weather_view(await ha.state(entities["weather"]))
                last_weather = time.monotonic()
            s.ha_ok = True
            if boost is not None:
                await boost.tick()
                s.boost_until = boost.until
        except Exception as e:
            if s.ha_ok:
                log.warning("home assistant: %s", e)
            s.ha_ok = False
        await asyncio.sleep(every)


# --------------------------------------------------------------------------- Cortana's activity
LED_STATE = Path("/run/cortana-led/state")
STALE_S = 60.0          # a status nobody ends (a lost "done") clears itself


def set_agent(s: State, kind: str, detail: str = "", now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    if kind != s.agent or detail != s.agent_detail:
        s.agent, s.agent_detail, s.agent_since = kind, detail, now


def on_message(s: State, text: str, now: float | None = None) -> None:
    """A line from the `cortana` channel."""
    word, _, rest = text.strip().partition(" ")
    rest = rest.strip()
    if word == "think":
        set_agent(s, "thinking", now=now)
    elif word in ("tool", "skill"):
        set_agent(s, word, rest, now=now)
    elif word == "done":
        set_agent(s, "idle", now=now)
    elif word == "a2a":
        s.a2a = rest == "on"
    # say / busy / quiet are for the Echo's screen (cortana-nowplaying): nothing here


def on_led(s: State, before: str, after: str, now: float | None = None) -> None:
    """The satellite's LED state changed (cortana-led.sh)."""
    if after == "listen":
        set_agent(s, "listening", now=now)            # the mic opened: the bar grows out
    elif after == "think" and s.agent == "listening":
        set_agent(s, "thinking", now=now)             # speech sent for transcription
    elif after == "off" and s.agent == "listening":
        set_agent(s, "idle", now=now)                 # nothing was said


def expire(s: State, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    if s.agent != "idle" and now - s.agent_since > STALE_S:
        set_agent(s, "idle", now=now)


async def watch_led(s: State, path: Path = LED_STATE, every: float = 0.15) -> None:
    last = ""
    while True:
        try:
            cur = path.read_text().strip()
        except OSError:
            cur = ""
        if cur != last:
            on_led(s, last, cur)
            last = cur
        expire(s)
        await asyncio.sleep(every)


# --------------------------------------------------------------------------- MPD
def _pairs(lines: list[str]) -> list[tuple[str, str]]:
    out = []
    for line in lines:
        k, sep, v = line.partition(": ")
        if sep:
            out.append((k, v))
    return out


def song_view(status: dict, song: dict, now: float) -> dict:
    """What to show for MPD's status + currentsong (the same fallbacks as cortana-nowplaying:
    untagged files are <Artist>/<Album>/<track>)."""
    path = song.get("file", "")
    parts = path.split("/") if "://" not in path else []
    title = song.get("Title") or (re.sub(r"^\d{1,3}[\s._-]+", "", parts[-1].rsplit(".", 1)[0]) if parts else "")
    artist = song.get("Artist") or (parts[0] if len(parts) >= 2 else "")
    return {"state": status.get("state", "stop"), "title": title, "artist": artist,
            "elapsed": float(status.get("elapsed") or 0), "at": now}


async def watch_mpd(s: State, host: str = "127.0.0.1", port: int = 6600,
                    on_line: Callable[[State, str], None] = on_message) -> None:
    while True:
        try:
            r, w = await asyncio.open_connection(host, port)
            await r.readline()                                    # "OK MPD x.y.z"

            async def cmd(c: str) -> list[str]:
                w.write((c + "\n").encode())
                await w.drain()
                lines = []
                while True:
                    line = (await r.readline()).decode("utf-8", "replace")
                    if not line:
                        raise ConnectionError("mpd closed the connection")
                    line = line.rstrip("\n")
                    if line == "OK":
                        return lines
                    if line.startswith("ACK"):
                        raise RuntimeError(line)
                    lines.append(line)

            await cmd("subscribe cortana")
            while True:
                status = dict(_pairs(await cmd("status")))
                song = dict(_pairs(await cmd("currentsong")))
                s.playing = song_view(status, song, time.time())
                for k, v in _pairs(await cmd("readmessages")):
                    if k == "message":
                        on_line(s, v)
                await cmd("idle player message")
        except Exception as e:
            log.info("mpd: %s (retrying)", e)
            s.playing = None
            await asyncio.sleep(5)


# --------------------------------------------------------------------------- Wi-Fi, Bluetooth
def wifi_quality(text: str, iface: str = "wlan0") -> int | None:
    """Link quality in percent from /proc/net/wireless (quality is out of 70)."""
    for line in text.splitlines():
        if line.strip().startswith(iface + ":"):
            try:
                return min(100, round(float(line.split()[2].rstrip(".")) * 100 / 70))
            except (IndexError, ValueError):
                return None
    return None


async def _run(*argv: str, timeout: float = 5) -> str:
    p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(p.communicate(), timeout)
    return out.decode("utf-8", "replace")


def active_ssid(nmcli_out: str) -> str | None:
    for line in nmcli_out.splitlines():
        active, _, ssid = line.partition(":")
        if active == "yes" and ssid:
            return ssid.replace("\\:", ":")
    return None


def bt_view(busctl_out: str) -> tuple[bool | None, str | None]:
    """`busctl get-property ... Connected Alias` prints `b true` then `s "Echo Show 8-6KX"`."""
    connected, name = None, None
    for line in busctl_out.splitlines():
        kind, _, val = line.partition(" ")
        if kind == "b":
            connected = val.strip() == "true"
        elif kind == "s":
            name = val.strip().strip('"')
    return connected, name


async def poll_links(s: State, speaker_mac: str, every: float = 5.0) -> None:
    n = 0
    dev = "/org/bluez/hci0/dev_" + speaker_mac.replace(":", "_")
    while True:
        try:
            s.wifi_quality = wifi_quality(Path("/proc/net/wireless").read_text())
        except OSError:
            s.wifi_quality = None
        if n % 12 == 0:                                   # nmcli costs more: once a minute
            try:
                s.wifi_ssid = active_ssid(await _run("nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"))
            except Exception:
                s.wifi_ssid = None
        if speaker_mac and n % 2 == 0:
            try:
                s.bt_connected, name = bt_view(await _run("busctl", "get-property", "org.bluez", dev,
                                                          "org.bluez.Device1", "Connected", "Alias"))
                s.bt_name = name or s.bt_name
            except Exception:
                s.bt_connected = None
        n += 1
        await asyncio.sleep(every)


# --------------------------------------------------------------------------- the agents
async def poll_agents(s: State, urls: dict[str, str], every: float = 30.0,
                      transport: httpx.AsyncBaseTransport | None = None) -> None:
    async with httpx.AsyncClient(timeout=5, transport=transport) as c:
        while True:
            for name, url in urls.items():
                try:
                    s.beliefs[name] = (await c.get(url)).json().get("beliefs")
                except Exception:
                    s.beliefs[name] = None
            await asyncio.sleep(every)


def parse_agents(spec: str) -> dict[str, str]:
    """"cortana=http://...:8099/dash,vector=http://...:8098/dash" -> {name: url}"""
    out = {}
    for part in spec.split(","):
        name, sep, url = part.strip().partition("=")
        if sep and name and url:
            out[name.strip().lower()] = url.strip()
    return out
