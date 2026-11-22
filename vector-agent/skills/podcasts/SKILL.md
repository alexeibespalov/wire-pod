---
name: podcasts
description: Find and play podcast episodes ("play the latest Rest Is History", "play the newest episode of Radiolab", "play the Lex Fridman episode about robots"). Also stop/pause requests while a podcast plays.
tools: play_podcast
---
# Playing podcasts

`play_podcast(show, episode, volume)` looks the show up in Apple's public podcast
directory, reads its feed and streams an episode through your speaker. It starts
right after you finish speaking.

- `show`: just the podcast's name ("The Rest Is History"). Speech-to-text may garble
  it: fix obvious mishearings before searching ("the rest is his story" -> "The Rest Is
  History").
- `episode`: leave empty for the newest episode; otherwise a few distinctive words from
  the episode title or topic.
- Tell them which episode you found, in one short sentence ("Here's the latest one,
  'The Fall of Rome', from last Tuesday.").
- Episodes can be long; `stop_music()` stops it. `now_playing()` says what's on.
- If the show can't be found, ask them to spell the name or try a shorter version.
- Never use dance mode for podcasts.
