"""Music Assistant side of the bookdoe provider: maps bookdoe works and shows onto MA
audiobooks, podcasts and episodes, and reports progress back through client.py.

Written against the Music Assistant 2.x provider interface; it is only importable inside
a Music Assistant install. All bookdoe logic lives in client.py (tested without MA)."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook, AudioFormat, MediaItemChapter, MediaItemImage, Podcast, PodcastEpisode, ProviderMapping,
    ImageType, UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .client import BookdoeClient, BookPlan, locate

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

CONF_URL = "url"
CONF_TOKEN = "token"
SUPPORTED_FEATURES = {ProviderFeature.LIBRARY_AUDIOBOOKS, ProviderFeature.LIBRARY_PODCASTS, ProviderFeature.SEARCH}
SEP = "|"  # audiobook item_id is "<work>|<asset>"


async def get_config_entries(mass, instance_id=None, action=None, values=None) -> tuple[ConfigEntry, ...]:
    """The two settings: the server address and a device token (`alxd_...`)."""
    return (
        ConfigEntry(key=CONF_URL, type=ConfigEntryType.STRING, label="bookdoe URL",
                    description="Base address of the bookdoe server, e.g. https://books.example", required=True),
        ConfigEntry(key=CONF_TOKEN, type=ConfigEntryType.SECURE_STRING, label="Device token",
                    description="Pair a device in bookdoe (Settings > Devices) and paste its alxd_ token. "
                                "A kid's token only shows that kid's books.", required=True),
    )


class BookdoeProvider(MusicProvider):
    """Audiobooks and podcasts from a bookdoe server."""

    client: BookdoeClient
    _plans: dict[str, BookPlan]
    _skew: dict[str, float]

    @property
    def supported_features(self) -> set[ProviderFeature]:
        return SUPPORTED_FEATURES

    async def handle_async_init(self) -> None:
        self.client = BookdoeClient(str(self.config.get_value(CONF_URL)), str(self.config.get_value(CONF_TOKEN)))
        self._plans = {}
        self._skew = {}
        try:
            await self.client.check()
        except Exception as err:  # bad token, revoked device or unreachable server
            raise LoginFailed(f"bookdoe refused the token or is unreachable: {err}") from err

    async def unload(self, is_removed: bool = False) -> None:
        await self.client.aclose()

    # ---- mapping ----
    def _mapping(self, item_id: str, url: str | None = None) -> ProviderMapping:
        return ProviderMapping(item_id=item_id, provider_domain=self.domain, provider_instance=self.instance_id,
                               audio_format=AudioFormat(content_type=ContentType.UNKNOWN), url=url)

    def _image(self, url: str | None):
        if not url:
            return None
        return UniqueList([MediaItemImage(type=ImageType.THUMB, path=self.client.absolute(url),
                                          provider=self.instance_id, remotely_accessible=False)])

    def _audiobook(self, w: dict, asset: dict) -> Audiobook:
        item_id = f"{w['id']}{SEP}{asset['id']}"
        book = Audiobook(item_id=item_id, provider=self.instance_id, name=w["title"],
                         provider_mappings={self._mapping(item_id)},
                         authors=UniqueList(a["name"] for a in w.get("authors", [])),
                         narrators=UniqueList(n["name"] for n in w.get("narrators", [])),
                         duration=int(asset.get("duration_seconds") or 0))
        book.metadata.description = w.get("description")
        book.metadata.images = self._image(w.get("cover_url"))
        return book

    def _podcast(self, s: dict) -> Podcast:
        p = Podcast(item_id=s["id"], provider=self.instance_id, name=s["title"],
                    provider_mappings={self._mapping(s["id"])})
        p.metadata.images = self._image(s.get("cover_url"))
        return p

    # ---- library ----
    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        async for w, asset in self.client.audiobooks():
            yield self._audiobook(w, asset)

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        work_id = prov_audiobook_id.split(SEP)[0]
        try:
            w, asset = await self.client.audiobook(work_id)
        except Exception as err:
            raise MediaNotFoundError(f"Audiobook {work_id} not found") from err
        book = self._audiobook(w, asset)
        plan = await self.client.plan(work_id, asset["id"])
        book.metadata.chapters = [
            MediaItemChapter(position=i + 1, name=c.get("title") or f"Chapter {i + 1}", start=float(c.get("start") or 0))
            for i, c in enumerate(plan.chapters)] or None
        book.resume_position_ms = int(plan.resume * 1000)
        return book

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        for s in await self.client.podcasts():
            yield self._podcast(s)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        for s in await self.client.podcasts():
            if s["id"] == prov_podcast_id:
                return self._podcast(s)
        raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found")

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode, None]:
        podcast = await self.get_podcast(prov_podcast_id)
        show = await self.client.show(prov_podcast_id)
        n = 0
        for season in show.get("seasons", []):
            for e in season.get("episodes", []):
                if e.get("missing"):
                    continue
                n += 1
                yield PodcastEpisode(item_id=e["id"], provider=self.instance_id, name=e["title"], position=n,
                                     podcast=podcast, duration=int(e.get("duration_seconds") or 0),
                                     provider_mappings={self._mapping(e["id"])})

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        e = await self.client.episode(prov_episode_id)
        podcast = await self.get_podcast(e["show_id"])
        return PodcastEpisode(item_id=e["episode_id"], provider=self.instance_id, name=e["title"], position=0,
                              podcast=podcast, duration=int(e.get("duration_seconds") or 0),
                              provider_mappings={self._mapping(e["episode_id"])})

    async def search(self, search_query: str, media_types: list[MediaType], limit: int = 5):
        from music_assistant_models.media_items import SearchResults
        result = SearchResults()
        q = search_query.lower()
        if MediaType.AUDIOBOOK in media_types:
            async for w, asset in self.client.audiobooks(q=search_query):
                result.audiobooks = [*result.audiobooks, self._audiobook(w, asset)]
                if len(result.audiobooks) >= limit:
                    break
        if MediaType.PODCAST in media_types:
            result.podcasts = [self._podcast(s) for s in await self.client.podcasts() if q in s["title"].lower()][:limit]
        return result

    # ---- playback ----
    async def get_stream_details(self, item_id: str, media_type: MediaType = MediaType.AUDIOBOOK) -> StreamDetails:
        if media_type == MediaType.PODCAST_EPISODE:
            e = await self.client.episode(item_id)
            return StreamDetails(provider=self.instance_id, item_id=item_id, media_type=media_type,
                                 audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                                 stream_type=StreamType.HTTP, path=e["url"], can_seek=True,
                                 duration=int(e.get("duration_seconds") or 0),
                                 extra_input_args=["-headers", f"Authorization: Bearer {self.client.token}\r\n"])
        work_id, asset_id = item_id.split(SEP)
        plan = await self.client.plan(work_id, asset_id)
        self._plans[item_id] = plan
        if len(plan.tracks) == 1:  # one file (an M4B): ffmpeg reads and seeks it itself
            return StreamDetails(provider=self.instance_id, item_id=item_id, media_type=media_type,
                                 audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                                 stream_type=StreamType.HTTP, path=plan.tracks[0].url, can_seek=True,
                                 duration=int(plan.duration),
                                 extra_input_args=["-headers", f"Authorization: Bearer {self.client.token}\r\n"])
        return StreamDetails(provider=self.instance_id, item_id=item_id, media_type=media_type,
                             audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                             stream_type=StreamType.CUSTOM, can_seek=True, duration=int(plan.duration))

    async def get_audio_stream(self, streamdetails: StreamDetails, seek_position: int = 0) -> AsyncGenerator[bytes, None]:
        """Multi-file audiobooks: the tracks one after another, starting at the track holding seek_position."""
        plan = self._plans[streamdetails.item_id]
        track, offset = locate(plan.tracks, float(seek_position))
        # A seek lands at the head of its track (a byte offset into a VBR file would be a guess), but MA
        # counts from seek_position: remember how far ahead of the audio it is, and take it off in on_played.
        self._skew[streamdetails.item_id] = offset
        started = False
        for t in plan.tracks:
            if t is not track and not started:
                continue
            started = True
            async for chunk in self.client.stream(t.url):
                yield chunk

    # ---- progress ----
    async def on_played(self, media_type: MediaType, prov_item_id: str, fully_played: bool, position: int,
                        media_item=None, is_playing: bool = False) -> None:
        """MA reports the position while playing and at the end; bookdoe stores it per account."""
        try:
            if media_type == MediaType.PODCAST_EPISODE:
                await self.client.report_episode(prov_item_id, float(position), force=not is_playing)
            elif media_type == MediaType.AUDIOBOOK:
                plan = self._plans.get(prov_item_id)
                if plan is None:
                    work_id, asset_id = prov_item_id.split(SEP)
                    plan = self._plans[prov_item_id] = await self.client.plan(work_id, asset_id)
                true_position = max(0.0, float(position) - self._skew.get(prov_item_id, 0.0))
                await self.client.report_book(plan, true_position, force=not is_playing)
        except Exception as err:  # never break playback over a failed save
            self.logger.warning("bookdoe progress not saved: %s", err)
