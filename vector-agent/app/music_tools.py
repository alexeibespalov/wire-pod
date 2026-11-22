"""Music / podcast / MIDI tools. Playback is queued and starts after Vector finishes
speaking his reply (voice), or right after the turn (A2A). See app/audio.py."""
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from . import library, midi, podcasts
from .audio import PLAYER, Item

_AFTER = "It starts right after you finish speaking, so keep your reply to one short sentence."
_Dance = Annotated[bool, Field(description="True = also start your dance-to-the-beat routine (DJ mode)")]
_Volume = Annotated[int, Field(description="Playback volume 10-100 (default 70)")]


@tool
async def play_music(
        query: Annotated[str, Field(description="Song, artist or album to find in the music library; "
                                                "empty with mode=shuffle for anything")] = "",
        mode: Literal["song", "artist", "album", "shuffle"] = "song",
        dance: _Dance = False,
        volume: _Volume = 70) -> str:
    """Play music from the household music library (the NAS). Load the 'music' skill first."""
    tracks, desc = library.search(query, mode)
    if not tracks:
        return f"error: {desc}"
    items = [Item(title=f"{t['title']} by {t['artist']}", source=t["path"]) for t in tracks]
    PLAYER.queue(items, volume, dance, desc)
    more = f" ({len(items)} tracks queued)" if len(items) > 1 else ""
    return f"queued {desc}{more}{', and you will dance' if dance else ''}. {_AFTER}"


@tool
async def play_podcast(
        show: Annotated[str, Field(description="Podcast name, e.g. 'The Rest Is History'")],
        episode: Annotated[str, Field(description="Words from the episode title, or empty for the latest")] = "",
        volume: _Volume = 70) -> str:
    """Find a podcast and play an episode (latest by default). Load the 'podcasts' skill first."""
    found = await podcasts.find_show(show)
    if not found:
        return f"error: couldn't find a podcast called {show!r}"
    eps = await podcasts.episodes(found["feedUrl"])
    ep = podcasts.pick(eps, episode or None)
    if not ep:
        return f"error: {found.get('collectionName', show)} has no playable episodes"
    name = found.get("collectionName", show)
    PLAYER.queue([Item(title=ep["title"], source=ep["url"], is_url=True)], volume, False, f"the podcast {name}")
    when = f" from {ep['date']}" if ep.get("date") else ""
    return f"queued {name}: '{ep['title']}'{when}. {_AFTER}"


@tool
async def play_midi(
        query: Annotated[str, Field(description="Song to look for on midiworld.com, e.g. 'beatles yesterday'")],
        dance: _Dance = False,
        volume: _Volume = 70) -> str:
    """Find a MIDI version of a song on midiworld.com, render it and play it. Load the
    'midi' skill first. Good for songs not in the music library, and for DJ mode."""
    results = await midi.search(query)
    pick = midi.best(results, query)
    if not pick:
        return f"error: no MIDI found on midiworld for {query!r}"
    try:
        wav, tmp = await midi.fetch_and_render(pick["url"])
    except ValueError as e:
        return f"error: {e}"
    PLAYER.queue([Item(title=pick["title"], source=str(wav), cleanup=tmp)], volume, dance,
                 f"a MIDI of {pick['title']}")
    return f"queued a MIDI version of {pick['title']}{', and you will dance' if dance else ''}. {_AFTER}"


@tool
async def stop_music() -> str:
    """Stop whatever music or podcast is playing."""
    return "stopped" if await PLAYER.stop() else "nothing was playing"


@tool
async def now_playing() -> str:
    """What's playing right now (title, source, how long)."""
    return PLAYER.describe()


MUSIC_TOOLS = [play_music, play_podcast, play_midi, stop_music, now_playing]
