---
name: midi
description: Play a MIDI version of almost any famous song from midiworld.com ("play a MIDI of Yesterday", "play the Mario theme", "play Für Elise"), especially songs that aren't in the music library. Good for DJ mode.
---
# Playing MIDI songs from midiworld

`play_midi(query, dance, volume)` searches midiworld.com (a big free MIDI collection:
pop, rock, classical, TV and game themes), downloads the best match, safely renders it
to audio with a General MIDI synth and plays it. It starts right after you finish speaking.

- `query`: song title, plus the artist or composer if known ("beatles yesterday",
  "beethoven fur elise", "super mario theme"). Fix speech-to-text mishearings first.
- It sounds like an old-school synth version, not the original recording: say so
  playfully if it fits ("Here's my chiptune take on Yesterday!").
- Prefer the music library (music skill) when they want the real recording and it's
  likely to be there; use MIDI when it isn't, or when they ask for MIDI / a synth
  version / a game or TV theme.
- If nothing is found or the file is rejected, say so and offer another song.
- Safety is handled by the tool (downloads only from midiworld, size and length
  limits, validated before rendering); never try to work around its errors.
