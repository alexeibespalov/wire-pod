"""Music: the MPD protocol (against a fake MPD), library matching, and the rule that the
music waits for Cortana to finish talking and comes back after."""
import asyncio
import time

import pytest

from agent_core import ytmusic
from app import config, music

LISTING = [
    ("directory", "Radiohead"), ("directory", "Radiohead/OK Computer"),
    ("file", "Radiohead/OK Computer/01 Airbag.mp3"),
    ("file", "Radiohead/OK Computer/02 Paranoid Android.flac"),
    ("file", "Radiohead/OK Computer/cover.jpg"),
    ("file", "Queen/A Night at the Opera/11 Bohemian Rhapsody.mp3"),
    ("file", "NOFX/Punk in Drublic/01 Linoleum.mp3"),
    ("file", "Daft Punk/Discovery/01 One More Time.mp3"),
]


class FakeMPD:
    """Just enough of MPD: status/stats/listall/currentsong and a play state."""

    def __init__(self):
        self.state, self.queue, self.volume, self.log = "stop", [], 100, []
        self.tags = {}                                   # song id -> {tag: value}, for streams

    def answer(self, cmd: str) -> list[str]:
        self.log.append(cmd)
        name, _, arg = cmd.partition(" ")
        if name == "status":
            return [f"state: {self.state}", f"volume: {self.volume}",
                    f"playlistlength: {len(self.queue)}", "song: 0"]
        if name == "stats":
            return ["songs: 5", "db_update: 1700000000"]
        if name == "listall":
            return [f"{k}: {v}" for k, v in LISTING]
        if name == "currentsong":
            if not self.queue:
                return []
            return [f"file: {self.queue[0]}", *(f"{k}: {v}" for k, v in self.tags.get(1, {}).items())]
        if name == "addid":
            self.queue.append(arg.strip('"'))
            return [f"Id: {len(self.queue)}"]
        if name == "addtagid":
            song_id, tag, value = arg.split(" ", 2)
            self.tags.setdefault(int(song_id), {})[tag] = value.strip('"')
            return []
        if name == "clear":
            self.queue, self.tags = [], {}
        elif name == "add":
            self.queue.append(arg.strip('"').replace('\\"', '"'))
        elif name == "play":
            self.state = "play"
        elif name == "pause":
            self.state = "pause"
        elif name == "stop":
            self.state = "stop"
        elif name == "setvol":
            self.volume = int(arg)
        elif name not in ("next", "previous"):
            raise ValueError(name)
        return []

    async def serve(self, reader, writer):
        writer.write(b"OK MPD 0.24.0\n")
        while line := (await reader.readline()).decode().rstrip("\n"):
            if line == "command_list_begin":
                cmds = []
                while (c := (await reader.readline()).decode().rstrip("\n")) != "command_list_end":
                    cmds.append(c)
            else:
                cmds = [line]
            try:
                out = [x for c in cmds for x in self.answer(c)]
                writer.write(("".join(x + "\n" for x in out) + "OK\n").encode())
            except ValueError as e:
                writer.write(f"ACK [5@0] {{{e}}} unknown command\n".encode())
            await writer.drain()
        writer.close()


@pytest.fixture
def mpd(monkeypatch):
    fake = FakeMPD()

    async def start():
        server = await asyncio.start_server(fake.serve, "127.0.0.1", 0)
        monkeypatch.setattr(config, "MPD_HOST", "127.0.0.1")
        monkeypatch.setattr(config, "MPD_PORT", server.sockets[0].getsockname()[1])
        return server
    fake.start = start
    music._tracks.clear()
    music.PLAYER.__init__()
    return fake


def test_quote():
    assert music.quote('AC/DC "Back"\\x') == '"AC/DC \\"Back\\"\\\\x"'


def test_listing_to_tracks():
    t = music.tracks_from_listing(LISTING)
    assert len(t) == 5                                   # directories and the jpg left out
    assert t[1] == {"path": "Radiohead/OK Computer/02 Paranoid Android.flac", "artist": "Radiohead",
                    "album": "OK Computer", "title": "02 Paranoid Android"}


def test_search_modes():
    t = music.tracks_from_listing(LISTING)
    picks, desc = music.search(t, "bohemian rhapsody", "song")
    assert [p["artist"] for p in picks] == ["Queen"] and "Bohemian" in desc
    picks, _ = music.search(t, "radiohead", "artist")
    assert len(picks) == 2
    picks, desc = music.search(t, "ok computer", "album")
    assert [p["title"] for p in picks] == ["01 Airbag", "02 Paranoid Android"]   # in order
    assert music.search(t, "punk", "song")[0][0]["artist"] in ("NOFX", "Daft Punk")
    assert music.search(t, "daft punk", "artist")[0][0]["artist"] == "Daft Punk"  # every word
    assert music.search(t, "beyonce", "artist")[0] == []
    assert len(music.search(t, "", "shuffle")[0]) == 5


def test_play_waits_for_her_then_plays(mpd):
    async def go():
        server = await mpd.start()
        busy = {"v": True}
        music.PLAYER.idle = lambda: not busy["v"]
        out = await music.play_music.ainvoke({"query": "ok computer", "mode": "album", "volume": 60})
        assert out.startswith("queued the album OK Computer by Radiohead (2 tracks")
        assert mpd.queue[0].endswith("Airbag.mp3") and mpd.volume == 60 and mpd.state == "stop"
        music.PLAYER.touch()
        await music.PLAYER.step()
        assert mpd.state == "stop"                       # she's still talking
        busy["v"] = False
        music.PLAYER.last_voice = time.monotonic() - 10
        await music.PLAYER.step()
        assert mpd.state == "play"                       # quiet: the music starts
        server.close()
    asyncio.run(go())


def test_wake_word_pauses_and_it_comes_back(mpd):
    async def go():
        server = await mpd.start()
        await music.play_music.ainvoke({"query": "radiohead", "mode": "artist"})
        music.PLAYER.last_voice = time.monotonic() - 10
        await music.PLAYER.step()
        assert mpd.state == "play"
        await music.PLAYER.duck()                        # "Okay Nabu"
        assert mpd.state == "pause"
        music.PLAYER.idle = lambda: False                # listening + answering
        await music.PLAYER.step()
        assert mpd.state == "pause" and music.PLAYER.want
        music.PLAYER.idle = lambda: True
        music.PLAYER.last_voice = time.monotonic() - 3   # done talking
        await music.PLAYER.step()
        assert mpd.state == "play"
        server.close()
    asyncio.run(go())


def test_someone_else_pausing_is_respected(mpd):
    async def go():
        server = await mpd.start()
        await music.play_music.ainvoke({"query": "queen", "mode": "artist"})
        music.PLAYER.last_voice = time.monotonic() - 60
        await music.PLAYER.step()
        assert mpd.state == "play"
        mpd.state = "pause"                              # paused from the app, nobody talking
        await music.PLAYER.step()
        assert not music.PLAYER.want
        await music.PLAYER.step()
        assert mpd.state == "pause"
        server.close()
    asyncio.run(go())


def test_controls(mpd):
    async def go():
        server = await mpd.start()
        assert "nothing is playing" in await music.now_playing.ainvoke({})
        await music.play_music.ainvoke({"query": "one more time"})
        assert "One More Time by Daft Punk" in await music.now_playing.ainvoke({})
        assert await music.music_control.ainvoke({"action": "pause"}) == "music paused"
        assert not music.PLAYER.want
        assert "set to 30" in await music.music_control.ainvoke({"action": "volume", "level": 30})
        assert (await music.play_music.ainvoke({"query": "zzz top"})).startswith("error:")
        server.close()
    asyncio.run(go())


def test_started_elsewhere_is_looked_after(mpd):
    async def go():
        server = await mpd.start()
        await music.play_music.ainvoke({"query": "queen", "mode": "artist"})
        await music.music_control.ainvoke({"action": "pause"})
        assert not music.PLAYER.want
        mpd.state = "play"                               # the Echo's play button
        music.PLAYER.last_voice = time.monotonic() - 60
        await music.PLAYER.step()
        assert music.PLAYER.want                         # so it comes back after she talks
        server.close()
    asyncio.run(go())


def test_tell_speaker_sends_the_sentence(mpd):
    async def go():
        server = await mpd.start()
        mpd.answer_orig = mpd.answer
        mpd.answer = lambda c: [] if c.startswith("sendmessage") and not mpd.log.append(c) else mpd.answer_orig(c)
        music.tell_speaker('say Here\'s some "Queen".')
        await asyncio.sleep(0.3)
        assert mpd.log == ['sendmessage cortana "say Here\'s some \\"Queen\\"."']
        server.close()
    asyncio.run(go())


def test_bare_stop_means_the_music(mpd):
    async def go():
        server = await mpd.start()
        assert not music.is_stop_request("Stop.")          # no music: it's a plain never-mind
        await music.play_music.ainvoke({"query": "queen", "mode": "artist"})
        mpd.state = "play"
        for said in ("Stop.", "stop!", "Pause", "quiet", "That's enough."):
            assert music.is_stop_request(said), said
        for said in ("Never mind.", "stop the timer", "what's this song?"):
            assert not music.is_stop_request(said), said
        await music.stop_for_voice()
        assert mpd.state == "pause" and not music.PLAYER.want
        music.PLAYER.last_voice = time.monotonic() - 60
        await music.PLAYER.step()
        assert mpd.state == "pause"                         # stays paused
        server.close()
    asyncio.run(go())


# ------------------------------------------------------------------ YouTube Music
YT = [{"id": "aaaaaaaaaaa", "title": "Hey Ya!", "artist": "Outkast", "album": "The Love Below", "duration": 235},
      {"id": "bbbbbbbbbbb", "title": "Roses", "artist": "Outkast", "album": "", "duration": 369}]


class FakeYouTube:
    """A YouTube Music that only has Outkast, and records what it was asked."""

    def __init__(self):
        self.asked, self.error = [], "nothing on YouTube Music matches that"

    async def search(self, query, mode):
        self.asked.append((query, mode))
        return (YT, "songs by Outkast") if "outkast" in query or mode == "liked" else ([], self.error)


@pytest.fixture
def youtube(monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(ytmusic, "available", lambda: True)
    monkeypatch.setattr(ytmusic, "search", fake.search)
    monkeypatch.setattr(ytmusic, "warm", lambda video_id: None)
    return fake


def test_what_the_library_lacks_comes_from_youtube(mpd, youtube):
    async def go():
        server = await mpd.start()
        out = await music.play_music.ainvoke({"query": "outkast", "mode": "artist"})
        assert out.startswith("queued songs by Outkast (2 tracks queued), from YouTube Music")
        assert mpd.queue == [ytmusic.stream_url("aaaaaaaaaaa"), ytmusic.stream_url("bbbbbbbbbbb")]
        assert mpd.tags == {1: {"Title": "Hey Ya!", "Artist": "Outkast", "Album": "The Love Below"},
                            2: {"Title": "Roses", "Artist": "Outkast"}}
        assert music.PLAYER.want and mpd.state == "stop"         # waits for her, like any music
        assert (await music.now_playing.ainvoke({})).startswith("stopped: Hey Ya! by Outkast")
        server.close()
    asyncio.run(go())


def test_the_library_is_tried_first(mpd, youtube):
    async def go():
        server = await mpd.start()
        out = await music.play_music.ainvoke({"query": "radiohead", "mode": "artist"})
        assert "from the family library" in out and youtube.asked == []
        out = await music.play_music.ainvoke({"query": "outkast", "mode": "artist", "source": "nas"})
        assert out.startswith("error: no artist called") and youtube.asked == []
        out = await music.play_music.ainvoke({"query": "radiohead outkast", "mode": "artist", "source": "youtube"})
        assert "from YouTube Music" in out and youtube.asked == [("radiohead outkast", "artist")]
        server.close()
    asyncio.run(go())


def test_liked_songs_and_failures(mpd, youtube):
    async def go():
        server = await mpd.start()
        assert "from YouTube Music" in await music.play_music.ainvoke({"mode": "liked"})
        youtube.error = ytmusic.EXPIRED
        out = await music.play_music.ainvoke({"query": "gym", "mode": "playlist"})
        assert out == f"error: {ytmusic.EXPIRED}"
        out = await music.play_music.ainvoke({"query": "beyonce", "mode": "artist"})
        assert "in the music library" in out and "on YouTube Music" in out   # both said no
        server.close()
    asyncio.run(go())


def test_without_youtube_music_it_is_the_library_only(mpd):
    async def go():
        server = await mpd.start()
        out = await music.play_music.ainvoke({"query": "beyonce", "mode": "artist"})
        assert out.startswith("error: no artist called")
        out = await music.play_music.ainvoke({"mode": "liked"})
        assert out.startswith("error: playlists and liked songs come from YouTube Music")
        server.close()
    asyncio.run(go())
