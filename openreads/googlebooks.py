"""Google Books API client -- supplemental enrichment only.

Open Library is the source of truth for the author/work/edition graph
(it has the structure Readarr expects); Google Books fills in gaps
(description, cover, page count, publisher) when OL's edition-level data
is thin. Never used as the primary/only source for a given edition.
"""
import os
import time
import threading
import httpx

API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")
BASE = "https://www.googleapis.com/books/v1"

_client = httpx.Client(base_url=BASE, timeout=3.0)

# Simple circuit breaker: a prolific author (500+ works) can trigger
# hundreds of enrichment calls in a burst, and Google Books rate-limits
# hard (429/503) well before that. Once we see a rate-limit response,
# stop calling out entirely for a cooldown window instead of hammering
# it (and paying a 6s timeout) on every subsequent edition.
_backoff_until = 0.0
_backoff_lock = threading.Lock()
_BACKOFF_SECONDS = 60


def _in_backoff():
    return time.time() < _backoff_until


def _note_rate_limited():
    global _backoff_until
    with _backoff_lock:
        _backoff_until = time.time() + _BACKOFF_SECONDS


def _enabled():
    return bool(API_KEY) and not _in_backoff()


def enabled():
    """True when a key is configured and the breaker is closed (public for resources)."""
    return _enabled()


def in_backoff():
    """True while the rate-limit breaker is open: a None answer then means 'did not ask'."""
    return _in_backoff()


def find_by_isbn(isbn: str):
    if not _enabled() or not isbn:
        return None
    try:
        r = _client.get("/volumes", params={"q": f"isbn:{isbn}", "key": API_KEY})
        if r.status_code in (429, 503):
            _note_rate_limited()
            return None
        r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            return None
        return _volume_info(items[0])
    except httpx.HTTPError:
        # Any failure -- timeout, connection reset, DNS hiccup -- gets the
        # same treatment as an explicit rate-limit. A silent timeout on
        # every single edition of a 100+ work author is what actually
        # broke search: it never showed up as a 429/503, just ~3s of dead
        # air per edition, which added up to far more than Readarr's own
        # client timeout.
        _note_rate_limited()
        return None


def find_by_title_author(title: str, author: str = None):
    if not _enabled() or not title:
        return None
    q = f"intitle:{title}"
    if author:
        q += f"+inauthor:{author}"
    try:
        r = _client.get("/volumes", params={"q": q, "key": API_KEY, "maxResults": 1})
        if r.status_code in (429, 503):
            _note_rate_limited()
            return None
        r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            return None
        return _volume_info(items[0])
    except httpx.HTTPError:
        _note_rate_limited()
        return None


def _series_info(info):
    """Map volumeInfo.seriesInfo -> [{'series_id', 'position'}].

    The Books API exposes an opaque seriesId (no series name) plus
    orderNumber per series, and bookDisplayNumber for the volume. Position
    is bookDisplayNumber when present, else that series' orderNumber.
    """
    si = info.get("seriesInfo") or {}
    display = str(si.get("bookDisplayNumber") or "").strip() or None
    out = []
    for vs in si.get("volumeSeries") or []:
        order = vs.get("orderNumber")
        out.append(
            {
                "series_id": vs.get("seriesId"),
                "position": display or (str(order) if order not in (None, "") else None),
            }
        )
    if not out and display:
        out.append({"series_id": None, "position": display})
    return out


def _volume_info(item):
    info = item.get("volumeInfo", {})
    image_links = info.get("imageLinks", {})
    image_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
    if image_url:
        image_url = image_url.replace("http://", "https://").replace("&edge=curl", "")
    isbn13 = isbn10 = None
    for ident in info.get("industryIdentifiers", []):
        if ident.get("type") == "ISBN_13":
            isbn13 = ident.get("identifier")
        elif ident.get("type") == "ISBN_10":
            isbn10 = ident.get("identifier")
    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "publisher": info.get("publisher"),
        "release_date": info.get("publishedDate"),
        "num_pages": info.get("pageCount"),
        "image_url": image_url,
        "isbn13": isbn13,
        "isbn10": isbn10,
        "series": _series_info(info),
        # Audience signals (kids classifier). Google's categories look like
        # "Juvenile Fiction / Animals / General"; maturityRating is MATURE or NOT_MATURE.
        "categories": [c for c in (info.get("categories") or []) if isinstance(c, str) and c.strip()],
        "maturity_rating": info.get("maturityRating"),
        "language": info.get("language"),
        "volume_id": item.get("id"),
    }
