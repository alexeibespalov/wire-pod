"""The dashboard without the hardware: the drawing, the parsers, the boost and the taps.

Run:  cd deploy/pizero/dashboard && python -m pytest tests -q
"""
import asyncio
import json
import time

import pytest

from cortana_dash import render as R
from cortana_dash import sources as S
from cortana_dash.boost import Boost

WALL = time.mktime((2026, 11, 25, 10, 47, 0, 0, 0, -1))     # Wed 25 Nov 2026, 10:47


def full_state() -> R.State:
    s = R.State(wifi_ssid="homenet", wifi_quality=77, bt_name="Echo Show 8-6KX", bt_connected=True,
                weather={"condition": "cloudy", "temperature": 4.7, "humidity": 84},
                playing={"state": "play", "title": "Clair de Lune", "artist": "Debussy", "elapsed": 161, "at": WALL},
                beliefs={"cortana": 87, "vector": 214}, ha_ok=True,
                lights={"reception": {"on": True, "pct": 30}, "living": {"on": False, "pct": None}},
                climate={"current": 19.5, "setpoint": 21.5, "action": "heating", "mode": "heat"})
    return s


# --------------------------------------------------------------------------- drawing
def test_everything_fits_the_40_by_15_grid():
    f = R.render(full_state(), WALL, 0.0)
    rows = f.text().split("\n")
    assert len(rows) == 15 and all(len(r) == 40 for r in rows)
    assert "▂▄▆█ HOMENET" in rows[0] and rows[0].rstrip().endswith("BT ECHO SHOW 8-6KX")
    assert rows[1].endswith("WED 25 NOV 2026 ")              # right-justified, away from the clock
    assert " 4.7°C" in rows[2] and "CLOUDY" in rows[3] and "RH 84%" in rows[4]
    assert "▶ CLAIR DE LUNE · DEBUSSY" in rows[5] and rows[5].rstrip().endswith("2:41")
    assert rows[6].startswith(" BELIEFS CORTANA: 87 VECTOR: 214") and rows[6].endswith("│ A2A  ")
    assert "RECEPTION" in rows[7] and "RECEPTION TEMP" in rows[7]
    assert "━━━●  ON 30%" in rows[8] and "19.5° SET 21.5° ≋" in rows[8]
    assert "●━━━  OFF" in rows[11] and "▲ 25° FOR 1 HOUR" in rows[11]
    assert rows[13].strip() == "CORTANA"
    assert rows[14].strip() == ""                              # nobody speaking


def test_offline_goes_dim_not_blank():
    f = R.render(R.State(), WALL, 0.0)
    rows = f.text().split("\n")
    assert "NO WI-FI" in rows[0] and "NO SPEAKER" in rows[0]
    assert "--.-°C" in rows[2] and "NO DATA" in rows[3]
    assert "NOTHING PLAYING" in rows[5] and "CORTANA: -- VECTOR: --" in rows[6]
    assert rows[8].count("-- OFFLINE --") == 2


def test_clock_digits_and_ghost_segments():
    pix = R.clock_pixels("1047")
    assert pix[0][1] == R.GHOST and pix[1][3] == R.LIT          # "1": no top bar, right side lit
    assert pix[3][6] == R.GHOST and pix[0][6] == R.LIT          # "0": no middle bar, top lit
    assert pix[2][10] == pix[4][10] == R.COLON


def test_a2a_goes_solid_while_the_agents_talk():
    s = full_state()
    s.a2a = True
    f = R.render(s, WALL, 0.0)
    ch, fg, bg, bold = f.cells[6][35]
    assert ch == "A" and bg == f.pal.lit and fg == R.BG


def test_listening_bar_grows_out_from_the_centre_then_pulses():
    s = full_state()
    S.set_agent(s, "listening", now=100.0)
    f = R.render(s, WALL, 100.1)                    # a fifth of the way out
    bg = [c[2] for c in f.cells[13]]
    assert bg[20] == f.pal.lit and bg[0] == f.pal.ghost and bg[39] == f.pal.ghost
    f = R.render(s, WALL, 100.5 + 0.8)              # grown, at the dim point of the pulse
    bg = [c[2] for c in f.cells[13]]
    assert bg[0] == bg[39] == bg[20] != f.pal.ghost and bg[20] != f.pal.lit
    assert "LISTENING" in f.row_text(13)


def test_bar_shows_tool_and_skill_calls():
    s = full_state()
    S.on_message(s, "tool home_intent__HassTurnOn", now=5)
    assert R.render(s, WALL, 5).row_text(13).strip() == "TOOL CALL:  HassTurnOn"
    S.on_message(s, "skill music", now=6)
    assert R.render(s, WALL, 6).row_text(13).strip() == "SKILL CALL:  music"
    S.on_message(s, "think", now=7)
    assert "THINKING" in R.render(s, WALL, 7).row_text(13)


def test_boost_tile_counts_down():
    s = full_state()
    s.boost_until = WALL + 47 * 60 + 12
    assert "▲ 25°  47:12  END" in R.render(s, WALL, 0).row_text(11)


def test_taps_land_on_the_right_tile():
    assert R.tile_at(5, 8) == 0 and R.tile_at(25, 7) == 1 and R.tile_at(0, 12) == 2 and R.tile_at(39, 10) == 3
    assert R.tile_at(5, 6) is None and R.tile_at(5, 13) is None


def test_her_words_scroll_in_from_the_right_and_queue_up():
    s = full_state()
    S.on_message(s, "say It's 5 degrees and cloudy.", now=100.0)
    assert R.render(s, WALL, 100.0).row_text(14).strip() == ""          # not in yet
    row = R.render(s, WALL, 101.0).row_text(14)                         # 10 characters in
    assert row.endswith("It's 5 deg") and row.startswith(" " * 30)
    S.on_message(s, "say Bring a coat.", now=102.0)                     # joins the queue
    assert s.speech == "It's 5 degrees and cloudy.   ·   Bring a coat."
    gone = 100.0 + (40 + len(s.speech)) / R.SPEECH_CPS
    assert "Bring a coat." in R.render(s, WALL, gone - 3.0).row_text(14)
    assert R.render(s, WALL, gone + 0.1).row_text(14).strip() == ""     # all gone past
    S.on_message(s, "say Anything else?", now=gone + 1)                 # a new run starts afresh
    assert s.speech == "Anything else?" and s.speech_since == gone + 1


# --------------------------------------------------------------------------- the activity
def test_led_and_channel_drive_the_bar():
    s = R.State()
    S.on_led(s, "off", "listen", now=1)
    assert s.agent == "listening"
    S.on_led(s, "listen", "think", now=2)
    assert s.agent == "thinking"
    S.on_message(s, "tool ask_vector", now=3)
    S.on_message(s, "a2a on")
    assert (s.agent, s.agent_detail, s.a2a) == ("tool", "ask_vector", True)
    S.on_led(s, "think", "off", now=4)                # TTS started: the agent's own state stands
    assert s.agent == "tool"
    S.on_message(s, "a2a off")
    S.on_message(s, "done", now=5)
    assert (s.agent, s.a2a) == ("idle", False)
    S.on_message(s, "say Hello there")                # for the Echo's screen, not ours
    assert s.agent == "idle"
    S.on_led(s, "off", "listen", now=10)
    S.on_led(s, "listen", "off", now=12)              # woke, heard nothing
    assert s.agent == "idle"
    S.on_message(s, "think", now=20)
    S.expire(s, now=20 + S.STALE_S + 1)               # a lost "done"
    assert s.agent == "idle"


# --------------------------------------------------------------------------- parsers
def test_parsers():
    assert S.wifi_quality("Inter-| sta-|\n face | tus\n wlan0: 0000   70.  -36.  -256  0 0 0 1 0 0\n") == 100
    assert S.wifi_quality(" wlan0: 0000   54.  -56.  -256\n") == 77
    assert S.wifi_quality("") is None
    assert S.active_ssid("no:Other\nyes:homenet\n") == "homenet"
    assert S.bt_view('b true\ns "Echo Show 8-6KX"\n') == (True, "Echo Show 8-6KX")
    assert S.parse_agents("cortana=http://a:8099/dash, vector=http://a:8098/dash") == {
        "cortana": "http://a:8099/dash", "vector": "http://a:8098/dash"}
    v = S.song_view({"state": "play", "elapsed": "12.5"}, {"file": "Daft Punk/Discovery/03 Digital Love.flac"}, 1.0)
    assert (v["title"], v["artist"], v["elapsed"]) == ("Digital Love", "Daft Punk", 12.5)
    v = S.song_view({"state": "play"}, {"file": "http://192.168.1.39:8097/yt/stream/x", "Title": "Song"}, 1.0)
    assert (v["title"], v["artist"]) == ("Song", "")
    assert S.light_view({"state": "on", "attributes": {"brightness": 76}}) == {"on": True, "pct": 30}
    assert S.light_view({"state": "unavailable"}) is None
    assert S.climate_view({"state": "heat", "attributes": {"current_temperature": 19.5, "temperature": 21.5,
                                                           "hvac_action": "heating"}})["setpoint"] == 21.5


# --------------------------------------------------------------------------- the boost
class FakeHA:
    def __init__(self, setpoint=21.5, mode="heat"):
        self.st = {"state": mode, "attributes": {"temperature": setpoint, "current_temperature": 19.5}}
        self.calls = []
        self.down = False

    async def state(self, entity):
        if self.down:
            raise ConnectionError("down")
        return json.loads(json.dumps(self.st))

    async def call(self, domain, service, data):
        if self.down:
            raise ConnectionError("down")
        self.calls.append((domain, service, data))
        if service == "set_temperature":
            self.st["attributes"]["temperature"] = data["temperature"]
            self.st["state"] = data.get("hvac_mode", self.st["state"])
        if service == "set_hvac_mode":
            self.st["state"] = data["hvac_mode"]


E = "climate.reception"


def test_boost_sets_25_and_puts_it_back_after_the_hour(tmp_path):
    now = [1000.0]
    ha = FakeHA()
    b = Boost(ha, E, tmp_path / "boost.json", clock=lambda: now[0])
    asyncio.run(b.toggle())
    assert ha.st["attributes"]["temperature"] == 25.0 and b.until == 1000.0 + 3600
    # a restart: the promise is in the file
    b = Boost(ha, E, tmp_path / "boost.json", clock=lambda: now[0])
    now[0] += 3599
    asyncio.run(b.tick())
    assert ha.st["attributes"]["temperature"] == 25.0
    now[0] += 2
    asyncio.run(b.tick())
    assert ha.st["attributes"]["temperature"] == 21.5 and b.until is None
    assert not (tmp_path / "boost.json").exists()


def test_tapping_again_ends_the_boost_early(tmp_path):
    ha = FakeHA()
    b = Boost(ha, E, tmp_path / "boost.json", clock=lambda: 0.0)
    asyncio.run(b.toggle())
    asyncio.run(b.toggle())
    assert ha.st["attributes"]["temperature"] == 21.5 and b.until is None


def test_a_setpoint_changed_by_hand_stands(tmp_path):
    now = [0.0]
    ha = FakeHA()
    b = Boost(ha, E, tmp_path / "boost.json", clock=lambda: now[0])
    asyncio.run(b.start())
    ha.st["attributes"]["temperature"] = 19.0           # someone turned it down meanwhile
    now[0] = 4000
    asyncio.run(b.tick())
    assert ha.st["attributes"]["temperature"] == 19.0 and b.until is None


def test_a_thermostat_that_was_off_goes_back_off(tmp_path):
    now = [0.0]
    ha = FakeHA(mode="off")
    b = Boost(ha, E, tmp_path / "boost.json", clock=lambda: now[0])
    asyncio.run(b.start())
    assert ha.st["state"] == "heat"
    now[0] = 4000
    asyncio.run(b.tick())
    assert ha.st["state"] == "off"


def test_ha_down_at_the_end_keeps_trying(tmp_path):
    now = [0.0]
    ha = FakeHA()
    b = Boost(ha, E, tmp_path / "boost.json", clock=lambda: now[0])
    asyncio.run(b.start())
    now[0] = 4000
    ha.down = True
    with pytest.raises(ConnectionError):
        asyncio.run(b.tick())
    assert b.until is not None                         # still owed
    ha.down = False
    asyncio.run(b.tick())
    assert ha.st["attributes"]["temperature"] == 21.5


# --------------------------------------------------------------------------- the app
def test_app_draws_and_taps_call_home_assistant(tmp_path):
    from cortana_dash.app import ENTITIES, Dashboard

    ha = FakeHA()
    boost = Boost(ha, ENTITIES["climate"], tmp_path / "boost.json")

    async def go():
        app = Dashboard(ha, boost, live=False)
        app.state = full_state()
        async with app.run_test(size=(40, 15)) as pilot:
            await pilot.pause()
            await pilot.click(offset=(5, 8))           # Reception lights (on at 30%)
            await pilot.click(offset=(5, 11))          # Living Room lights (off)
            await pilot.click(offset=(25, 8))          # the temperature only shows
            await pilot.click(offset=(25, 11))         # boost
            await pilot.pause()
            return app
    app = asyncio.run(go())
    assert ("light", "turn_off", {"entity_id": ENTITIES["reception"]}) in ha.calls
    assert app.state.lights["reception"]["on"] is False          # shown at once
    assert ("light", "turn_on", {"entity_id": ENTITIES["living"], "brightness_pct": 100}) in ha.calls
    assert app.state.lights["living"] == {"on": True, "pct": 100}  # full, not the last brightness
    assert any(c[1] == "set_temperature" and c[2]["temperature"] == 25.0 for c in ha.calls)
    assert len(ha.calls) == 3 and app.state.boost_until is not None


def test_polling_is_not_logged():
    import logging
    from cortana_dash.app import setup_logging
    setup_logging()
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    assert logging.getLogger("cortana-dash").isEnabledFor(logging.INFO)
