"""The Music Assistant provider's bookdoe client (integrations/music_assistant/bookdoe/client.py)
against the real Player API: a device token lists and plays audiobooks and podcast episodes, the
resume position comes back, progress is written to bookdoe, and a kid's token sees only the kid's
shelf. The MA-facing shim (provider.py) is tested with stubbed MA modules and checked by hand
(docs/MUSIC_ASSISTANT.md)."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from app import db
from app.main import app
from test_player_api import local_mode, pair, world  # noqa: F401  (fixtures)
from test_podcast_playback import _episode

SRC = Path(__file__).resolve().parents[1] / "integrations" / "music_assistant" / "bookdoe" / "client.py"
spec = importlib.util.spec_from_file_location("bookdoe_ma_client", SRC)
ma = importlib.util.module_from_spec(spec)
sys.modules["bookdoe_ma_client"] = ma
spec.loader.exec_module(ma)

BASE = "http://books.test"


def client_for(token):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    return ma.BookdoeClient(BASE, token, http=http, device_id="ma-test")


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [x async for x in agen]


def test_locate_and_elapsed_walk_the_tracks():
    t = [ma.Track("a", "u", 100), ma.Track("b", "u", 50)]
    assert ma.locate(t, 30)[1] == 30 and ma.locate(t, 120) == (t[1], 20) and ma.locate(t, 999)[0] is t[1]
    assert ma.elapsed_of(t, "b", 20) == 120 and ma.elapsed_of(t, "zzz", 5) == 0


def test_audiobook_plays_and_progress_round_trips_into_bookdoe(world):
    jane, jh = world["jane"]
    _, _, token = pair(jane, jh, name="Music Assistant", platform="music-assistant")
    w, a, fids = world["audio"]

    async def go():
        c = client_for(token)
        assert (await c.check())["account_type"] == "adult"
        books = await collect(c.audiobooks())
        work, asset = next((x, y) for x, y in books if x["title"] == "Spoken Book")
        assert asset["id"] == a
        plan = await c.plan(w, a)
        assert [t.id for t in plan.tracks] == fids and plan.duration == 1200 and plan.resume == 0
        # The stream URL works with the token and honours Range (what the speaker's ffmpeg does).
        body = b"".join([x async for x in c.stream(plan.tracks[1].url)])
        assert body.startswith(b"ID3")
        # 700 s into a two-track, 600 s-each book = 100 s into track two.
        assert await c.report_book(plan, 700, force=True) is True
        again = await c.plan(w, a)
        assert again.resume == 700
        # a second report inside the throttle window is skipped
        assert await c.report_book(plan, 710) is False
        await c.aclose()
    run(go())
    with db.session() as conn:
        row = conn.execute("SELECT file_id,position FROM media_progress WHERE asset_id=?", (a,)).fetchone()
    assert (row["file_id"], row["position"]) == (fids[1], 100)


def test_progress_appears_in_continue_listening(world):
    jane, jh = world["jane"]
    dev, _, token = pair(jane, jh)
    w, a, _ = world["audio"]

    async def go():
        c = client_for(token)
        await c.report_book(await c.plan(w, a), 50, force=True)
        await c.aclose()
    run(go())
    items = dev.get("/api/player/v1/continue").json()["items"]
    assert [i["work_id"] for i in items] == [w]


def test_kid_token_lists_only_the_kids_shelf_and_cannot_fetch_the_rest(world):
    owen, oh = world["owen"]
    _, _, token = pair(owen, oh, name="Owen MA")

    async def go():
        c = client_for(token)
        assert await collect(c.audiobooks()) == []
        with pytest.raises(httpx.HTTPStatusError) as e:
            await c.audiobook(world["audio"][0])
        assert e.value.response.status_code == 404
        await c.aclose()
    run(go())


def test_bad_token_is_refused(world):
    async def go():
        c = client_for("alxd_nope")
        with pytest.raises(httpx.HTTPStatusError) as e:
            await c.check()
        assert e.value.response.status_code in (401, 403)
        await c.aclose()
    run(go())


def test_podcast_episode_lists_resumes_and_saves_progress(world, tmp_path):
    jane, jh = world["jane"]
    _, _, token = pair(jane, jh)
    ep = _episode(tmp_path, root=False)

    async def go():
        c = client_for(token)
        shows = await c.podcasts()
        assert [s["title"] for s in shows] == ["The Show"]
        show = await c.show(shows[0]["id"])
        assert [e["id"] for s in show["seasons"] for e in s["episodes"]] == [ep]
        e = await c.episode(ep)
        assert e["url"].startswith(BASE) and e["position_seconds"] == 0
        assert (await c.report_episode(ep, 300, force=True)) is True
        assert (await c.episode(ep))["position_seconds"] == 300
        assert (await c.report_episode(ep, 310)) is False  # throttled
        body = b"".join([x async for x in c.stream(e["url"], 3)])
        assert body.startswith(bytes([0, 1, 2]))  # Range from byte 3, past "ID3"
        await c.aclose()
    run(go())


# ---- provider.py (the MA-facing layer) with minimal stubs of the Music Assistant modules ----
import types

PKG = SRC.parent


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return f"{cls.__name__}.{name}"


class _Stub(metaclass=_Meta):
    def __init__(self, *a, **kw):
        self.__dict__.update(kw)
        self.metadata = types.SimpleNamespace()


def _load_provider():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    names = {
        "music_assistant_models.config_entries": ["ConfigEntry", "ConfigValueType"],
        "music_assistant_models.enums": ["ConfigEntryType", "ContentType", "MediaType", "ProviderFeature", "StreamType"],
        "music_assistant_models.errors": ["LoginFailed", "MediaNotFoundError"],
        "music_assistant_models.media_items": ["Audiobook", "AudioFormat", "MediaItemChapter", "MediaItemImage", "Podcast",
                                              "PodcastEpisode", "ProviderMapping", "ImageType", "UniqueList"],
        "music_assistant_models.streamdetails": ["StreamDetails"],
    }
    mod("music_assistant_models")
    for m, cls in names.items():
        mod(m, **{c: type(c, (_Stub,), {}) for c in cls})
    mod("music_assistant")
    mod("music_assistant.models")
    mod("music_assistant.models.music_provider", MusicProvider=type("MusicProvider", (), {}))
    pspec = importlib.util.spec_from_file_location("bookdoe_ma_pkg", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
    pkg = importlib.util.module_from_spec(pspec)
    sys.modules["bookdoe_ma_pkg"] = pkg
    # the package's client module must be the one the tests above use for Track and BookPlan
    sys.modules["bookdoe_ma_pkg.client"] = ma
    pspec.loader.exec_module(pkg)
    sspec = importlib.util.spec_from_file_location("bookdoe_ma_pkg.provider", PKG / "provider.py")
    prov = importlib.util.module_from_spec(sspec)
    sys.modules["bookdoe_ma_pkg.provider"] = prov
    sspec.loader.exec_module(prov)
    return prov


class FakeClient:
    token = "alxd_x"

    def __init__(self, plan=None):
        self.plan_ = plan
        self.streamed, self.book_reports, self.episode_reports = [], [], []

    async def plan(self, work_id, asset_id):
        return self.plan_

    async def episode(self, episode_id):
        return {"url": "http://x/ep", "duration_seconds": 90, "episode_id": episode_id}

    async def stream(self, url, start=0):
        self.streamed.append(url)
        yield url.encode()

    async def report_book(self, plan, elapsed, *, force=False):
        self.book_reports.append((elapsed, force))

    async def report_episode(self, episode_id, position, *, force=False):
        self.episode_reports.append((episode_id, position, force))


def make_provider(plan=None):
    prov = _load_provider()
    p = object.__new__(prov.BookdoeProvider)
    p.client, p._plans, p._skew = FakeClient(plan), {}, {}
    p.instance_id = "bookdoe1"
    p.logger = types.SimpleNamespace(warning=lambda *a: None)
    return prov, p


def two_track_plan():
    return ma.BookPlan("w", "a", "T", [ma.Track("f1", "u1", 600), ma.Track("f2", "u2", 600)],
                       chapters=[{"title": "One", "start": 0}, {"title": "Two", "start": 600}])


def test_provider_stream_details_http_for_one_file_custom_for_several():
    prov, p = make_provider(ma.BookPlan("w", "a", "T", [ma.Track("f1", "u1", 600)]))
    sd = run(p.get_stream_details("w|a"))
    assert sd.stream_type == "StreamType.HTTP" and sd.path == "u1" and sd.duration == 600
    prov, p = make_provider(two_track_plan())
    sd = run(p.get_stream_details("w|a"))
    assert sd.stream_type == "StreamType.CUSTOM" and sd.duration == 1200
    ep = run(p.get_stream_details("e1", prov.MediaType.PODCAST_EPISODE))
    assert ep.stream_type == "StreamType.HTTP" and ep.path == "http://x/ep"


def test_provider_multi_file_seek_streams_from_the_right_track_and_keeps_progress_consistent():
    prov, p = make_provider(two_track_plan())
    sd = run(p.get_stream_details("w|a"))
    assert [c.decode() for c in run(collect(p.get_audio_stream(sd, 0)))] == ["u1", "u2"]
    # Seek to 700 s: playback starts at the head of track two (600 s); MA thinks it is at 700 s.
    p.client.streamed.clear()
    assert [c.decode() for c in run(collect(p.get_audio_stream(sd, 700)))] == ["u2"]
    # 30 s later MA reports 730 s; the audio actually played is 630 s into the book.
    run(p.on_played(prov.MediaType.AUDIOBOOK, "w|a", False, 730, is_playing=True))
    assert p.client.book_reports == [(630.0, False)]
    # A fresh stream from the start clears the skew.
    run(collect(p.get_audio_stream(sd, 0)))
    run(p.on_played(prov.MediaType.AUDIOBOOK, "w|a", True, 1200))
    assert p.client.book_reports[-1] == (1200.0, True)


def test_provider_on_played_reports_episode_and_book():
    prov, p = make_provider(two_track_plan())
    run(p.on_played(prov.MediaType.PODCAST_EPISODE, "e1", False, 42, is_playing=True))
    assert p.client.episode_reports == [("e1", 42.0, False)]
    run(p.on_played(prov.MediaType.AUDIOBOOK, "w|a", True, 100))
    assert p.client.book_reports == [(100.0, True)]


def test_provider_chapters_start_from_the_plan():
    prov, p = make_provider(two_track_plan())

    async def audiobook(work_id):
        return {"id": "w", "title": "T"}, {"id": "a", "duration_seconds": 1200}

    p.client.audiobook = audiobook
    p.client.absolute = lambda u: u
    p.domain = "bookdoe"
    book = run(p.get_audiobook("w|a"))
    assert [(c.name, c.start) for c in book.metadata.chapters] == [("One", 0.0), ("Two", 600.0)]
