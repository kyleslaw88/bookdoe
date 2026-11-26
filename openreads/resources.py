"""Builds the exact JSON shapes Readarr's BookInfoProxy expects, matching
the internal.* schemas from rreading-glasses' own OpenAPI spec (pulled
live from a running instance to get field names/types exactly right).

Data comes from Open Library first; Google Books fills gaps at the
edition level only. Everything is cached in SQLite (db.py) so repeat
requests -- and Readarr's own periodic refresh scans -- don't hammer
either upstream API.
"""
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
import aliases
import audiobookdb as abdb
import db
import seriesnorm
import openlibrary as ol
import googlebooks as gb

FRESH_SECONDS = 7 * 24 * 3600  # re-fetch upstream data at most weekly
ENRICH_RETRY_SECONDS = 24 * 3600  # don't hammer Google Books retrying a failed enrichment

_YEAR_RE = re.compile(r"(1[0-9]{3}|20[0-9]{2})")
_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y", "%Y-%m", "%m/%d/%Y")


def _normalize_date(raw) -> str:
    """Readarr's client deserializes ReleaseDate/ReleaseDateRaw as .NET
    DateTime, not a free-form string -- despite the OpenAPI schema saying
    'string'. Open Library's publish_date is unstructured text ("2019",
    "January 2019", "Jan 15, 2019", ...); anything that isn't a full
    ISO-parseable date throws a JsonReaderException client-side, which
    Readarr's own BookInfoProxy.Search silently swallows into an empty
    result set -- no error, no timeout, just "no results". Every date we
    emit must be a real ISO-8601 datetime or empty."""
    if not raw:
        return ""
    raw = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%dT00:00:00Z")
        except ValueError:
            continue
    m = _YEAR_RE.search(raw)
    if m:
        return f"{m.group(1)}-01-01T00:00:00Z"
    return ""


def _series_order(position) -> int:
    """Integer sort key for a position string ('6' -> 6, '1-3' -> 1,
    '0.5' -> 0); 0 when there isn't a leading number."""
    m = re.match(r"\s*(\d+)", position or "")
    return int(m.group(1)) if m else 0


def _stale(fetched_at):
    return not fetched_at or (time.time() - fetched_at) > FRESH_SECONDS


def _kca(kind: str, ol_key: str) -> str:
    return f"kca://{kind}/openlibrary.{ol_key}" if ol_key else ""


def ensure_author(author_id: int, force: bool = False):
    row = db.author_by_id(author_id)
    if row is None:
        return None
    if force or _stale(row["fetched_at"]):
        data = ol.get_author(row["ol_key"])
        if data:
            db.update_author(
                author_id,
                name=data["name"],
                description=data["description"],
                image_url=data["image_url"],
            )
            row = db.author_by_id(author_id)
    return row


# What to do with works that have no language data on any edition (about
# 10 of 86 for Tui T. Sutherland, several of them real English titles such
# as "The Hidden Kingdom"). Default: include -- dropping them loses real
# English books, and Readarr adds them unmonitored. Set to "exclude" for a
# strict English-only list.
UNKNOWN_LANGUAGE_POLICY = os.environ.get("OPENREADS_UNKNOWN_LANGUAGE", "include").lower()
_NO_SIGNAL = {"und", "zxx", "mul"}


def is_english_work(languages) -> bool:
    """languages: comma string from works.languages, or None/'' if unknown."""
    codes = {c for c in (languages or "").split(",") if c}
    if "eng" in codes:
        return True
    if codes - _NO_SIGNAL:
        return False  # has language data, none of it English
    return UNKNOWN_LANGUAGE_POLICY != "exclude"


def ensure_author_works(author_id: int, force: bool = False):
    row = db.author_by_id(author_id)
    if row is None:
        return
    works = db.works_for_author(author_id)
    # Also re-list when cached works predate language tracking (NULL), so
    # existing authors pick up the English filter on their next request.
    if works and not force and all(w["languages"] is not None for w in works):
        return
    # A person may have several OL author records (see aliases.py): list
    # the union of all of them under the one canonical author.
    listed, langs = [], {}
    for key in aliases.group_keys(row["ol_key"]):
        listed += ol.get_author_works(key)
        langs.update(ol.get_author_work_languages(key) or {})
    for w in listed:
        work_id = db.get_or_create_work(w["ol_key"], w["title"])
        db.link_work_author(work_id, author_id)
        db.set_work_languages(work_id, langs.get(w["ol_key"], []))


def ensure_work(work_id: int, force: bool = False, enrich: bool = True):
    row = db.work_by_id(work_id)
    if row is None:
        return None
    if force or _stale(row["fetched_at"]):
        data = ol.get_work(row["ol_key"])
        if data:
            db.update_work(work_id, title=data["title"], description=data["description"])
            try:
                import v2db
                v2db.observe_legacy("work", work_id, "openlibrary", {"title": data.get("title"), "description": data.get("description"), "series": data.get("series"),
                                                              "subjects": data.get("subjects"), "subject_places": data.get("subject_places")})
            except Exception:
                pass
            for a_key in data["author_keys"]:
                author_id = db.get_or_create_author(a_key)
                db.link_work_author(work_id, author_id)
            for s in data["series"]:
                if seriesnorm.is_imprint(s["name"]):
                    continue
                series_id = db.get_or_create_series(s["name"])
                db.link_work_series(work_id, series_id, s["position"])
            row = db.work_by_id(work_id)
    # Search passes enrich=False: it creates a work per upstream hit, and a Google
    # Books call each (up to 3s) turned a search into a ten-second wait. Descriptions
    # are not shown in a result list anyway - they are fetched by get_work(), which is
    # the moment one is actually displayed.
    _enrich_work_with_google(row, enable=enrich)
    return db.work_by_id(work_id) or row


def _enrich_work_with_google(row, enable: bool = True):
    """Fill a work-level description from Google Books when Open Library has none.

    Google Books was already consulted for *editions*, but a work's own description -
    the one a client actually displays - came from Open Library alone. For whole
    authors that carries nothing: Craig Alanson's entire catalogue is in Open Library
    with zero-length descriptions, while Google Books has 1,200+ characters for the
    same titles. Never overwrites a description Open Library did supply.
    """
    if not enable or row is None or row["description"]:
        return
    keys = row.keys()
    attempted_at = row["description_enrich_attempted_at"] if "description_enrich_attempted_at" in keys else None
    if attempted_at and (time.time() - attempted_at) < ENRICH_RETRY_SECONDS:
        return
    # Marked before the call, not after: a work Google Books has nothing for must not
    # be re-asked on every search that happens to return it.
    db.mark_work_description_enriched(row["id"])
    authors = db.authors_for_work(row["id"])
    author_name = authors[0]["name"] if authors else None
    gvol = gb.find_by_title_author(row["title"], author_name)
    if not gvol or not gvol.get("description"):
        return
    db.update_work(row["id"], description=gvol["description"])
    try:
        import v2db
        v2db.observe_legacy(
            "work", row["id"], "google_books",
            {"title": gvol.get("title"), "description": gvol.get("description")},
            confidence=0.9,
        )
    except Exception:
        pass


AUDIENCE_RETRY_SECONDS = 30 * 24 * 3600  # a work Google answered (or had nothing for) is not re-asked for a month


def ensure_work_audience(work_id: int, force: bool = False) -> dict:
    """Make sure the audience signals (subjects, categories, maturity rating) for a work
    are on record, asking each provider at most once: Open Library for the work's
    subjects (one call, skipped when subjects are already stored) and Google Books for
    categories and maturityRating (one call, by ISBN when an edition has one, else title
    and author; skipped without an API key, while the rate-limit breaker is open, or when
    asked within AUDIENCE_RETRY_SECONDS). Returns per-provider status plus `complete`,
    False when a provider could not answer now and the caller should come back later."""
    import v2db

    row = db.work_by_id(work_id)
    if row is None:
        return {"status": {}, "complete": True, "found": False}
    status = {}
    cid = v2db.sync_work(work_id)
    have_subjects = any(a["provider"] == "openlibrary" and a["field"] == "subjects" for a in v2db.audience_for_work(cid)) if cid else False
    if have_subjects and not force:
        status["openlibrary"] = "stored"
    else:
        try:
            data = ol.get_work_subjects(row["ol_key"])
        except Exception:
            status["openlibrary"] = "error"
        else:
            if data is None:
                status["openlibrary"] = "none"
            else:
                v2db.observe_legacy("work", work_id, "openlibrary", {"subjects": data["subjects"], "subject_places": data["subject_places"]})
                status["openlibrary"] = "ok" if data["subjects"] else "none"
    attempted = row["audience_attempted_at"] if "audience_attempted_at" in row.keys() else None
    if not gb.API_KEY:
        status["google_books"] = "disabled"
    elif gb.in_backoff():
        status["google_books"] = "deferred"
    elif attempted and not force and (time.time() - attempted) < AUDIENCE_RETRY_SECONDS:
        status["google_books"] = "stored"
    else:
        authors = db.authors_for_work(work_id)
        author_name = authors[0]["name"] if authors else None
        gvol = None
        for e in db.editions_for_work(work_id):
            isbn = e["isbn13"] or e["isbn10"]
            if isbn:
                gvol = gb.find_by_isbn(isbn)
                if gvol or gb.in_backoff():
                    break
        if not gvol and not gb.in_backoff():
            gvol = gb.find_by_title_author(row["title"], author_name)
        if gvol:
            v2db.observe_legacy("work", work_id, "google_books", {
                "categories": gvol.get("categories"), "maturity_rating": gvol.get("maturity_rating"),
                "language": gvol.get("language"), "publisher": gvol.get("publisher"),
                "num_pages": gvol.get("num_pages"),
                "series": [s for s in (gvol.get("series") or []) if s.get("series_id") or s.get("position")] or None},
                confidence=0.9)
            db.mark_work_audience_attempted(work_id)
            status["google_books"] = "ok"
        elif gb.in_backoff():
            status["google_books"] = "deferred"  # the breaker opened on this call: nothing was learned
        else:
            db.mark_work_audience_attempted(work_id)
            status["google_books"] = "none"
    complete = not any(v in ("error", "deferred") for v in status.values())
    return {"status": status, "complete": complete, "found": True, "audience": v2db.audience_for_work(cid) if cid else []}


def ensure_work_editions(work_id: int, force: bool = False):
    row = db.work_by_id(work_id)
    if row is None:
        return
    if not force and row["editions_fetched_at"]:
        return
    adopted = False
    for e in ol.get_work_editions(row["ol_key"]):
        if not adopted:
            adopted = db.adopt_placeholder_edition(work_id, e["ol_key"])
        edition_id = db.get_or_create_edition(e["ol_key"], work_id, e["title"])
        updates = {
            k: v
            for k, v in {
                "isbn13": e["isbn13"],
                "isbn10": e["isbn10"],
                "publisher": e["publisher"],
                "release_date": e["release_date"],
                "num_pages": e["num_pages"],
                "image_url": e["image_url"],
            }.items()
            if v
        }
        if updates:
            db.update_edition(edition_id, **updates)
        try:
            import v2db
            v2db.observe_legacy("edition", edition_id, "openlibrary", {k: e.get(k) for k in ("title", "isbn13", "isbn10", "publisher", "release_date", "num_pages", "image_url")})
        except Exception:
            pass
    db.mark_work_editions_fetched(work_id)


def _enrich_edition_with_google(edition_row, work_title: str, author_name: str = None, enable: bool = True):
    """Fill gaps (description, cover, publisher, pages) from Google Books
    when Open Library's edition-level data is thin. Never overwrites data
    OL already gave us. Skipped entirely when enable=False (bulk hydration
    paths, where 20+ works being enriched serially would blow past
    Readarr's HTTP timeout -- see build_work_resource's enrich param)."""
    if not enable:
        return edition_row
    needs_enrichment = not edition_row["description"] or not edition_row["image_url"]
    if not needs_enrichment:
        return edition_row
    attempted_at = edition_row["enrich_attempted_at"]
    if attempted_at and (time.time() - attempted_at) < ENRICH_RETRY_SECONDS:
        return edition_row
    db.update_edition(edition_row["id"], enrich_attempted_at=time.time())
    gvol = None
    if edition_row["isbn13"]:
        gvol = gb.find_by_isbn(edition_row["isbn13"])
    elif edition_row["isbn10"]:
        gvol = gb.find_by_isbn(edition_row["isbn10"])
    if not gvol:
        gvol = gb.find_by_title_author(work_title, author_name)
    if gvol:
        try:
            import v2db
            v2db.observe_legacy("edition", edition_row["id"], "google_books", {k: gvol.get(k) for k in ("title", "description", "publisher", "release_date", "num_pages", "image_url", "isbn13", "isbn10", "series", "categories", "maturity_rating", "language")}, confidence=0.9)
        except Exception:
            pass
        updates = {}
        if not edition_row["description"] and gvol.get("description"):
            updates["description"] = gvol["description"]
        if not edition_row["image_url"] and gvol.get("image_url"):
            updates["image_url"] = gvol["image_url"]
        if not edition_row["publisher"] and gvol.get("publisher"):
            updates["publisher"] = gvol["publisher"]
        if not edition_row["num_pages"] and gvol.get("num_pages"):
            updates["num_pages"] = gvol["num_pages"]
        if updates:
            db.update_edition(edition_row["id"], **updates)
            edition_row = db.edition_by_id(edition_row["id"])
    return edition_row


def _enrich_series_positions(work_id: int, work_title: str, author_name: str = None):
    """Fill a missing series position from Google Books' seriesInfo.

    Google exposes only an opaque seriesId, not a name, so this never
    creates series: it only adds a position to a series Open Library
    already named, and only when the work has exactly one such series
    (otherwise we can't tell which one Google's number belongs to)."""
    missing = [s for s in db.series_for_work(work_id) if not s["position"]]
    if len(db.series_for_work(work_id)) != 1 or not missing:
        return
    w = db.work_by_id(work_id)
    attempted = w["series_enrich_attempted_at"]
    if attempted and (time.time() - attempted) < ENRICH_RETRY_SECONDS:
        return
    db.update_work(work_id, series_enrich_attempted_at=time.time())
    gvol = None
    for e in db.editions_for_work(work_id):
        isbn = e["isbn13"] or e["isbn10"]
        if isbn:
            gvol = gb.find_by_isbn(isbn)
            if gvol:
                break
    if not gvol:
        gvol = gb.find_by_title_author(work_title, author_name)
    if not gvol:
        return
    positions = [s["position"] for s in gvol.get("series", []) if s.get("position")]
    if positions:
        db.link_work_series(work_id, missing[0]["id"], positions[0])


MAX_SIBLING_FETCH = 5  # cap on extra Open Library fetches per work


def _series_with_siblings(work_id: int, title: str) -> list:
    """The work's series links, plus those of duplicate works.

    Open Library often holds one book as several works (same title, same
    author, different work ids) and tags only some with series. Any work
    by the same author with the same seriesnorm.title_key is treated as
    the same book: its series/position is inherited where this work has
    none. Computed per response, nothing is written back and the duplicate
    works themselves are left alone. Returns rows shaped like
    db.series_for_work ('id', 'name', 'position')."""
    key = seriesnorm.title_key(title)
    merged = {r["id"]: dict(r) for r in db.series_for_work(work_id)}
    if not key:
        return list(merged.values())
    sibs = [s["id"] for s in db.works_sharing_author(work_id)
            if seriesnorm.title_key(s["title"]) == key]
    fetched = 0
    for sib_id in sorted(sibs):
        sib = db.work_by_id(sib_id)
        if _stale(sib["fetched_at"]) and fetched < MAX_SIBLING_FETCH:
            fetched += 1
            try:
                ensure_work(sib_id)
            except Exception as e:  # a sibling's failure must not fail this work
                import logging
                logging.getLogger("openreads").warning("sibling fetch failed for %s: %s", sib_id, e)
        for row in db.series_for_work(sib_id):
            cur = merged.get(row["id"])
            if cur is None:
                merged[row["id"]] = dict(row)
            elif not cur["position"] and row["position"]:
                cur["position"] = row["position"]
    return list(merged.values())


# Network lookups against AudiobookDB allowed per author build; the rest are
# picked up on later refreshes (results, including misses, are cached).
ABDB_MAX_LOOKUPS_PER_BUILD = int(os.environ.get("AUDIOBOOKDB_MAX_LOOKUPS_PER_BUILD", "25"))
ABDB_MAX_SERIES = 3


def _enrich_series_from_audiobookdb(work_id: int, title: str, author_names: list, allow_network: bool, budget=None):
    """Fill series gaps from AudiobookDB (audiobook-side data; priority
    rules in audiobookdb.py). Runs only for a work that has no series (own
    or inherited from duplicate-title works) or a series without a
    position. Series names are matched through seriesnorm (via
    db.get_or_create_series), so they join the same rows Open Library used.
    `budget` ({'left': n}) caps network lookups within one author build;
    without network permission only cached answers are used."""
    if not abdb.ENABLED:
        return
    rows = _series_with_siblings(work_id, title)
    if rows and all(r["position"] for r in rows):
        return
    authors = [n for n in author_names if n]
    if allow_network and not abdb.is_cached(title, authors):
        if budget is not None:
            if budget["left"] <= 0:
                allow_network = False
            else:
                budget["left"] -= 1
    found = abdb.find_series(title, authors, allow_network=allow_network)
    if found:
        try:
            import v2db
            v2db.observe_legacy("work", work_id, "audiobookdb", {"series": found}, confidence=0.95)
        except Exception:
            pass
    for s in (found or [])[:ABDB_MAX_SERIES]:
        series_id = db.get_or_create_series(s["name"])
        db.link_work_series(work_id, series_id, s["position"], source="audiobookdb")


def build_book_resource(edition_id: int, work_title: str = None, author_name: str = None, author_ids: list = None, enrich: bool = True) -> dict:
    e = db.edition_by_id(edition_id)
    if e is None:
        return None
    e = _enrich_edition_with_google(e, work_title or e["title"] or "", author_name, enable=enrich)
    title = e["title"] or work_title or "Unknown"
    contributors = [{"ForeignId": aid, "Role": "Author"} for aid in (author_ids or [])]
    return {
        "Asin": e["asin"] or "",
        "AverageRating": 0,
        "Contributors": contributors,
        "Description": e["description"] or "",
        "EditionInformation": "",
        "ForeignId": e["id"],
        "Format": "",
        "FullTitle": title,
        "ImageUrl": e["image_url"] or "",
        "IsEbook": False,
        "Isbn13": e["isbn13"] or "",
        "KCA": _kca("book", e["ol_key"]),
        "Language": "eng",
        "NumPages": e["num_pages"] or 0,
        "Publisher": e["publisher"] or "",
        "RatingCount": 0,
        "RatingSum": 0,
        "ReleaseDate": _normalize_date(e["release_date"]),
        "ReleaseDateRaw": _normalize_date(e["release_date"]),
        "ShortTitle": title,
        "Title": title,
        "Url": "",
    }


def build_series_resource(series_id: int) -> dict:
    s = db.series_by_id(series_id)
    if s is None:
        return None
    return {
        "Description": "",
        "ForeignId": s["id"],
        "KCA": "",
        "LinkItems": [],
        "Title": s["name"],
    }


def build_work_resource(work_id: int, hydrate_editions: bool = True, thin: bool = False, enrich: bool = True, abdb_budget=None) -> dict:
    """Shared shape for /work/{id}, /book/{editionId}, and entries nested
    inside AuthorResource.Works / bulkBookResource.Works.

    thin=True skips edition hydration for performance when listing dozens
    of works under an author (the individual /work/{id} call will do the
    full fetch on demand). Thin entries still carry their Authors and one
    Book (the first cached edition, else a stable placeholder) -- Readarr's
    SanitizeWorks drops any work whose Books or Authors is empty, taking
    its series links with it.
    """
    do_editions = hydrate_editions and not thin
    if do_editions:
        # ensure_work (OL /works/{id}.json) and ensure_work_editions (OL
        # /works/{id}/editions.json) are independent OL calls keyed off the
        # same ol_key -- run them concurrently instead of back-to-back.
        # With Readarr's client fetching one work per HTTP request (no
        # batching for this older/stock codepath), this per-work latency
        # is the thing that adds up across a prolific author's catalog.
        with ThreadPoolExecutor(max_workers=2) as pool:
            work_future = pool.submit(ensure_work, work_id)
            pool.submit(ensure_work_editions, work_id)
            w = work_future.result()
    else:
        w = ensure_work(work_id)
    if w is None:
        return None

    authors_out = []
    for a in db.authors_for_work(work_id):
        authors_out.append(
            {
                "AverageRating": 0,
                "Description": a["description"] or "",
                "ForeignId": a["id"],
                "ImageUrl": a["image_url"] or "",
                "KCA": _kca("author", a["ol_key"]),
                "Name": a["name"] or "",
                "RatingCount": 0,
                "Series": [],
                "Url": "",
                "Works": [],
            }
        )

    books_out = []
    best_book_id = 0
    if do_editions:
        editions = db.editions_for_work(work_id)
        author_name = authors_out[0]["Name"] if authors_out else None
        author_ids = [a["ForeignId"] for a in authors_out]
        for i, e in enumerate(editions):
            # Only the lead edition is worth a Google Books call -- a work
            # can carry dozens of foreign-language/reprint editions, and
            # enriching each one serially is what drove real Readarr
            # lookups into multi-minute hangs against Google's rate limit.
            book = build_book_resource(
                e["id"], work_title=w["title"], author_name=author_name,
                author_ids=author_ids, enrich=(enrich and i == 0)
            )
            if book:
                books_out.append(book)
        if books_out:
            best_book_id = books_out[0]["ForeignId"]
        else:
            # No editions found on Open Library -- synthesize one placeholder
            # edition so Readarr has something to attach a release to.
            synth_id = db.get_or_create_placeholder_edition(work_id, w["title"])
            book = build_book_resource(
                synth_id, work_title=w["title"], author_name=author_name,
                author_ids=author_ids, enrich=enrich
            )
            books_out.append(book)
            best_book_id = synth_id

    else:
        author_ids = [a["ForeignId"] for a in authors_out]
        cached = db.editions_for_work(work_id)
        lead_id = min((e["id"] for e in cached), default=None)
        if lead_id is None:
            lead_id = db.get_or_create_placeholder_edition(work_id, w["title"])
        book = build_book_resource(
            lead_id, work_title=w["title"], author_ids=author_ids, enrich=False
        )
        books_out.append(book)
        best_book_id = lead_id

    if do_editions and enrich:
        _enrich_series_positions(
            work_id, w["title"], authors_out[0]["Name"] if authors_out else None
        )

    # Single-work lookups may use the network; author builds pass a budget;
    # bulk (enrich=False) only reads cached answers.
    _enrich_series_from_audiobookdb(
        work_id, w["title"], [a["Name"] for a in authors_out],
        allow_network=enrich and (abdb_budget is not None or not thin), budget=abdb_budget,
    )

    series_out = []
    for s in _series_with_siblings(work_id, w["title"]):
        series_out.append(
            {
                "Description": "",
                "ForeignId": s["id"],
                "KCA": "",
                "LinkItems": [
                    {
                        "ForeignWorkId": work_id,
                        "PositionInSeries": s["position"] or "",
                        "Primary": True,
                        "SeriesPosition": _series_order(s["position"]),
                    }
                ],
                "Title": s["name"],
            }
        )

    title = w["title"] or "Unknown"
    return {
        "Authors": authors_out,
        "AverageRating": 0,
        "BestBookId": best_book_id,
        "Books": books_out,
        "ForeignId": work_id,
        "FullTitle": title,
        "Genres": [],
        "KCA": _kca("work", w["ol_key"]),
        "RatingCount": 0,
        "RatingSum": 0,
        "RelatedWorks": [],
        "ReleaseDate": "",
        "ReleaseDateRaw": "",
        "Series": series_out,
        "ShortTitle": title,
        "Title": title,
        "Url": "",
    }


def _build_work_safe(any_id: int):
    """Isolate one id's failure so a single Open Library hiccup (reset
    connection, timeout) doesn't take down the whole bulk response --
    Readarr would rather get 19 of 20 works than a 500 for all 20.

    Real Goodreads-derived ids share one global numbering, so work ids
    and edition/book ids never collide -- callers built against that
    contract (e.g. Readarr's own combined search, which resolves ids via
    a search result's BookId rather than WorkId) pass either kind
    interchangeably into /bulk. Our works and editions tables are
    separate autoincrement counters, so a given integer can be a valid
    id in both -- try it as a work id first, and only fall back to
    resolving it as an edition's parent work if that comes up empty."""
    try:
        w = build_work_resource(any_id, enrich=False)
        if w is not None:
            return w
        e = db.edition_by_id(any_id)
        if e is not None and e["work_id"] is not None:
            return build_work_resource(e["work_id"], enrich=False)
        return None
    except Exception as e:
        import logging
        logging.getLogger("openreads").warning("bulk hydration failed for id %s: %s", any_id, e)
        return None


def build_works_bulk(work_ids: list) -> list:
    """Hydrate many works concurrently, without Google Books enrichment
    (see build_work_resource's enrich flag) -- used by /bulk and
    /book/bulk, which Readarr calls with 15-20+ ids in one request and
    expects a response well within its HTTP timeout.

    Concurrency is kept modest (4) because Open Library resets connections
    under heavier simultaneous load from a single client."""
    if not work_ids:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(work_ids))) as pool:
        results = list(pool.map(_build_work_safe, work_ids))
    return [r for r in results if r]


def _refresh_series_titles(works):
    """A series row can be renamed to its canonical spelling while an
    author/bulk response is still being assembled, leaving earlier works
    carrying the older title. Re-read the names so one response never
    shows two titles for one series id."""
    names = {}
    for w in works:
        for s in w["Series"]:
            if s["ForeignId"] not in names:
                row = db.series_by_id(s["ForeignId"])
                names[s["ForeignId"]] = row["name"] if row else s["Title"]
            s["Title"] = names[s["ForeignId"]]


def merge_series(series_lists) -> list:
    """Combine per-work Series entries into one entry per series
    ForeignId, with LinkItems unioned across all works (deduped by
    ForeignWorkId). Readarr matches each author-level series link against
    the books it already has locally, so it needs every work's link, not
    just whichever work was seen first."""
    merged = {}
    for series in series_lists:
        for s in series:
            entry = merged.get(s["ForeignId"])
            if entry is None:
                entry = merged[s["ForeignId"]] = {**s, "LinkItems": []}
            have = {li["ForeignWorkId"] for li in entry["LinkItems"]}
            for li in s["LinkItems"]:
                if li["ForeignWorkId"] not in have:
                    entry["LinkItems"].append(dict(li))
                    have.add(li["ForeignWorkId"])
    for entry in merged.values():
        entry["LinkItems"].sort(key=lambda li: (li["SeriesPosition"], li["ForeignWorkId"]))
    return list(merged.values())


def build_author_resource(author_id: int, force: bool = False) -> dict:
    row = db.author_by_id(author_id)
    if row is None:
        return None
    author_id = row["id"]  # canonical id if author_id was a duplicate's
    # ensure_author (OL /authors/{id}.json) and ensure_author_works (OL
    # /authors/{id}/works.json) are independent OL calls keyed off the same
    # ol_key -- run concurrently. An ambiguous search term can resolve to a
    # dozen-plus candidate authors, each fetched as a separate sequential
    # HTTP request by the client, so shaving latency off each one matters.
    with ThreadPoolExecutor(max_workers=2) as pool:
        author_future = pool.submit(ensure_author, author_id, force)
        pool.submit(ensure_author_works, author_id, force)
        a = author_future.result()
    if a is None:
        return None

    works_out = []
    listed = [w for w in db.works_for_author(author_id) if is_english_work(w["languages"])]
    # Fetch every work first: series inherited from duplicate-title works
    # must not depend on which duplicate happens to be built first.
    for w in listed:
        ensure_work(w["id"])
    budget = {"left": ABDB_MAX_LOOKUPS_PER_BUILD}
    for w in listed:
        wr = build_work_resource(w["id"], thin=True, abdb_budget=budget)
        if wr:
            works_out.append(wr)

    _refresh_series_titles(works_out)
    return {
        "AverageRating": 0,
        "Description": a["description"] or "",
        "ForeignId": author_id,
        "ImageUrl": a["image_url"] or "",
        "KCA": _kca("author", a["ol_key"]),
        "Name": a["name"] or "",
        "RatingCount": 0,
        "Series": merge_series(w["Series"] for w in works_out),
        "Url": "",
        "Works": works_out,
    }
