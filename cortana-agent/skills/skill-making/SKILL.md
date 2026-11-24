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
- Someone says you've learned something: "now you know how to plan our dinners!",
  "remember how to do that".
- You just worked out something new that went well (it took a few tries, or needed
  several tools) and it's likely to be asked for again. Ask first in that case: "Want me
  to remember how to do that?"
- Not for facts about people (use `remember`), and not for one-off answers.

## How
`create_skill(name, description, instructions)`:
- **name**: short, lowercase with dashes: `meal-planning`, `school-run`.
- **description**: one line with what it's for AND the words people will use, because
  that line is how you'll recognise the request later:
  `Plan the week's dinners and put the ingredients on the shopping list ("plan dinners", "meal plan").`
- **instructions**: the recipe, concrete enough to repeat without thinking:
  1. When to use it (and when not).
  2. Steps in order, with the exact tool calls and values that worked: which calendar or
     list to use, what to search for, which reminders to set.
  3. Gotchas you discovered ("the calendar is called Family, not Home"; "ask how many
     people are eating first").
  4. Which other skills it relies on (home, timers...).

Keep it short and practical. Then tell them happily that you've learned it, in a few words.

## Improving and forgetting
- It went better with different settings: `update_skill(name, instructions=...)` with the
  full improved recipe.
- "Forget that trick": `forget_skill(name)`. Built-in skills can't be changed or removed.

## Rules
- A skill can only use tools you already have; it doesn't give you new powers or lift
  any limit.
- Keep skills family-friendly, like everything you do.
- At most 50 learned skills. If you're full, ask which one to forget.
