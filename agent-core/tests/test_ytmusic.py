"""YouTube Music: finding tracks (a fake ytmusicapi client), resolving (a fake yt-dlp),
and the stream route (a fake YouTube). No network.

Run:  cd agent-core && python -m pytest tests -q
"""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import test_core  # noqa: F401  (registers the fake agent config the core modules read)
from agent_core import ytmusic
from agent_core.settings import cfg

A, B = "aaaaaaaaaaa", "bbbbbbbbbbb"


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(test_core.cfg, "DATA_DIR", tmp_path)
    ytmusic._resolved.clear()
    ytmusic._client_cache = None


def sign_in(tmp_path):
    (tmp_path / "ytmusic").mkdir(exist_ok=True)
    (tmp_path / "ytmusic" / "cookies.txt").write_text(
        "# Netscape HTTP Cookie File\n\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tsid\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsap\n"
        ".google.com\tTRUE\t/\tTRUE\t0\tNID\tnot-for-youtube\n")


class FakeClient:
    def __init__(self, alive=True, broken=False):
        self.alive, self.broken = alive, broken

    def search(self, query, filter, limit):
        if self.broken:
            raise KeyError("contents")
        if filter == "albums":
            return [{"browseId": "ALB"}]
        return [{"videoId": A, "title": "Airbag", "artists": [{"name": "Radiohead"}],
                 "album": {"name": "OK Computer"}, "duration_seconds": 284},
                {"videoId": None, "title": "no id"}]

    def get_album(self, browse_id):
        return {"title": "OK Computer", "artists": [{"name": "Radiohead"}], "tracks": [
            {"videoId": A, "title": "Airbag", "artists": [{"name": "Radiohead"}], "album": "OK Computer"},
            {"videoId": B, "title": "Gone", "artists": [], "isAvailable": False}]}

    def get_library_playlists(self, limit):
        return [{"playlistId": "P1", "title": "Running Mix"}, {"playlistId": "P2", "title": "Dinner"}]

    def get_playlist(self, playlist_id, limit):
        return {"tracks": [{"videoId": B, "title": "Go", "artists": [{"name": "X"}], "length": "3:05"}]}

    def get_account_info(self):
        if not self.alive:
            raise RuntimeError("401")
        return {"accountName": "someone"}


def find(query, mode):
    return asyncio.run(ytmusic.search(query, mode))


def test_cookie_header_takes_only_youtube_cookies(tmp_path):
    sign_in(tmp_path)
    assert ytmusic.cookie_header(tmp_path / "ytmusic" / "cookies.txt") == "__Secure-3PSID=sid; SAPISID=sap"


def test_song_and_album(monkeypatch):
    monkeypatch.setattr(ytmusic, "_client", lambda: FakeClient())
    picks, desc = find("airbag", "song")
    assert picks == [{"id": A, "title": "Airbag", "artist": "Radiohead", "album": "OK Computer", "duration": 284}]
    assert desc == "Airbag by Radiohead"
    picks, desc = find("ok computer", "album")
    assert [t["id"] for t in picks] == [A] and desc == "the album OK Computer by Radiohead"   # unavailable left out
    assert find("", "song") == ([], "say what to play")


def test_playlists_need_the_sign_in(tmp_path, monkeypatch):
    monkeypatch.setattr(ytmusic, "_client", lambda: FakeClient())
    assert find("running", "playlist") == ([], ytmusic.NOT_SIGNED_IN)
    assert find("", "liked") == ([], ytmusic.NOT_SIGNED_IN)
    sign_in(tmp_path)
    picks, desc = find("running", "playlist")
    assert picks[0]["id"] == B and picks[0]["duration"] == 185 and desc == "the playlist Running Mix"
    picks, desc = find("gym", "playlist")
    assert picks == [] and "Running Mix, Dinner" in desc           # tells her what there is


def test_a_dead_session_is_named_as_such(tmp_path, monkeypatch):
    monkeypatch.setattr(ytmusic, "_client", lambda: FakeClient(alive=False, broken=True))
    assert "didn't answer" in find("airbag", "song")[1]            # not signed in: just a failure
    sign_in(tmp_path)
    assert find("airbag", "song") == ([], ytmusic.EXPIRED)
    monkeypatch.setattr(ytmusic, "_client", lambda: FakeClient(alive=True, broken=True))
    assert "didn't answer" in find("airbag", "song")[1]            # signed in and alive: not the sign-in


def test_resolve_is_cached_until_the_url_expires(monkeypatch):
    calls = []

    def extract(video_id):
        calls.append(video_id)
        return f"https://yt.test/{video_id}?expire={int(time.time()) + 6 * 3600}&n={len(calls)}", {"User-Agent": "x"}
    monkeypatch.setattr(ytmusic, "_extract", extract)

    async def go():
        first = await ytmusic.resolve(A)
        assert await ytmusic.resolve(A) == first and calls == [A]
        ytmusic._resolved[A] = (time.time() - 1, *first)           # past its time
        assert (await ytmusic.resolve(A))[0].endswith("n=2")
        with pytest.raises(ytmusic.YTMusicError):
            await ytmusic.resolve("../../etc")
    asyncio.run(go())


class Body(httpx.AsyncByteStream):
    """A body that is streamed, as the real one is. Given fewer bytes than the
    Content-Length says, it ends the way a dropped connection does."""

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def fake_youtube(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(ytmusic.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_stream_needs_the_signature():
    client = TestClient(ytmusic.stream_app())
    assert client.get(f"/yt/stream/{A}").status_code == 403
    assert client.get(f"/yt/stream/{A}?s={ytmusic.sign(B)}").status_code == 403
    assert client.get(f"/yt/stream/not-an-id?s={ytmusic.sign('not-an-id')}").status_code == 403
    assert ytmusic.stream_url(A) == f"{cfg.YTMUSIC_STREAM_BASE}/yt/stream/{A}?s={ytmusic.sign(A)}"


def test_stream_relays_and_survives_a_stale_url_and_a_dropped_connection(monkeypatch):
    song = bytes(range(256)) * 40
    seen = []
    monkeypatch.setattr(ytmusic, "_extract", lambda vid: (f"https://yt.test/{vid}?v={len(seen)}", {}))

    def youtube(request):
        seen.append((str(request.url), request.headers.get("range")))
        if len(seen) == 1:
            return httpx.Response(403)                             # the cached URL went stale
        rng = request.headers.get("range")
        if not rng:                                                # drops after 1000 bytes
            return httpx.Response(200, headers={"content-type": "audio/mp4", "content-length": str(len(song))},
                                  stream=Body(song[:1000]))
        start = int(rng.removeprefix("bytes=").split("-")[0])
        return httpx.Response(206, stream=Body(song[start:]), headers={
            "content-type": "audio/mp4", "content-length": str(len(song) - start),
            "content-range": f"bytes {start}-{len(song) - 1}/{len(song)}"})
    fake_youtube(monkeypatch, youtube)

    r = TestClient(ytmusic.stream_app()).get(f"/yt/stream/{A}?s={ytmusic.sign(A)}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4" and r.content == song
    assert [rng for _, rng in seen] == [None, None, f"bytes=1000-{len(song) - 1}"]
    assert seen[0][0] != seen[1][0]                                # resolved again after the 403


def test_stream_passes_a_range_request_through(monkeypatch):
    monkeypatch.setattr(ytmusic, "_extract", lambda vid: ("https://yt.test/x", {}))
    fake_youtube(monkeypatch, lambda req: httpx.Response(
        206, stream=Body(b"tail"), headers={"content-range": "bytes 96-99/100", "content-length": "4",
                                            "content-type": "audio/mp4"})
        if req.headers.get("range") == "bytes=96-" else httpx.Response(500))
    r = TestClient(ytmusic.stream_app()).get(f"/yt/stream/{A}?s={ytmusic.sign(A)}", headers={"Range": "bytes=96-"})
    assert r.status_code == 206 and r.content == b"tail" and r.headers["content-range"] == "bytes 96-99/100"


def test_stream_says_when_youtube_refuses(monkeypatch):
    def refuse(video_id):
        raise ytmusic.SignInExpired()
    monkeypatch.setattr(ytmusic, "_extract", refuse)
    r = TestClient(ytmusic.stream_app()).get(f"/yt/stream/{A}?s={ytmusic.sign(A)}")
    assert r.status_code == 502 and "expired" in r.json()["detail"]
