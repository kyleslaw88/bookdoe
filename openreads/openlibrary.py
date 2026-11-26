"""Open Library API client.

Open Library (openlibrary.org) is the Internet Archive's free, open
bibliographic database. No API key required. This is our primary source
for the author -> work -> edition graph, mirroring the shape Readarr
expects from its old Goodreads-backed metadata source.

Docs: https://openlibrary.org/developers/api
"""
import re
from functools import lru_cache

import httpx

import seriesnorm

BASE = "https://openlibrary.org"
COVERS = "https://covers.openlibrary.org"
UA = "openreads-metadata-bridge/1.0 (self-hosted; contact: n/a)"

_transport = httpx.HTTPTransport(retries=3)
_client = httpx.Client(
    base_url=BASE, headers={"User-Agent": UA}, timeout=20.0, transport=_transport
)


def _strip_key(key: str) -> str:
    """'/authors/OL123A' -> 'OL123A'"""
    return key.rstrip("/").split("/")[-1]


def _text(value):
    """OL sometimes returns {'value': '...'} instead of a plain string for
    long text fields (bios, descriptions)."""
    if isinstance(value, dict):
        return value.get("value", "")
    return value or ""


def search(query: str, limit: int = 20):
    """Returns raw OL search docs."""
    r = _client.get(
        "/search.json",
        params={
            "q": query,
            "limit": limit,
            "fields": "key,title,author_key,author_name,first_publish_year,"
            "edition_key,cover_edition_key,isbn,cover_i",
        },
    )
    r.raise_for_status()
    return r.json().get("docs", [])


def get_author(ol_key: str):
    r = _client.get(f"/authors/{ol_key}.json")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    photo_url = None
    if data.get("photos") and data["photos"][0] > 0:
        photo_url = f"{COVERS}/a/id/{data['photos'][0]}-L.jpg"
    else:
        photo_url = f"{COVERS}/a/olid/{ol_key}-L.jpg"
    return {
        "name": data.get("name") or data.get("personal_name") or ol_key,
        "description": _text(data.get("bio")),
        "image_url": photo_url,
    }


def get_author_works(ol_key: str, limit: int = 200):
    """List of {key, title} for a given author, newest metadata first."""
    r = _client.get(f"/authors/{ol_key}/works.json", params={"limit": limit})
    r.raise_for_status()
    out = []
    for entry in r.json().get("entries", []):
        out.append(
            {
                "ol_key": _strip_key(entry["key"]),
                "title": entry.get("title") or "Unknown",
            }
        )
    return out


def parse_series_string(raw):
    """Split a free-text OL series string into (name, position).

    Returns (name, None) when no position can be cleanly extracted, and
    (None, None) for empty input. A bare number is never treated as a
    name, and a name is never reduced to nothing. See
    seriesnorm.split_position for the recognised formats.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    return seriesnorm.split_position(raw)


@lru_cache(maxsize=1024)
def _series_name(series_key: str):
    """Resolve '/series/OL335559L' to its display name (None on failure)."""
    try:
        r = _client.get(f"/series/{_strip_key(series_key)}.json")
        if r.status_code != 200:
            return None
        return (r.json().get("name") or "").strip() or None
    except httpx.HTTPError:
        return None


def _series_from_raw(raw):
    """Normalize any OL 'series' value into [{'name', 'position'}].

    Handles a bare string, a list of strings (free text, maybe with a
    position), and the structured work-level form
    [{'series': {'key': '/series/OL..L'}, 'position': '6'}].
    """
    if not raw:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    out = []
    for item in raw:
        name = position = None
        if isinstance(item, str):
            name, position = parse_series_string(item)
        elif isinstance(item, dict):
            position = str(item["position"]).strip() if item.get("position") not in (None, "") else None
            ref = item.get("series")
            if isinstance(ref, dict) and ref.get("key"):
                name = _series_name(ref["key"])
            elif isinstance(ref, str):
                name, parsed = parse_series_string(ref)
                position = position or parsed
            elif item.get("name"):
                name, parsed = parse_series_string(item["name"])
                position = position or parsed
        if name and not seriesnorm.is_imprint(name):
            out.append({"name": name, "position": position or None})
    return out


def _series_from_editions(ol_key: str, limit: int = 50):
    out = []
    r = _client.get(f"/works/{ol_key}/editions.json", params={"limit": limit})
    r.raise_for_status()
    for e in r.json().get("entries", []):
        out.extend(_series_from_raw(e.get("series")))
    return out


def _merge_series(primary, extra):
    """Combine series lists by case-insensitive name. Keeps primary's
    ordering/name; fills a missing position from any entry that has one,
    preferring the most common position among edition-level votes. New
    names from `extra` are appended only when primary is empty (edition
    strings for a work with a known series are usually just variants of
    it)."""
    key = seriesnorm.norm_key

    votes = {}
    display = {}
    for s in extra:
        k = key(s["name"])
        display.setdefault(k, s["name"])
        if s["position"]:
            votes.setdefault(k, []).append(s["position"])
        else:
            votes.setdefault(k, [])

    def best(k):
        v = votes.get(k) or []
        return max(set(v), key=v.count) if v else None

    if primary:
        return [
            {"name": s["name"], "position": s["position"] or best(key(s["name"]))}
            for s in primary
        ]
    # Edition-only: prefer the series name with the most evidence.
    order = sorted(votes, key=lambda k: -sum(1 for s in extra if key(s["name"]) == k))
    return [{"name": display[k], "position": best(k)} for k in order[:1]]


def get_author_work_languages(ol_key: str, limit: int = 200):
    """{work_key: [lang codes]} for an author's works, in one search call.

    Open Library's search index rolls each work's edition languages up into
    a `language` list (ISO 639-2, e.g. ['eng', 'fre']). Works with no
    language data on any edition are absent from the list / have no field.
    Returns None if the lookup fails, so callers can treat everything as
    unknown rather than dropping works."""
    try:
        r = _client.get(
            "/search.json",
            params={"author_key": ol_key, "fields": "key,language", "limit": limit},
        )
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return {
        _strip_key(d["key"]): list(d.get("language") or [])
        for d in r.json().get("docs", [])
    }


def _string_list(value, limit=60):
    """A list of non-empty strings from an OL subject field (anything else -> [])."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip()[:200])
    return out[:limit]


def get_work_subjects(ol_key: str):
    """{'subjects': [...], 'subject_places': [...]} from the work record (one call), or
    None when Open Library has no such work."""
    r = _client.get(f"/works/{ol_key}.json")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    return {"subjects": _string_list(data.get("subjects")), "subject_places": _string_list(data.get("subject_places"))}


def get_work(ol_key: str):
    r = _client.get(f"/works/{ol_key}.json")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    author_keys = [
        _strip_key(a["author"]["key"])
        for a in data.get("authors", [])
        if "author" in a and "key" in a["author"]
    ]
    series = _series_from_raw(data.get("series"))
    if any(s["position"] is None for s in series) or not series:
        # Work records rarely carry series info (or carry it without a
        # position); edition records often do, as free text.
        try:
            edition_series = _series_from_editions(ol_key)
        except httpx.HTTPError:
            edition_series = []
        series = _merge_series(series, edition_series)
    cover_url = None
    if data.get("covers") and data["covers"][0] > 0:
        cover_url = f"{COVERS}/b/id/{data['covers'][0]}-L.jpg"
    return {
        "title": data.get("title") or "Unknown",
        "description": _text(data.get("description")),
        "author_keys": author_keys,
        "series": series,
        "cover_url": cover_url,
        "first_publish_date": data.get("first_publish_date"),
        # Already in this response, so recording them costs no extra call.
        "subjects": _string_list(data.get("subjects")),
        "subject_places": _string_list(data.get("subject_places")),
    }


def get_work_editions(ol_key: str, limit: int = 50):
    r = _client.get(f"/works/{ol_key}/editions.json", params={"limit": limit})
    r.raise_for_status()
    out = []
    for e in r.json().get("entries", []):
        isbn13 = (e.get("isbn_13") or [None])[0]
        isbn10 = (e.get("isbn_10") or [None])[0]
        cover_url = None
        if e.get("covers") and e["covers"][0] > 0:
            cover_url = f"{COVERS}/b/id/{e['covers'][0]}-L.jpg"
        out.append(
            {
                "ol_key": _strip_key(e["key"]),
                "title": e.get("title") or "Unknown",
                "isbn13": isbn13,
                "isbn10": isbn10,
                "publisher": (e.get("publishers") or [None])[0],
                "release_date": e.get("publish_date"),
                "num_pages": e.get("number_of_pages"),
                "image_url": cover_url,
                "physical_format": e.get("physical_format"),
            }
        )
    return out


def find_edition_by_isbn(isbn: str):
    r = _client.get(f"/isbn/{isbn}.json")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    works = data.get("works") or []
    work_key = _strip_key(works[0]["key"]) if works else None
    cover_url = None
    if data.get("covers") and data["covers"][0] > 0:
        cover_url = f"{COVERS}/b/id/{data['covers'][0]}-L.jpg"
    return {
        "ol_key": _strip_key(data["key"]),
        "work_ol_key": work_key,
        "title": data.get("title") or "Unknown",
        "isbn13": (data.get("isbn_13") or [None])[0],
        "isbn10": (data.get("isbn_10") or [None])[0],
        "publisher": (data.get("publishers") or [None])[0],
        "release_date": data.get("publish_date"),
        "num_pages": data.get("number_of_pages"),
        "image_url": cover_url,
    }
