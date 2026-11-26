"""AudiobookDB client -- per-book series enrichment (audiobook-side data).

AudiobookDB (audiobookdb.org) is a community audiobook catalog with a
documented, free API. It is NOT a catalog replacement here: openreads still
builds the author -> work -> edition graph from Open Library. This adapter
answers one question for a book openreads already knows -- "what series is
this, and at what position?" -- by searching title + author.

Field priority (a judgment call, not a stated fact -- change it here):
  * series name/position: AudiobookDB wins. Audible-side data is curated per
    audiobook and had series for books Open Library had none for (Wilkie
    Collins: 5 of 6 previously series-less titles). Where both sources give a
    position for the same series, AudiobookDB's replaces Open Library's.
    Series *names* still go through seriesnorm, so "Molly Murphy Mysteries"
    and Open Library's "Molly Murphy" land on the same series row.
  * everything else (title, authors, editions, description, cover, ...):
    Open Library / Google Books keep ownership.
Series present only in Open Library are kept (not deleted) when AudiobookDB
knows the book under a different series.

Rate limits: anonymous tier is 5 req/s and a small daily budget (a free API
key raises it 20x). We (1) space requests >= MIN_INTERVAL apart, (2) keep a
persistent per-UTC-day request counter capped at DAILY_CAP, (3) stop for the
day when the server's own x-ratelimit-budget header runs low, (4) back off
after 429/5xx/timeouts, and (5) cache every answer -- including "not found"
-- in SQLite (db.enrichment_*), so a book is looked up at most once per TTL.
"""
import difflib
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import httpx

import db
import seriesnorm

log = logging.getLogger("openreads")

SOURCE = "audiobookdb"
BASE = os.environ.get("AUDIOBOOKDB_BASE", "https://audiobookdb.org/api")
API_KEY = os.environ.get("AUDIOBOOKDB_API_KEY", "")
ENABLED = os.environ.get("AUDIOBOOKDB_ENABLED", "1").lower() not in ("0", "false", "no", "off")
UA = "openreads-metadata-bridge/1.0 (self-hosted; series enrichment)"

# Anonymous tier: 5 req/s. Stay under it, and under the daily budget with
# headroom for manual use. With an API key (much larger budget) raise the
# cap via AUDIOBOOKDB_DAILY_CAP.
MIN_INTERVAL = float(os.environ.get("AUDIOBOOKDB_MIN_INTERVAL", "0.25"))
DAILY_CAP = int(os.environ.get("AUDIOBOOKDB_DAILY_CAP", "9000" if API_KEY else "400"))
BUDGET_RESERVE = 20          # stop when the server says fewer than this remain
BACKOFF_SECONDS = 60
FOUND_TTL = 30 * 24 * 3600   # a resolved series rarely changes
MISSING_TTL = 7 * 24 * 3600  # "not on AudiobookDB yet" is rechecked weekly

# Shared lookup allowance for EVERY path that can hit the network (author
# builds and individual /work/{id} refreshes alike): a token bucket that
# starts full at BURST and refills one token per REFILL_SECONDS. Anything
# that finds it empty is served from cache only and retried on a later
# request, exactly like an author build past its per-build cap. This is what
# stops a burst of per-book Readarr refreshes from being an uncapped side
# door; spacing (MIN_INTERVAL) and the persistent daily counter apply below
# it, in _post_search. In-memory, i.e. per process.
BURST = int(os.environ.get("AUDIOBOOKDB_BURST", "25"))
REFILL_SECONDS = float(os.environ.get("AUDIOBOOKDB_REFILL_SECONDS", "10"))
# How long a coalesced duplicate request waits for the in-flight one.
FLIGHT_WAIT_SECONDS = float(os.environ.get("AUDIOBOOKDB_FLIGHT_WAIT", "30"))

_headers = {"User-Agent": UA}
if API_KEY:
    _headers["X-API-Key"] = API_KEY
_client = httpx.Client(base_url=BASE, headers=_headers, timeout=6.0)

_lock = threading.Lock()
_next_call_at = 0.0
_backoff_until = 0.0
_exhausted_day = None

class TokenBucket:
    def __init__(self, capacity, refill_seconds, clock=time.time):
        self.capacity = capacity
        self.refill_seconds = max(refill_seconds, 0.001)
        self._clock = clock
        self._tokens = float(capacity)
        self._stamp = clock()
        self._lock = threading.Lock()

    def try_take(self) -> bool:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._stamp) / self.refill_seconds)
            self._stamp = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False


_bucket = TokenBucket(BURST, REFILL_SECONDS)


class _Flight:
    """One in-flight lookup that duplicate requests wait on."""
    def __init__(self):
        self.done = threading.Event()
        self.result = None


_inflight = {}
_inflight_lock = threading.Lock()

# Titles that are bundles, not a book with a series position.
_BUNDLE_RE = re.compile(r"\b(box(?:ed)?\s*set|collection\s*set|books?\s+collection|bundle|\d+\s+books?)\b", re.I)


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _may_call() -> bool:
    if not ENABLED:
        return False
    if time.time() < _backoff_until or _exhausted_day == _today():
        return False
    return db.usage_count(SOURCE, _today()) < DAILY_CAP


def _throttle():
    """Serialize callers so requests are >= MIN_INTERVAL apart."""
    global _next_call_at
    with _lock:
        wait = _next_call_at - time.time()
        _next_call_at = max(time.time(), _next_call_at) + MIN_INTERVAL
    if wait > 0:
        time.sleep(wait)


def _note_headers(resp):
    global _exhausted_day
    try:
        budget = int(resp.headers.get("x-ratelimit-budget", ""))
    except ValueError:
        return
    if budget <= BUDGET_RESERVE:
        _exhausted_day = _today()
        log.warning("audiobookdb daily budget nearly spent (%s left); pausing until tomorrow", budget)


def _backoff(seconds=BACKOFF_SECONDS):
    global _backoff_until
    _backoff_until = time.time() + seconds


def _post_search(query: str):
    """Returns a list of book dicts, or None on any failure/limit."""
    if not _may_call():
        return None
    _throttle()
    db.usage_incr(SOURCE, _today())
    try:
        r = _client.post("/search", json={"q": query, "type": "books"})
    except httpx.HTTPError:
        _backoff()
        return None
    _note_headers(r)
    if r.status_code == 429:
        try:
            _backoff(min(int(r.headers.get("retry-after", BACKOFF_SECONDS)), 3600))
        except ValueError:
            _backoff()
        return None
    if r.status_code >= 500:
        _backoff()
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, list) else None


# ---- matching ---------------------------------------------------------------

def _person_key(name: str) -> list:
    return re.sub(r"[^\w\s]", " ", (name or "").lower()).split()


def same_person(a: str, b: str) -> bool:
    """Same author name, tolerant of middle initials and punctuation but not
    of different people ("Wilkie Collins" != "Rachel Collins")."""
    ta, tb = _person_key(a), _person_key(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(short) >= 2 and short[0] == long_[0] and short[-1] == long_[-1]


def core_title(title: str) -> str:
    """Comparison form of a title: drops "(A Series Mystery)" parentheticals,
    a ": subtitle" and trailing "Book N"-style tags, then title_key."""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "")
    t = re.sub(r"\s*[:—–-]\s+.*$", "", t) if len(t.split()) > 3 else t
    t = re.sub(r",?\s*\b(book|vol\.?|volume|part)\s*\d+\s*$", "", t, flags=re.I)
    return seriesnorm.title_key(t)


def _title_score(ours: str, theirs: str) -> float:
    a, b = core_title(ours), core_title(theirs)
    if not a or not b:
        return 0.0
    if a == b or seriesnorm.title_key(ours) == seriesnorm.title_key(theirs):
        return 1.0
    # "Thursday Murder Club 2" must never match book 1 on fuzzy similarity
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


MIN_TITLE_SCORE = 0.92


def _authors_of(book) -> list:
    names = [p["person"]["name"] for p in book.get("people") or []
             if (p.get("role") or {}).get("name") == "Author" and p.get("person")]
    return names or [a.get("name") for a in book.get("authors") or [] if a.get("name")]


def _series_of(book) -> list:
    out = []
    for s in book.get("seriesWithId") or []:
        pos = s.get("position") or {}
        value = pos.get("value") or s.get("ordinal")
        out.append({
            "name": (s.get("title") or "").strip(),
            "position": clean_position(value),
            "external_id": s.get("id"),
        })
    if not out:  # older/list shapes: {"series": {"id","title"}, "position": {...}}
        raw = book.get("series")
        for s in ([raw] if isinstance(raw, dict) else raw or []):
            if isinstance(s, dict) and isinstance(s.get("series"), dict):
                out.append({
                    "name": (s["series"].get("title") or "").strip(),
                    "position": clean_position((s.get("position") or {}).get("value") or s.get("ordinal")),
                    "external_id": s["series"].get("id"),
                })
    return [s for s in out if s["name"] and not seriesnorm.is_imprint(s["name"])]


def clean_position(value):
    """'1', 1, 'Book 1', '2, Dramatized Adaptation', '1-3' -> '1' / '1-3'
    (first number or a plain range); None when nothing usable."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"\.\d+|\d+(?:\.\d+)?", text)
    return m.group(0) if m else None


def pick_match(results, title: str, authors: list):
    """Best result for (title, any of authors), or None. Requires the author
    to match (a same-titled book by someone else is never accepted) and a
    close title; among candidates prefers a book that has series data, then
    the closest title. Deliberately biased to a miss over a wrong match."""
    best, best_key = None, None
    for book in results or []:
        if not any(same_person(a, theirs) for a in authors for theirs in _authors_of(book)):
            continue
        score = _title_score(title, book.get("title") or "")
        if score < MIN_TITLE_SCORE:
            continue
        key = (bool(_series_of(book)), score)
        if best_key is None or key > best_key:
            best, best_key = book, key
    return best


# ---- public API --------------------------------------------------------------

def lookup_key(title: str, authors: list) -> str:
    people = "|".join(sorted(" ".join(_person_key(a)) for a in authors if a))
    return f"{seriesnorm.title_key(title)}#{people}"


def is_bundle(title: str) -> bool:
    return bool(_BUNDLE_RE.search(title or ""))


def is_cached(title: str, authors) -> bool:
    """True if a fresh answer (hit or miss) is already stored, i.e.
    find_series would not need the network."""
    authors = [x for x in (authors or []) if x and x.strip()]
    if not title or not authors:
        return True
    cached = db.enrichment_get(SOURCE, lookup_key(title, authors))
    if cached is None:
        return False
    ttl = FOUND_TTL if cached["found"] else MISSING_TTL
    return time.time() - cached["fetched_at"] < ttl


def find_series(title: str, authors, allow_network: bool = True):
    """Series for a book, as [{'name', 'position', 'external_id'}].

    Returns None when nothing is known yet (not looked up / budget or
    backoff prevented a lookup) and [] when AudiobookDB was asked and has no
    series for it (a genuine standalone or an unlisted book) -- both mean
    "leave Open Library's data alone". Results, including misses, are cached.
    """
    authors = [a for a in (authors or []) if a and a.strip()]
    if not ENABLED or not title or not authors or is_bundle(title):
        return None
    key = lookup_key(title, authors)
    hit = _cached_series(key)
    if hit is not None:
        return hit
    if not allow_network:
        return None

    # Single-flight: identical lookups (same title+author key) already in
    # progress -- overlapping builds, a client retrying a timed-out call, two
    # authors sharing a work -- wait for the first and reuse its result
    # instead of each issuing their own request. Different keys never wait
    # on each other.
    with _inflight_lock:
        flight = _inflight.get(key)
        leader = flight is None
        if leader:
            flight = _inflight[key] = _Flight()
    if not leader:
        flight.done.wait(FLIGHT_WAIT_SECONDS)
        return flight.result  # None if the leader failed or we timed out
    try:
        # the previous leader may have finished between our cache check and
        # our registration
        hit = _cached_series(key)
        if hit is None:
            hit = _fetch_series(key, title, authors)
        flight.result = hit
        return hit
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        flight.done.set()


def _cached_series(key: str):
    """Fresh cached answer ([] for a cached miss), or None."""
    cached = db.enrichment_get(SOURCE, key)
    if cached is None:
        return None
    ttl = FOUND_TTL if cached["found"] else MISSING_TTL
    if time.time() - cached["fetched_at"] >= ttl:
        return None
    return cached["payload"]["series"] if cached["found"] else []


def _fetch_series(key: str, title: str, authors: list):
    if not _bucket.try_take():
        return None  # over the shared allowance: cache-only for now
    results = _post_search(f"{title} {authors[0]}")
    if results is None:
        return None  # failure/limit: not cached, retried later
    match = pick_match(results, title, authors)
    series = _series_of(match) if match else []
    db.enrichment_put(SOURCE, key, bool(series), {"series": series, "matched": (match or {}).get("title")})
    return series
