---
name: music
description: Play music from the household music library on the NAS ("play some music", "play Radiohead", "play the album OK Computer", "play Bohemian Rhapsody", "shuffle"), and stop it or say what's playing ("stop the music", "what's this song?").
tools: play_music
---
# Playing music from the library

`play_music(query, mode, dance, volume)` plays from the family's music library (about
7,800 songs on the NAS, organised by artist and album). Music comes out of your own
small speaker and starts right after you finish speaking.

## Picking the mode
- `song`: one specific track ("play Bohemian Rhapsody").
- `artist`: a shuffle of everything by one artist ("play some Radiohead").
- `album`: a whole album in order ("play the album OK Computer").
- `shuffle`: `query=""` for a random mix of everything ("play some music"), or a
  word to shuffle matching music ("shuffle some jazz").
- Put only the useful words in `query` (artist and/or title), not "play" or "please".

## Talking
- Say one short sentence ("Ooh, good choice! Here's some Radiohead.") and stop: the
  music starts as soon as you finish.
- If the library has nothing matching, don't keep retrying with other modes or words:
  try at most once more (e.g. artist mode if song mode failed), then say plainly it's
  not in the library and offer a MIDI version (midi skill) or the closest artist the
  tool suggested. Never narrate your searching.
- `stop_music()` when they say stop / enough / turn it off / quiet. `now_playing()` for
  "what song is this?".
- Your speaker is tiny: it's fun, not hi-fi. Don't oversell it.
- To dance as well, see the dj skill.
