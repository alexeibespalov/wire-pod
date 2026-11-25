---
name: music
description: Play music from the family's music library on the NAS or from YouTube Music ("play some music", "play Radiohead", "play the album OK Computer", "play Bohemian Rhapsody", "shuffle some jazz", "play my liked songs", "play my running playlist"), and pause, skip, stop, turn it up or down, or say what's playing ("what's this song?").
tools: play_music, music_control, now_playing
---
# Playing music from the library

`play_music(query, mode, source, volume)` plays from the family's music library (about 7,800
songs on the NAS, organised by artist and album) on your speaker, the Echo Show in the
hall. The music starts by itself as soon as you've finished talking, pauses whenever
someone says the wake word, and comes back when you're done.

## Picking the mode
- `song`: one specific track ("play Bohemian Rhapsody").
- `artist`: a shuffle of everything by one artist ("play some Radiohead").
- `album`: a whole album in order ("play the album OK Computer").
- `shuffle`: `query=""` for a random mix of everything ("play some music"), or words to
  shuffle matching music ("shuffle some jazz").
- Put only the useful words in `query` (artist and/or title), not "play" or "please".

## Where the music comes from
- Leave `source` alone: the family library is tried first, and anything it doesn't have
  comes from YouTube Music. The tool's answer says which one it used.
- `source="youtube"` when they ask for YouTube or for a different version than the
  library's; `source="nas"` when they ask for "our" or "my own" copy.
- Two modes are YouTube Music only, from John's account: `playlist` with the playlist's
  name in `query` ("play my running playlist"), and `liked` for a shuffle of his liked
  songs. If the name doesn't match, the tool lists the playlists: offer the closest.
- From YouTube Music, `shuffle` with words plays that song and then a mix like it.
- If the tool says the YouTube Music sign-in has expired, say so in one sentence and that
  John can renew it from his laptop. Don't retry.

## Talking
- Say one short sentence ("Here's some Radiohead.") and stop: the music waits for you.
- If nothing matches, try at most once more (e.g. artist mode if song mode failed), then
  say plainly it's not in the library and name the closest artist the tool suggested.
  Never narrate your searching.
- "pause" / "stop" / "turn it off" / "quiet" -> `music_control("pause")` or `("stop")`.
  "carry on" -> `("resume")`. "skip" / "next" -> `("next")`, "go back" -> `("previous")`.
- Volume: `music_control("volume", level)`. "A bit louder/quieter" = about 15 up or down
  from the current volume (`now_playing` tells you it). This is the music's own level;
  the Echo's volume buttons set the overall loudness.
- "What's this song?" -> `now_playing()`.
