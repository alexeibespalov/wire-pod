---
name: body-learning
description: Practise and improve a physical skill by yourself ("learn to push the cube", "get better at that", "practise nudging", "work out how far to roll to reach the edge of the desk", "make that move more reliable"). The try-measure-adjust-save loop, naming variants by intent, and when to auto-tune a number.
---
# Learning a movement by practising

Load the `movement` skill too: it has the step format and units. This skill is HOW to
practise. Be a careful, curious learner, not a daredevil: small moves, one change at a time.

## The practice loop
1. Look first. `look` at what is in front of you (or `sense("distance")`, `sense("cube")`)
   and say to yourself what you want to happen ("the cube moves about 5 cm").
2. Try a SMALL version. Short drive (30-60 mm), slow speed (40-60). Use `move` for a
   first try, or `run_maneuver` if you already have a draft.
3. Read the measured result (moved / turned / distance ahead / "cube moved"), and `look`
   again if the outcome is visual (did the cube go anywhere?). Compare with the goal.
4. Adjust ONE thing: a distance, a speed, an angle, or a step. Try again.
5. When a version works, `save_maneuver` it (with params for the numbers you tuned).
6. Run the saved version once, then `record_attempt(name, success|partial|failure, note)`
   with a note on what you'd change. Honest outcomes matter: partial = roughly did it.
7. Stop after about 5-8 tries in one session, or as soon as it works twice in a row.

## Names: by intent, not by try number
Name by what it is FOR, and keep different intents as different maneuvers:
`dribble_cube` (small taps that keep the cube near), `kick_cube` (one hard shove),
`nudge_forward`, `back_up_a_bit`. Don't save `try3`. If a new version is better, re-save the
SAME name (old versions are kept). If it is a different intent (soft vs hard), save a new
name. Check `list_maneuvers` first so you don't duplicate one.

## When to use `tune`
Use it when the SHAPE of the move is right and only one number is off, and you can measure
the result: "make nudge move exactly 10 cm" ->
`tune("nudge_forward","mm",20,200,8,"displacement_mm","100")`.
- measure: displacement_mm (how far), heading_deg (how much turned), prox_after_mm (distance
  ahead at the end), cube_moved (did the cube move at all).
- target: a number to reach, or "max" / "min".
- It really drives every trial (up to 20) and comes back to where it started each time, then
  saves the best value as the param's default. It costs no thinking per trial.
- Don't tune when the move is the wrong idea, when the result is noisy or only visual, or
  with a wide range. Keep low..high small and safe (e.g. 20..200 mm).
- After tuning, run the maneuver once and `record_attempt`.

## Staying safe (always)
- Practise on a flat, clear desk with your charger nearby and someone around. Never near an
  edge: if you are unsure where the edge is, drive less, not more. A cliff or pick-up stops
  you automatically; if that happens, stop practising and tell the person.
- Small first, then bigger. Never double a distance or speed in one go.
- No fast mode while learning.
- Don't practise while someone is talking to you, or at night.
- If three tries in a row fail, stop, say what you learned, and ask for help or a new idea.

## Practising on your own (background sessions)
For anything that needs more than a few tries ("practise kicking the ball", "learn to line
the blocks up", "get better at that"), don't do all the tries inside the conversation:
- `start_practice(goal, minutes, max_tries)` with a concrete goal ("push the red ball about
  10 cm and keep it in front of you (dribble)"). Then tell them in a few words that you'll
  practise quietly. You pause automatically whenever someone talks to you.
- `practice_status()` for "how's it going?"; `stop_practice()` for "stop practising".
- Afterwards your instructions show "YOU PRACTISED EARLIER" with what you learned: use the
  maneuver it names.
- While you sleep you review your attempts and may save improved DRAFT maneuvers; a draft
  becomes "tested" after a real successful run + record_attempt.
