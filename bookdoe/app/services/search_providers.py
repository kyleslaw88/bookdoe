from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings
from . import provider_results as pr
from .search import search_local
from .search_normalize import normalize_query

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
UA = "bookdoe/0.1 (federated search)"

# Spaced out so a federated call never looks like a burst to any single upstream
# (mirrors the spirit of openreads/audiobookdb.py's own throttling, at a much
# smaller scale here since each provider is only hit once per search).
INTER_PROVIDER_DELAY_SECONDS = 0.05


def _empty_result(provider: str) -> dict[str, Any]:
    return {
        "title": None,
        "subtitle": None,
        "authors": [],
        "series": [],
        "identifiers": [],
        "publication_date": None,
        "cover_url": None,
        "provider": provider,
        "match_score": 0.0,
    }


def _result(provider: str, **fields: Any) -> dict[str, Any]:
    r = _empty_result(provider)
    r.update({k: v for k, v in fields.items() if v is not None})
    return r


class SearchProvider(ABC):
    """A single federated search source.

    search() may raise for upstream failure (timeout, non-2xx, malformed body);
    fetch() classifies that into a ProviderResult (success / not found /
    transient / rate limited / permanent) so one provider's outage cannot take
    the whole search down, and a throttled provider is never mistaken for one
    with no matches.
    """

    name: str = ""
    # Remote providers get persisted retry state; the local catalog has no upstream to back off from.
    remote: bool = True

    def fetch(self, query: str, limit: int) -> pr.ProviderResult:
        try:
            results = self.search(query, limit)
        except Exception as exc:
            result = pr.classify_exception(exc, self.name)
            log.warning("federated search: provider %r %s: %s", self.name, result.status, result.error)
            return result
        return pr.ProviderResult(pr.SUCCESS if results else pr.NOT_FOUND, self.name, data=results)

    @abstractmethod
    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Return results in the common format, best match first."""

    def enabled(self) -> bool:
        return True


class OpenLibraryProvider(SearchProvider):
    name = "openlibrary"
    BASE = "https://openlibrary.org"

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        with httpx.Client(base_url=self.BASE, headers={"User-Agent": UA}, timeout=DEFAULT_TIMEOUT) as c:
            r = c.get("/search.json", params={"q": query, "limit": limit})
            r.raise_for_status()
            docs = r.json().get("docs", [])
        return [self._map(d) for d in docs]

    def _map(self, doc: dict[str, Any]) -> dict[str, Any]:
        cover_url = None
        if doc.get("cover_i"):
            cover_url = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"
        identifiers = [{"namespace": "isbn", "value": v} for v in (doc.get("isbn") or [])]
        if doc.get("key"):
            identifiers.append({"namespace": "openlibrary", "value": doc["key"].rstrip("/").split("/")[-1]})
        return _result(
            self.name,
            title=doc.get("title"),
            authors=list(doc.get("author_name") or []),
            series=list(doc.get("series") or []) if isinstance(doc.get("series"), list) else [],
            identifiers=identifiers,
            publication_date=str(doc["first_publish_year"]) if doc.get("first_publish_year") else None,
            cover_url=cover_url,
            match_score=0.5,
        )


class HardcoverProvider(SearchProvider):
    name = "hardcover"
    BASE = "https://hardcover.app"

    def enabled(self) -> bool:
        log.warning("federated search: hardcover provider disabled -- hardcover.app blocks requests with 403 (Cloudflare)")
        return False

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        with httpx.Client(base_url=self.BASE, headers={"User-Agent": UA}, timeout=DEFAULT_TIMEOUT) as c:
            r = c.get("/api/v1/search", params={"q": query, "limit": limit})
            r.raise_for_status()
            data = r.json()
        items = data.get("results") or data.get("data") or []
        return [self._map(item) for item in items]

    def _map(self, item: dict[str, Any]) -> dict[str, Any]:
        identifiers = []
        if item.get("isbn"):
            identifiers.append({"namespace": "isbn", "value": item["isbn"]})
        if item.get("id"):
            identifiers.append({"namespace": "hardcover", "value": str(item["id"])})
        authors = item.get("authors") or item.get("contributions") or []
        if authors and isinstance(authors[0], dict):
            authors = [a.get("name") for a in authors if a.get("name")]
        return _result(
            self.name,
            title=item.get("title"),
            subtitle=item.get("subtitle"),
            authors=list(authors),
            series=[item["series"]] if item.get("series") else [],
            identifiers=identifiers,
            publication_date=item.get("release_date") or item.get("publication_date"),
            cover_url=item.get("cover_url") or item.get("image_url"),
            match_score=0.5,
        )


class AudiobookDBProvider(SearchProvider):
    name = "audiobookdb"
    BASE = "https://api.audiobookdb.com"

    def enabled(self) -> bool:
        log.warning("federated search: audiobookdb provider disabled -- api.audiobookdb.com is unreachable (404)")
        return False

    def _api_key(self) -> str:
        return settings.audiobookdb_api_key

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        headers = {"User-Agent": UA}
        key = self._api_key()
        if key:
            headers["X-API-Key"] = key
        with httpx.Client(base_url=self.BASE, headers=headers, timeout=DEFAULT_TIMEOUT) as c:
            r = c.get("/api/v1/audiobooks", params={"filter[search]": query, "page[limit]": limit})
            r.raise_for_status()
            data = r.json()
        items = data.get("data") or []
        return [self._map(item) for item in items][:limit]

    def _map(self, item: dict[str, Any]) -> dict[str, Any]:
        attrs = item.get("attributes") or item
        identifiers = []
        if attrs.get("isbn"):
            identifiers.append({"namespace": "isbn", "value": attrs["isbn"]})
        if attrs.get("asin"):
            identifiers.append({"namespace": "asin", "value": attrs["asin"]})
        if item.get("id"):
            identifiers.append({"namespace": "audiobookdb", "value": str(item["id"])})
        authors = attrs.get("authors") or []
        if authors and isinstance(authors[0], dict):
            authors = [a.get("name") for a in authors if a.get("name")]
        series = attrs.get("series") or []
        if series and isinstance(series[0], dict):
            series = [s.get("name") for s in series if s.get("name")]
        return _result(
            self.name,
            title=attrs.get("title"),
            authors=list(authors),
            series=list(series),
            identifiers=identifiers,
            publication_date=attrs.get("release_date") or attrs.get("publication_date"),
            cover_url=attrs.get("cover_url") or attrs.get("image_url"),
            match_score=0.5,
        )


class GoogleBooksProvider(SearchProvider):
    name = "googlebooks"
    BASE = "https://www.googleapis.com"

    def _api_key(self) -> str:
        return settings.google_books_api_key

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "maxResults": min(limit, 40)}
        key = self._api_key()
        if key:
            params["key"] = key
        with httpx.Client(base_url=self.BASE, headers={"User-Agent": UA}, timeout=DEFAULT_TIMEOUT) as c:
            r = c.get("/books/v1/volumes", params=params)
            r.raise_for_status()
            data = r.json()
        items = data.get("items") or []
        return [self._map(item) for item in items]

    def _map(self, item: dict[str, Any]) -> dict[str, Any]:
        info = item.get("volumeInfo") or {}
        identifiers = []
        for ident in info.get("industryIdentifiers") or []:
            if ident.get("type") == "ISBN_13":
                identifiers.append({"namespace": "isbn", "value": ident["identifier"]})
            elif ident.get("type") == "ISBN_10":
                identifiers.append({"namespace": "isbn10", "value": ident["identifier"]})
        if item.get("id"):
            identifiers.append({"namespace": "googlebooks", "value": item["id"]})
        image_links = info.get("imageLinks") or {}
        cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
        if cover_url:
            cover_url = cover_url.replace("http://", "https://")
        series_info = info.get("seriesInfo") or {}
        series = [series_info["seriesId"]] if series_info.get("seriesId") else []
        return _result(
            self.name,
            title=info.get("title"),
            subtitle=info.get("subtitle"),
            authors=list(info.get("authors") or []),
            series=series,
            identifiers=identifiers,
            publication_date=info.get("publishedDate"),
            cover_url=cover_url,
            match_score=0.5,
        )


class LocalMetadataProvider(SearchProvider):
    """Searches bookdoe's own catalog -- the "local first" leg of federation."""

    name = "local"
    remote = False

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        local = search_local(query, limit=limit)
        return [self._map(r) for r in local["results"]]

    def _map(self, r: dict[str, Any]) -> dict[str, Any]:
        return _result(
            self.name,
            title=r.get("title"),
            authors=list(r.get("authors") or []),
            series=list(r.get("series") or []),
            identifiers=list(r.get("identifiers") or []),
            match_score=1.0,
        )


_PROVIDERS: dict[str, SearchProvider] = {
    "openlibrary": OpenLibraryProvider(),
    "hardcover": HardcoverProvider(),
    "audiobookdb": AudiobookDBProvider(),
    "googlebooks": GoogleBooksProvider(),
    "local": LocalMetadataProvider(),
}

DEFAULT_PROVIDERS = ["local", "openlibrary", "googlebooks"]


def get_provider(name: str) -> SearchProvider:
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown search provider: {name!r}") from None


SEARCH_ENTITY = "search"


@dataclass
class FederatedSearch:
    results: list[dict[str, Any]]
    # Per-provider outcome (data stripped); a rate-limited provider shows up here with its
    # retry time instead of silently looking like "no matches".
    outcomes: dict[str, pr.ProviderResult] = field(default_factory=dict)


def federated_search(
    query: str,
    limit: int = 20,
    providers: list[str] | None = None,
) -> FederatedSearch:
    """Query multiple providers concurrently and merge into one ranked list.

    Local-first: the local catalog is always queried, and its results are never
    dropped by an unrelated remote outage. Each remote provider's outcome is
    classified and persisted (provider_retry_state, keyed by provider and query): a
    provider still waiting out a retry is not asked again (deferred), and a failure
    never degrades into "not found" -- the search just proceeds with whichever
    providers answered.
    """
    q = normalize_query(query)
    if not q:
        return FederatedSearch([])

    names = list(dict.fromkeys(providers if providers is not None else DEFAULT_PROVIDERS))
    active = [get_provider(n) for n in names if get_provider(n).enabled()]
    if not active:
        return FederatedSearch([])

    outcomes: dict[str, pr.ProviderResult] = {}
    runnable: list[SearchProvider] = []
    for provider in active:
        waiting = pr.deferred(provider.name, SEARCH_ENTITY, q) if provider.remote else None
        if waiting:
            outcomes[provider.name] = waiting
        else:
            runnable.append(provider)

    fetched: dict[str, pr.ProviderResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(runnable))) as pool:
        futures = {}
        for i, provider in enumerate(runnable):
            # Small stagger so concurrent providers don't all fire in the same
            # instant -- cheap courtesy to upstreams, not a real rate limiter.
            if i > 0:
                time.sleep(INTER_PROVIDER_DELAY_SECONDS)
            futures[pool.submit(provider.fetch, q, limit)] = provider
        for future in as_completed(futures):
            fetched[futures[future].name] = future.result()

    results: list[dict[str, Any]] = []
    for provider in runnable:
        result = fetched[provider.name]
        if provider.remote:
            pr.record(provider.name, SEARCH_ENTITY, q, result)
        results.extend(result.data or [])
        outcomes[provider.name] = pr.ProviderResult(
            result.status, result.provider, error=result.error, retry_after=result.retry_after, retry_at=result.retry_at)

    results.sort(key=lambda r: (-(r.get("match_score") or 0.0), (r.get("title") or "").casefold()))
    return FederatedSearch(results[:limit], outcomes)


def search_federated(
    query: str,
    limit: int = 20,
    providers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """federated_search() without the per-provider outcomes."""
    return federated_search(query, limit, providers).results
