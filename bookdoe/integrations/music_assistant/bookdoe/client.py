"""bookdoe Player API client for the Music Assistant provider. No Music Assistant imports,
so it is tested on its own (tests/test_music_assistant_provider.py)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

API = "/api/player/v1"
PAGE = 200
# Same budget the server enforces per account and book (docs/PLAYER_API.md, "Offline reconciliation").
MIN_REPORT_GAP = 5.0


@dataclass
class Track:
    id: str
    url: str
    duration: float


@dataclass
class BookPlan:
    work_id: str
    asset_id: str
    title: str
    tracks: list[Track]
    chapters: list[dict[str, Any]] = field(default_factory=list)
    resume: float = 0.0

    @property
    def duration(self) -> float:
        return sum(t.duration for t in self.tracks)


def locate(tracks: list[Track], elapsed: float) -> tuple[Track, float]:
    """The track holding `elapsed` seconds from the start of the book and the offset inside it."""
    elapsed = max(0.0, elapsed)
    for t in tracks:
        if elapsed < t.duration or t is tracks[-1]:
            return t, min(elapsed, t.duration) if t.duration else elapsed
        elapsed -= t.duration
    raise ValueError("no tracks")


def elapsed_of(tracks: list[Track], file_id: str | None, position: float | None) -> float:
    """Seconds from the start of the book for a stored (track, position); 0 for an unknown track."""
    total = 0.0
    for t in tracks:
        if t.id == file_id:
            return total + (position or 0.0)
        total += t.duration
    return 0.0


class BookdoeClient:
    """Talks to /api/player/v1 with a device token (`alxd_...`). The token carries the owner's
    account type, so a kid's token only ever lists what that kid may see."""

    def __init__(self, base_url: str, token: str, *, http: httpx.AsyncClient | None = None, device_id: str | None = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.device_id = device_id
        self._http = http or httpx.AsyncClient(timeout=30)
        self._last_report: dict[str, float] = {}

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def absolute(self, url: str) -> str:
        return url if url.startswith("http") else self.base + url

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._http.request(method, self.base + API + path, headers=self.headers, **kw)
        r.raise_for_status()
        return r.json()

    async def _pages(self, path: str, params: dict[str, Any] | None = None):
        offset = 0
        while True:
            page = await self._json("GET", path, params={**(params or {}), "limit": PAGE, "offset": offset})
            for item in page["items"]:
                yield item
            offset += PAGE
            if offset >= page["total"] or not page["items"]:
                return

    async def check(self) -> dict[str, Any]:
        """Who the token belongs to (raises on a bad or revoked token)."""
        return await self._json("GET", "/me")

    async def audiobooks(self, q: str | None = None):
        params: dict[str, Any] = {"kind": "audiobook"}
        if q:
            params["q"] = q
        async for w in self._pages("/library", params):
            asset = pick_audio_asset(w)
            if asset:
                yield w, asset

    async def audiobook(self, work_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        w = await self._json("GET", f"/library/{work_id}")
        asset = pick_audio_asset(w)
        if not asset:
            raise LookupError("no playable audio")
        return w, asset

    async def plan(self, work_id: str, asset_id: str) -> BookPlan:
        m = await self._json("GET", f"/audio/{work_id}/{asset_id}")
        tracks = [Track(f["id"], self.absolute(f["url"]), float(f.get("duration_seconds") or 0)) for f in m["files"]]
        prog = m.get("progress") or {}
        resume = elapsed_of(tracks, prog.get("file_id"), prog.get("position"))
        return BookPlan(work_id, asset_id, m["title"], tracks, m.get("chapters") or [], resume)

    async def report_book(self, plan: BookPlan, elapsed: float, *, force: bool = False) -> bool:
        """Save the position. Throttled so a ticking player does not hit the server's write budget."""
        now = time.time()
        if not force and now - self._last_report.get(plan.asset_id, 0) < MIN_REPORT_GAP:
            return False
        track, offset = locate(plan.tracks, elapsed)
        body: dict[str, Any] = {"asset_id": plan.asset_id, "file_id": track.id, "position": offset,
                                "duration": track.duration or None, "client_updated_at": now}
        if plan.duration:
            body["percent"] = max(0.0, min(1.0, elapsed / plan.duration))
        if self.device_id:
            body["device_id"] = self.device_id
        res = await self._json("PUT", f"/progress/{plan.asset_id}", json=body)
        self._last_report[plan.asset_id] = now
        return bool(res.get("applied", True))

    async def podcasts(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/podcasts")
        return data if isinstance(data, list) else data.get("items") or data.get("shows") or []

    async def show(self, show_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/podcasts/shows/{show_id}")

    async def episode(self, episode_id: str) -> dict[str, Any]:
        """Start info: `url` made absolute, `position_seconds` is the resume point (0 once played)."""
        e = await self._json("GET", f"/podcasts/episodes/{episode_id}")
        e["url"] = self.absolute(e["url"])
        return e

    async def report_episode(self, episode_id: str, elapsed: float, *, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self._last_report.get(episode_id, 0) < MIN_REPORT_GAP:
            return False
        await self._json("POST", f"/podcasts/episodes/{episode_id}/progress",
                         json={"position_seconds": max(0.0, elapsed), "client_updated_at": now})
        self._last_report[episode_id] = now
        return True

    async def stream(self, url: str, start: int = 0):
        """Bytes of a media URL with the token; `start` is a byte offset (Range)."""
        headers = dict(self.headers)
        if start:
            headers["Range"] = f"bytes={start}-"
        async with self._http.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes(64 * 1024):
                yield chunk

    async def aclose(self) -> None:
        await self._http.aclose()


def pick_audio_asset(work: dict[str, Any]) -> dict[str, Any] | None:
    """The asset to play: the readable audiobook asset, the server's preferred one when it names one."""
    assets = [a for a in work.get("assets", []) if a.get("kind") == "audiobook" and a.get("readable")]
    preferred = (work.get("preferred") or {}).get("audiobook")
    for a in assets:
        if a["id"] == preferred:
            return a
    return assets[0] if assets else None
