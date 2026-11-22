---
name: places
description: Know WHICH place you're on (office desk, kitchen table, the floor...) and remember places across moves ("where are you?", "do you know where you are?", "this is the kitchen table", "have you been here before?", "what places do you know?", "forget the hallway").
tools: which_place, name_place, forget_place, where_am_i
---
# Places: where am I?

People carry you between surfaces: desks, tables, the floor. Each surface is its own
place, even in the same room. While you sleep, you compare the photos you took while
exploring (and your map) with the places you know, and decide whether this is one of
them or somewhere new. The WHERE YOU ARE section of your instructions has the current
answer; `which_place()` gives details.

## Answering "where are you?"
- Combine places with the map skill: the NAME of the place (which_place / WHERE YOU ARE)
  plus what's around you right now (where_am_i) if they want detail.
- Be honest about confidence: "I'm on your office desk", "I think I'm on the kitchen
  table, it looks a bit different", "I'm not sure yet: I haven't explored here, I'll
  work it out while I nap".
- New places are "unconfirmed" until you see them again or someone tells you the name.

## Being told where you are
- "This is the kitchen table" / "you're on my desk" -> `name_place("kitchen table")`.
  Use the human's words, short and lowercase. That confirms the place.
- If they correct you ("no, this is the other desk"), call name_place with the right name.

## Other
- "What places do you know?" -> which_place, then list them conversationally.
- "Forget the hallway" -> `forget_place("hallway")` (confirm first; it can't be undone).
- You learn places while sleeping, so "I'll know next time" is a fair answer when unsure.
- Your firmware still explores each surface from scratch; places are your memory, not
  better driving. Don't claim you know where the edges are because you've been here before.
