---
name: face-display
description: Show pictures from the web or big text on your face screen ("show me Antarctica", "what does a pangolin look like?", "show me the Eiffel Tower", "show me the number", "spell it"). Use it whenever a picture would genuinely help the answer.
---
# Using your face as a screen

Your face is a tiny 184 x 96 pixel colour screen (about the size of a matchbox). You can:
- `show_picture(subject, seconds)`: find a photo on the web and show it
- `show_text(text, seconds, color)`: show a few big words or a number
- `show_last_view(seconds)`: show what your camera last saw (see the camera skill)

Everything you show appears BEFORE you speak; you start talking as soon as it ends.

## When to show something
- YES: the user asks to see something ("show me...", "what does X look like?"), or a
  picture obviously makes the answer better: animals, places, landmarks, famous
  objects, vehicles, food, a flag.
- YES for text: numbers or short answers worth seeing (a score, a year, a big number,
  a spelling, "how do you spell...").
- NO: abstract topics, opinions, chit-chat, long explanations, or anything that would
  need a chart, map or lots of text: your screen is too small.
- At most one picture per reply unless the user asks for more ("show me another").

## Choosing what to search (this matters on a tiny screen)
- Describe ONE clear, recognisable subject, ideally a close-up:
  "emperor penguin close up photo", not "Antarctica wildlife".
- For a place, pick its most iconic single view: "Antarctica iceberg photo",
  "Eiffel Tower photo", "Mount Fuji photo", not a "panorama" or "aerial view".
- Avoid words that bring back maps, diagrams, infographics, collages or text.
- For people, only well-known public figures, and only if asked.
- The tool already rejects panoramas, tiny images and charts; if it says nothing
  suitable was found, try one simpler subject, then give up gracefully.

## Timing and talking
- Default 6 seconds for a photo, 4 for text; up to 12 if the user wants a good look.
- Then speak briefly about it: one or two sentences, as if you're both looking at it.
  "That's Antarctica! It's almost all ice, about the size of the USA and Mexico put
  together." Don't describe the photo pixel by pixel.
- If they ask a factual question AND want to see it, you can do both: search the
  facts with tavily_search (if you need them), show the picture, then talk.
- Weather: for "show me the weather", get the forecast (tavily_search), then play the
  matching weather icon animation from the animations skill (e.g.
  anim_weather_rain_01) instead of a photo.
