---
name: dj
description: DJ mode - play music and dance to it at the same time ("DJ for me", "put on some music and dance", "dance party!", "play Daft Punk and dance").
---
# DJ mode: music + your built-in dancing

In DJ mode you play a track AND dance to it. Your built-in dance-to-the-beat routine
listens through your own microphones, finds the beat of the music coming out of your
speaker, and dances in time. Verified: it works.

## How
1. Load the music skill (library) or the midi skill (MIDI), whichever source fits:
   - a real song they named that's likely in the library -> `play_music(..., dance=True)`
   - a famous tune not in the library, a game/TV theme, or "something fun" ->
     `play_midi(..., dance=True)`
   - "just play something to dance to" -> `play_music(query="", mode="shuffle", dance=True)`
2. Set `dance=True`: about 3 seconds after the music starts, your dance routine kicks in.
   Don't call do_action("dance") separately.
3. Say one hyped-up sentence ("Let's go, dance party!"), then stop talking.

## Good to know
- The dance routine drives you off your charger first and needs some room on the desk.
- It listens for about 10-15 seconds to find the beat before dancing, so pick songs
  with a clear, steady beat (dance, pop, rock, disco, game themes). Slow ballads,
  speech and classical without a pulse don't work well.
- "Stop" stops the music; your dance routine winds down by itself.
