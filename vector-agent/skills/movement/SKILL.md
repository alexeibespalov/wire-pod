---
name: movement
description: Move your own body in measured, safe ways ("wiggle", "nod", "do a little dance", "nudge forward until something is close", "turn around", "come closer", "what's in front of you?") and save the moves you make up as named maneuvers. Also reading your own body sensors. Use instead of the coarse drive/turn tools whenever the move is more than one simple step.
---
# Moving your body: move, sense, maneuvers

You move by giving a list of steps. Everything is measured: after a run you are told how
far you really went, how much you really turned and what your distance sensor reads, so you
can judge it and adjust. You always stop by yourself if you are picked up, near a cliff, on
your charger (wheels only), bumped, or too far from where you started. Never try to
override that.

Moving your body -> these tools. Doing something LATER or REPEATEDLY over time (every
morning, in 10 minutes) -> the routine/schedule tools (other skill), not this one.

## Tools
- `move(steps)`: run a step list now.
- `sense(what)`: read your sensors, nothing moves. what = body (position, heading, head,
  lift), distance (front sensor), flags (charger, picked up, cliff, touched), cube, all.
- `save_maneuver(name, description, steps, params, tags)`: keep a step list under a name.
- `run_maneuver(name, values)`: run a saved one. `values` sets its params, e.g. {"mm": 80}.
- `list_maneuvers(query)`, `show_maneuver(name)`, `delete_maneuver(name)`.
- `record_attempt(name, outcome, note)`: say how a run went (success / partial / failure).
- `tune(...)`: search one number automatically (see the body-learning skill).

## Steps and units
Each step is an object with a "step" key:
- `{"step":"drive","mm":50,"speed":60}`: mm + is forward, - is backward. Max 300 mm per step.
  speed 10-120 mm/s (add "fast":true for up to 220; only on a clear, flat desk).
- `{"step":"turn","deg":90}`: + is left, - is right. A full turn is 360.
- `{"step":"head","deg":20}`: -22 (down) to 45 (up).
- `{"step":"lift","height":0.5}`: 0 (down) to 1 (up).
- `{"step":"wheels","left":40,"right":-40,"ms":600}`: each wheel mm/s for ms (max 3000).
  Opposite signs spin on the spot; different speeds make a curve.
- `{"step":"wait","ms":300}`: pause (max 5000).
- `{"step":"maneuver","name":"nod"}`: run a saved maneuver as one step (add "args").
- `{"step":"repeat","times":3,"steps":[...]}`: repeat steps (max 20 times).
- `{"step":"if","sensor":{"prox_below_mm":80},"then":[...],"else":[...]}`: branch on a sensor
  (prox_below_mm, touch, or cube_visible).
- A drive may stop early: `"stop_when":{"prox_below_mm":40}` (or touch, cube_moved).

Limits: a whole run is at most 60 seconds and 200 expanded steps. If you ask for more than a
limit allows it is trimmed and the answer tells you ("capped"). Keep moves small.

## Examples
Nod:
`[{"step":"head","deg":25},{"step":"head","deg":-5},{"step":"head","deg":25},{"step":"head","deg":10}]`

Wiggle:
`[{"step":"repeat","times":3,"steps":[{"step":"turn","deg":15},{"step":"turn","deg":-30},{"step":"turn","deg":15}]}]`

A little dance (head, lift, and a spin):
`[{"step":"lift","height":1},{"step":"repeat","times":2,"steps":[{"step":"turn","deg":40},{"step":"head","deg":30},{"step":"turn","deg":-40},{"step":"head","deg":0}]},{"step":"turn","deg":360},{"step":"lift","height":0}]`

Nudge forward until something is 4 cm away (never more than 15 cm, in case nothing is there):
`[{"step":"drive","mm":150,"speed":40,"stop_when":{"prox_below_mm":40}}]`
The result says what you reached: "distance ahead 180->38mm" means you stopped near it.

## Saving, params and recursion
- Save a move you like, named for its intent: `save_maneuver("nod", "nod yes twice", [...])`.
  New ones are "draft" until a run goes well and you call record_attempt with "success".
- Make numbers tunable with params: steps use `"$mm"`, params declare it:
  `params={"mm":{"default":50,"min":10,"max":200}}`, steps `[{"step":"drive","mm":"$mm"}]`.
  Values you pass are clamped to min/max.
- A maneuver may call others: `{"step":"maneuver","name":"wiggle"}`. At most 4 levels deep,
  never in a loop (it is rejected when you save).
- Re-saving a name makes a new version (the last 5 are kept).
- "MANEUVERS YOU KNOW" in your instructions lists the saved ones, best first; prefer a
  tested maneuver over inventing a new one.

## After a move
- Use the measured numbers, not your guess: "I went about 8 cm", not "I went 10 cm".
- If it stopped with "stopped: ...", say so plainly and briefly ("I stopped, you picked me
  up"). Don't retry on your own after being picked up or at a cliff.
- "I'm on my charger" means wheels are refused; head and lift still work.
- Keep your spoken reply short; the move is the point.
