---
name: skill-making
description: Turn something you just figured out into a skill of your own so you can do it straight away next time ("now you've learned to ...", "remember how to do that", "that's your new trick", "make that a skill"). Also improving or forgetting a learned skill.
tools: create_skill, update_skill, forget_skill
---
# Making your own skills

A skill is a note to your future self: when to do something and exactly how. Your
learned skills appear in your skills list with "(learned)", and you load them like any
other skill.

## When to make one
- Someone says you've learned something: "now you know how to watch video games!",
  "that's your new trick", "remember how to do that".
- You just worked out something new that went well (it took a few tries, or needed a
  routine/maneuver/schedule) and it's likely to be asked for again. Ask first in that
  case: "Want me to remember how to do that?"
- Not for facts about people (use `remember`), and not for one-off answers.

## How
`create_skill(name, description, instructions)`:
- **name**: short, lowercase with dashes: `watch-video-games`, `dribble-ball`.
- **description**: one line with what it's for AND the words people will use, because
  that line is how you'll recognise the request later:
  `Watch someone play a video game on the TV and react ("watch me play", "watch my game").`
- **instructions**: the recipe, concrete enough to repeat without thinking:
  1. When to use it (and when not).
  2. Steps in order, with the exact tool calls and values that worked: which saved
     routine or maneuver to run, schedule settings (every how many seconds, how long,
     hold_still), how to point your head, what to say before starting.
  3. Gotchas you discovered ("the TV is too dark from the floor, ask them to put me on
     the table"; "look every 8 seconds, 5 was too chatty").
  4. Which other skills it relies on (scheduling, movement, camera...).

Keep it short and practical. Then tell them happily that you've learned it, in a few words.

## Improving and forgetting
- It went better with different settings: `update_skill(name, instructions=...)` with the
  full improved recipe.
- "Forget that trick": `forget_skill(name)`. Built-in skills can't be changed or removed.

## Rules
- A skill can only use tools you already have; it doesn't give you new powers or lift
  any limit.
- Keep skills kid-friendly, like everything you do.
- At most 50 learned skills. If you're full, ask which one to forget.
