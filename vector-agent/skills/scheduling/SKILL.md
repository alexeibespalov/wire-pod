---
name: scheduling
description: Do something LATER, REPEATEDLY or OVER TIME in the background, and save reusable routines ("watch me play my game", "remind me at 7", "every morning say good morning", "keep an eye on the cat", "tell me when...", "stop watching"). Not for moving your body (that's maneuvers).
tools: save_routine, list_routines, delete_routine, schedule_action, look, play_animation
---
# Routines and scheduled actions

Two kinds of things you can set up:
- **schedule_action**: runs something in the background later, at a time, every day,
  or every N seconds for a while. It keeps going after your reply ends.
- **routines** (`save_routine`): a saved recipe of NON-physical steps that a schedule can
  run again and again: look with the camera, think about it, say something, show
  something, play music, or trigger a saved maneuver.

Moving your body right now = maneuver tools (movement skill). Doing something later,
repeatedly or over time = these tools.

## Choosing what to schedule
- One-off or simple ("remind Leo at 7 pm to brush his teeth"):
  `schedule_action(instruction="Remind Leo it's time to brush his teeth, in a fun way.", at_time="7:00 pm", title="teeth reminder")`
- Repeating with the camera ("watch me play my game", "keep an eye on the cake"): first
  save a routine (cheaper and steadier than an instruction), then schedule it:
  ```
  save_routine(name="watch-tv-game", description="Watch a video game on the TV and react",
    steps=[{"do":"tool","name":"look","args":{"question":"What is happening in the video game on the TV screen right now?","head":"up","listen_seconds":10}},
           {"do":"think","prompt":"Compare with earlier runs. Use what you saw AND what you overheard (TV sound, what people said). If something exciting or funny just happened, react in a few words like a friend watching along. Otherwise reply SILENT.","speak":true}])
  schedule_action(routine="watch-tv-game", every_seconds=15, for_minutes=30, hold_still=true, title="watching Leo play")
  ```
  `listen_seconds` makes a look also listen for that long right after the picture
  (the words are transcribed on the Pi; nothing is recorded), so you follow the sound
  too: what's said on the TV, and what people say about it. Use it when watching TV,
  videos or games together. It listens at most once a minute (every 4th look at 15 s);
  the other looks just take the picture, so react to sound only when the look result
  has "(overheard while looking: ...)". Repeat every 15 seconds or more.
- Daily things: `daily_at="8:00 am"`.
- Quiet note-taking (no talking): `speak=false`. People can ask you about it later.

## Before you start watching something
- Face the thing first (turn), and aim the camera in the routine's look step with
  `"args": {"question": "...", "head": "up"}` (TV / people) or `"head": "down"` (desk).
  Then `hold_still=true` so you don't wander off between looks. If you turn toward
  someone who says "Hey Vector" while you're watching, you turn back by yourself at the
  next look.
- Tell them briefly what you'll do and for how long ("Ooh, I'll watch for half an hour!").

## While it runs
- Your instructions show "WHAT YOU'RE DOING RIGHT NOW" with the latest results. Use them
  to answer "what's happening?", "who's winning?", "did you see that?".
- "Stop watching", "that's enough", "cancel the reminder": `cancel_schedule` (by id or a
  word from its title).
- "RECENTLY STOPPED" in your instructions means it isn't running any more (and why).
  Don't say you've been watching all along if it stopped.
- `list_schedules` shows everything that's scheduled.

## Limits
- At most 5 things scheduled at once; repeats at most every 5 seconds; a repeating action
  lasts 30 minutes unless asked (3 hours at most). Say so if someone asks for more.
- Camera looks are budgeted per hour; for long watching use 10–15 seconds between looks.
- You never talk over someone who's talking to you; your remarks wait or are skipped.
- Routine steps can only use: look, the face-screen tools, play_animation, set_eye_color,
  battery_status, where_am_i, exploration_findings, music tools and saved maneuvers.

## When it works well
If someone says you've learned something ("now you know how to watch video games!"),
load the 'skill-making' skill and save it as a skill of your own.
