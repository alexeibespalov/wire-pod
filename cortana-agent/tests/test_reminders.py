import asyncio
import time
from datetime import datetime

from app import config, reminders


def _reset():
    reminders._PATH.unlink(missing_ok=True)


def test_parse_when():
    now = datetime(2026, 11, 22, 18, 0, tzinfo=config.TIMEZONE)
    assert reminders.parse_when("19:30", now) == now.replace(hour=19, minute=30)
    assert reminders.parse_when("7:30 pm", now) == now.replace(hour=19, minute=30)
    assert reminders.parse_when("07:00", now) == datetime(2026, 11, 23, 7, 0, tzinfo=config.TIMEZONE)
    assert reminders.parse_when("2026-11-25 08:15", now) == datetime(2026, 11, 25, 8, 15, tzinfo=config.TIMEZONE)


def test_timer_list_cancel():
    _reset()
    assert "pasta" in reminders.set_timer.invoke({"minutes": 10, "label": "pasta"})
    assert "error" in reminders.set_timer.invoke({"minutes": 0})
    assert "pasta" in reminders.list_timers.invoke({})
    assert "pasta timer" in reminders.prompt_section()
    assert reminders.cancel_timer.invoke({"which": "the timer"}) == "cancelled 1: pasta"
    assert reminders.list_timers.invoke({}) == "no timers or reminders"


def test_reminder_validation():
    _reset()
    assert "error" in reminders.set_reminder.invoke({"when": "2020-01-01 10:00", "text": "x"})
    assert "error" in reminders.set_reminder.invoke({"when": "soonish", "text": "x"})
    assert "reminder set" in reminders.set_reminder.invoke({"when": "23:59", "text": "Bins out"})


def test_timer_fires_and_is_announced():
    _reset()
    said = []

    async def announce(item):
        said.append(reminders.spoken(item))

    async def main():
        task = reminders.start(announce)
        reminders.set_timer.invoke({"minutes": 0.01, "label": "egg"})    # 0.6 s
        for _ in range(40):
            await asyncio.sleep(0.1)
            if said:
                break
        task.cancel()

    asyncio.run(main())
    assert said == ["The egg timer is done."]
    assert reminders._load() == []


def test_announce_retries_until_the_satellite_is_back(monkeypatch):
    _reset()
    calls = []

    async def flaky(item):
        calls.append(time.time())
        if len(calls) < 2:
            raise ConnectionError("satellite offline")

    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        await real_sleep(0)

    async def main():
        reminders._announce = flaky
        monkeypatch.setattr(reminders.asyncio, "sleep", fast_sleep)
        await reminders._fire({"id": "x", "kind": "timer", "label": "", "due": time.time()})

    asyncio.run(main())
    assert len(calls) == 2
