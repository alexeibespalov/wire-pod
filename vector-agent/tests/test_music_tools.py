"""play_music picks between the household library and YouTube Music, and hands YouTube
tracks to the player as stream addresses (the path podcasts already use). No network."""
import asyncio

import pytest

from agent_core import ytmusic
from app import library, music_tools

NAS = [{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer", "path": "/media/Music/a.mp3"}]
YT = [{"id": "aaaaaaaaaaa", "title": "Hey Ya!", "artist": "Outkast", "album": "", "duration": 235},
      {"id": "bbbbbbbbbbb", "title": "Intro", "artist": "", "album": "", "duration": 60}]


@pytest.fixture
def world(monkeypatch):
    """A library that only has Radiohead, a YouTube Music that only has Outkast, and a
    player that records what it was given."""
    w = {"queued": None, "asked": [], "youtube": True, "error": "nothing on YouTube Music matches that"}
    monkeypatch.setattr(library, "search", lambda q, mode: (NAS, "songs by Radiohead") if "radiohead" in q
                        else ([], f"no artist called {q!r} in the music library"))

    async def search(query, mode):
        w["asked"].append((query, mode))
        return (YT, "songs by Outkast") if "outkast" in query or mode == "liked" else ([], w["error"])
    monkeypatch.setattr(ytmusic, "available", lambda: w["youtube"])
    monkeypatch.setattr(ytmusic, "search", search)
    monkeypatch.setattr(ytmusic, "warm", lambda video_id: None)
    monkeypatch.setattr(music_tools.PLAYER, "queue", lambda items, volume, dance, label: w.update(
        queued=(items, volume, dance, label)))
    return w


def play(**args):
    return asyncio.run(music_tools.play_music.ainvoke(args))


def test_the_library_is_tried_first(world):
    out = play(query="radiohead", mode="artist")
    assert "from the household library" in out and world["asked"] == []
    items, volume, dance, label = world["queued"]
    assert items[0].source == "/media/Music/a.mp3" and not items[0].is_url and volume == 70


def test_what_the_library_lacks_comes_from_youtube(world):
    out = play(query="outkast", mode="artist", dance=True, volume=40)
    assert out.startswith("queued songs by Outkast (2 tracks queued), from YouTube Music, and you will dance")
    items, volume, dance, label = world["queued"]
    assert [(i.title, i.source, i.is_url) for i in items] == [
        ("Hey Ya! by Outkast", ytmusic.stream_url("aaaaaaaaaaa"), True),
        ("Intro", ytmusic.stream_url("bbbbbbbbbbb"), True)]
    assert (volume, dance, label) == (40, True, "songs by Outkast")


def test_source_and_youtube_only_modes(world):
    assert play(query="outkast", mode="artist", source="nas").startswith("error: no artist called")
    assert world["asked"] == [] and world["queued"] is None
    assert "from YouTube Music" in play(query="radiohead outkast", mode="artist", source="youtube")
    assert "from YouTube Music" in play(mode="liked")
    world["error"] = ytmusic.EXPIRED
    assert play(query="gym", mode="playlist") == f"error: {ytmusic.EXPIRED}"
    out = play(query="beyonce", mode="artist")
    assert "in the music library" in out and "on YouTube Music" not in out.split(";")[0]


def test_without_youtube_music_it_is_the_library_only(world):
    world["youtube"] = False
    assert play(query="beyonce", mode="artist").startswith("error: no artist called")
    assert play(mode="liked").startswith("error: playlists and liked songs come from YouTube Music")
    assert world["asked"] == []
