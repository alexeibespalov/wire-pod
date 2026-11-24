---
name: timers
description: Longer-running help with timers and reminders - several timers at once while cooking, a reminder for every morning this week, "remind Leo at four to practise piano", bin day.
tools: set_timer, set_reminder, list_timers, cancel_timer
---
# Timers and reminders

`set_timer`, `set_reminder`, `list_timers` and `cancel_timer` are always available; this
skill is the how-to for anything beyond a single timer.

## Timers
- Give each timer a short label when there's more than one ("pasta", "garlic bread"), so
  "how long on the pasta?" and "cancel the bread" work.
- Minutes can be fractions: 0.5 is thirty seconds, 90 is an hour and a half.
- Confirm briefly: "Pasta, ten minutes. Bread, six." Not the end times unless asked.

## Reminders
- `set_reminder(when, text)`: `when` is local time, "HH:MM" (24h, the next time it's that
  o'clock) or "YYYY-MM-DD HH:MM". Work the date out from RIGHT NOW ("tomorrow at eight"
  -> tomorrow's date, "08:00").
- Write `text` as what you'll SAY, to the person: "Leo, time to practise piano." If it's
  for someone specific and you don't know who's asking, ask.
- Repeating ("every morning this week"): set one reminder per day, up to seven, and say
  how many you set. Nothing repeats forever.
- Vague times: "in the evening" -> ask, or pick a sensible time and say which.

## Rules
- Up to 20 timers and reminders at once; timers up to 24 hours, reminders up to 60 days.
- Never set a reminder that would be scary or unkind, or that pretends to be someone else.
