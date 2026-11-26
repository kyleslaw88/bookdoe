"""Cover art: the library grid rendered an empty frame for every work.

Works imported from Readarr carry no cover_url at all, so a grid that rendered
`w.cover_url` had nothing to render for 12,026 of 12,026 works. bookdoe now
resolves art from the file on disk, the linked metadata record, or OpenLibrary,
caches the bytes, and serves them itself.

Network isolation: an autouse fixture replaces ``covers.httpx.Client`` for the
entire file, so any test that reaches a remote source without stubbing it fails
immediately.  This blocks exactly ``httpx.Client`` and nothing else; it covers
every remote source in ``services/covers.py`` (``_fetch``, ``lookup_cover_ids``
and ``_from_google_books``) only because each opens its connection through
``httpx.Client``.  A source using ``httpx.get``, ``httpx.AsyncClient`` or another
HTTP library would bypass it.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import covers

# Capture the real httpx.Client at import time, before any test patches it.
# This is needed by helpers that build MockTransport stand-ins: if we capture
# ``httpx.Client`` at call time we pick up the *previous* test's patch, so a
# second monkeypatch in the same test silently keeps answering the first status.
import httpx as _httpx
_REAL_HTTPX_CLIENT = _httpx.Client

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


# ── Network isolation ──────────────────────────────────────────────────
# Every test in this file that exercises a remote cover source stubs the
# underlying ``httpx.Client`` (or the higher-level ``lookup_cover_ids`` /
# ``_fetch`` wrappers).  This autouse fixture makes the claim true by
# construction: any test that forgets to stub will raise immediately.
#
# Tests that legitimately need the network (e.g. ``_search_returning``
# helpers that build a ``MockTransport``) monkey-patch ``httpx.Client``
# themselves, which overrides this fixture's patch.


class _NoNetwork(BaseException):
    """Raised when code under test tries to open a real HTTP connection.

    Derives from BaseException so the broad ``except Exception`` handlers around
    remote fetches in ``covers`` cannot swallow it.
    """


def _no_client(*args, **kwargs):  # pragma: no cover
    raise _NoNetwork("live network call blocked by autouse fixture")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Block all real HTTP calls in this file."""
    monkeypatch.setattr(covers.httpx, "Client", _no_client)


def test_block_network_fixture_works():
    """The autouse fixture blocks the real, un-stubbed remote path.

    No monkey-patch of its own: ``resolve_cover`` with a work that has an ISBN
    reaches ``lookup_cover_ids`` and so ``httpx.Client``, and ``_NoNetwork`` must
    propagate out of every ``except Exception`` on the way.
    """
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9781234567897','test',1,1.0)",
            (work_id,),
        )
    with pytest.raises(_NoNetwork, match="live network call blocked"):
        covers.resolve_cover(work_id, allow_remote=True)


# ── Helpers ────────────────────────────────────────────────────────────


def _work(title: str = "Dead World") -> str:
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'unresolved',?,?)",
            (wid, title, title, t, t),
        )
    return wid


def _epub(path: Path, *, image: bytes = PNG, href: str = "images/cover.png") -> Path:
    opf = f"""<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Dead World</dc:title>
        <meta name="cover" content="cover-image"/></metadata>
      <manifest><item id="cover-image" href="{href}" media-type="image/png"/></manifest>
    </package>"""
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr(f"OEBPS/{href}", image)
    return path


def _attach_file(work_id: str, path: Path, kind: str = "ebook") -> None:
    asset_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,?,?,'owned',1,?,?)",
            (asset_id, work_id, kind, path.suffix.lstrip("."), t, t),
        )
        conn.execute(
            """INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at)
               VALUES (?,?,?,?, '{}',1,0,0,?)""",
            (db.uid(), asset_id, str(path), path.stat().st_size, t),
        )


def test_sniff_rejects_non_images():
    assert covers.sniff(PNG) == "image/png"
    assert covers.sniff(JPEG) == "image/jpeg"
    # A provider that answers a missing cover with an HTML error page still labels it
    # image/jpeg often enough that the header cannot be trusted.
    assert covers.sniff(b"<!DOCTYPE html><html>not found</html>") is None
    assert covers.sniff(b"") is None


def test_cover_extracted_from_epub_and_served(tmp_path):
    work_id = _work()
    _attach_file(work_id, _epub(tmp_path / "book.epub"))

    result = covers.resolve_cover(work_id, allow_remote=False)

    assert result["status"] == "cached"
    assert result["source"] == "embedded"
    assert covers.cover_file(work_id).read_bytes() == PNG

    with TestClient(app) as c:
        r = c.get(f"/api/v1/library/{work_id}/cover")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content == PNG
        # Covers are immutable until refreshed, so they must not inherit the
        # API-wide no-store.
        assert "no-store" not in r.headers.get("cache-control", "")


def test_missing_cover_is_a_404_so_the_ui_can_fall_back(tmp_path):
    work_id = _work()
    with TestClient(app) as c:
        assert c.get(f"/api/v1/library/{work_id}/cover").status_code == 404


def test_failure_is_remembered_and_not_retried_forever(tmp_path):
    work_id = _work()
    assert covers.resolve_cover(work_id, allow_remote=False)["status"] == "none"
    row = db.one("SELECT * FROM cover_cache WHERE work_id=?", (work_id,))
    assert row["status"] == "none" and row["attempts"] == 1

    covers.resolve_cover(work_id, allow_remote=False)
    assert db.one("SELECT attempts FROM cover_cache WHERE work_id=?", (work_id,))["attempts"] == 2

    # Once a work has been tried MAX_ATTEMPTS times it drops out of the backfill.
    with db.tx() as conn:
        conn.execute("UPDATE cover_cache SET attempts=? WHERE work_id=?", (covers.MAX_ATTEMPTS, work_id))
    assert covers.backfill(limit=10, allow_remote=False)["considered"] == 0


def test_cached_row_without_bytes_is_refetched(tmp_path):
    work_id = _work()
    _attach_file(work_id, _epub(tmp_path / "book.epub"))
    covers.resolve_cover(work_id, allow_remote=False)

    # A restored database or a wiped cache directory leaves the row pointing at
    # nothing; that must not serve a permanent 404.
    covers.cover_file(work_id).unlink()
    assert covers.cached(work_id) is None
    assert db.one("SELECT * FROM cover_cache WHERE work_id=?", (work_id,)) is None
    assert covers.resolve_cover(work_id, allow_remote=False)["status"] == "cached"


def test_backfill_skips_works_that_already_have_art(tmp_path):
    first = _work("One")
    _attach_file(first, _epub(tmp_path / "one.epub"))
    second = _work("Two")

    result = covers.backfill(limit=10, allow_remote=False, pause=0)
    assert (result["considered"], result["found"], result["missing"]) == (2, 1, 1)

    # Second pass: the cached work is done and the failed one is inside its cooldown.
    assert covers.backfill(limit=10, allow_remote=False, pause=0)["considered"] == 0


def test_remote_sources_are_skipped_when_remote_fetching_is_off(monkeypatch, tmp_path):
    work_id = _work()
    with db.tx() as conn:
        conn.execute("UPDATE works SET cover_url='https://example.invalid/cover.jpg' WHERE id=?", (work_id,))

    def explode(url):  # pragma: no cover - asserts it is never reached
        raise AssertionError(f"remote fetch attempted for {url}")

    monkeypatch.setattr(covers, "_fetch", explode)
    result = covers.resolve_cover(work_id, allow_remote=False)

    # Deferred, not "none": a provider that has never been asked has not said no, and
    # recording a failure would spend an attempt the work never actually had. With
    # BOOKDOE_COVER_REMOTE_FETCH=0 this is every remote-only work in the library.
    assert result["status"] == "deferred"
    assert db.one("SELECT * FROM cover_cache WHERE work_id=?", (work_id,)) is None


def test_a_work_with_no_remote_source_is_recorded_as_having_none(monkeypatch):
    """Nothing to fall back to, so "no cover" is the honest, final answer."""
    work_id = _work()
    assert covers.resolve_cover(work_id, allow_remote=False)["status"] == "none"


def test_embedded_art_is_preferred_over_any_provider(monkeypatch, tmp_path):
    """The jacket in the user's own file beats a provider's.

    It costs no request, cannot fail on a provider outage, and is the cover of the
    edition they actually own rather than whichever edition the provider considers
    canonical.
    """
    work_id = _work()
    _attach_file(work_id, _epub(tmp_path / "book.epub"))
    with db.tx() as conn:
        conn.execute("UPDATE works SET cover_url='https://example.invalid/cover.jpg' WHERE id=?", (work_id,))

    def explode(url):  # pragma: no cover - asserts it is never reached
        raise AssertionError("a provider was contacted for a work with embedded art")

    monkeypatch.setattr(covers, "_fetch", explode)
    result = covers.resolve_cover(work_id, allow_remote=True)

    assert result["source"] == "embedded"
    assert result["remote_used"] is False
    assert covers.cover_file(work_id).read_bytes() == PNG


def test_metadata_url_is_used_when_there_is_no_local_art(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute("UPDATE works SET cover_url='https://example.invalid/cover.jpg' WHERE id=?", (work_id,))
    monkeypatch.setattr(covers, "_fetch", lambda url: JPEG)

    result = covers.resolve_cover(work_id, allow_remote=True)
    assert result["source"] == "metadata"
    assert covers.cover_file(work_id).read_bytes() == JPEG


def test_openlibrary_is_asked_when_nothing_local_exists(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9781234567897','test',1,1.0)",
            (work_id,),
        )
    asked = []

    def fake_fetch(url):
        asked.append(url)
        return JPEG

    monkeypatch.setattr(covers, "_fetch", fake_fetch)
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: {"9781234567897": 999})
    result = covers.resolve_cover(work_id, allow_remote=True)

    assert result["source"] == "openlibrary"
    assert asked == ["https://covers.openlibrary.org/b/id/999-L.jpg"]


def test_a_single_work_refresh_resolves_its_own_cover_id(monkeypatch):
    """A one-off refresh has no batch to ride on, so it looks its own ISBN up."""
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9781234567897','test',1,1.0)",
            (work_id,),
        )
    looked_up = []
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: (looked_up.append(list(isbns)), {"9781234567897": 42})[1])
    monkeypatch.setattr(covers, "_fetch", lambda url: JPEG)

    assert covers.resolve_cover(work_id, allow_remote=True)["source"] == "openlibrary"
    assert looked_up == [["9781234567897"]]


def test_non_image_payload_is_never_cached(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute("UPDATE works SET cover_url='https://example.invalid/cover.jpg' WHERE id=?", (work_id,))
    monkeypatch.setattr(covers, "_fetch", lambda url: None)
    assert covers.resolve_cover(work_id, allow_remote=True)["status"] == "none"
    assert not covers.cover_file(work_id).exists()


def test_throttling_is_not_recorded_as_a_missing_cover(monkeypatch):
    """A 429 means "not now", not "this book has no art".

    Recording it as a failure would spend one of the work's three attempts and
    eventually mark a book with a perfectly good cover as having none.
    """
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9781234567897','test',1,1.0)",
            (work_id,),
        )

    def throttled(url):
        raise covers.RateLimited("429")

    # Stub the OpenLibrary id lookup too: left live, whether _fetch is reached at all
    # depends on what openlibrary.org answers for a made-up ISBN that day (it found a
    # cover id here, but not on the nightly runner, which failed this test).
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: {"9781234567897": 999})
    monkeypatch.setattr(covers, "_fetch", throttled)
    result = covers.resolve_cover(work_id, allow_remote=True)

    assert result["status"] == "deferred"
    assert db.one("SELECT * FROM cover_cache WHERE work_id=?", (work_id,)) is None
    # Still queued for the next run rather than written off.
    assert covers.backfill(limit=5, allow_remote=False, pause=0)["considered"] == 1


def test_http_status_classification(monkeypatch):
    """403/429/5xx are throttling or outage; 404 really is "no cover"."""
    import httpx

    def answering(status):
        def handler(request):
            return httpx.Response(status, content=b"")

        def factory(**kwargs):
            return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))

        return factory

    for status in (403, 429):
        monkeypatch.setattr(covers.httpx, "Client", answering(status))
        with pytest.raises(covers.RateLimited):
            covers._fetch("https://example.invalid/cover.jpg")

    # A 5xx is one object being unavailable, not a request to slow down.
    for status in (500, 503):
        monkeypatch.setattr(covers.httpx, "Client", answering(status))
        with pytest.raises(covers.UpstreamUnavailable):
            covers._fetch("https://example.invalid/cover.jpg")

    monkeypatch.setattr(covers.httpx, "Client", answering(404))
    assert covers._fetch("https://example.invalid/cover.jpg") is None


def test_backfill_serves_owned_works_before_wanted_ones(tmp_path):
    """Owned books resolve locally and always succeed; wanted ones need a provider."""
    wanted = _work("Wanted Book")
    owned = _work("Owned Book")
    _attach_file(owned, _epub(tmp_path / "owned.epub"))

    # One slot only: the owned work must take it even though it was created second
    # and the ordering's later tie-breaker is most-recently-updated.
    result = covers.backfill(limit=1, allow_remote=False, pause=0)

    assert result["found"] == 1
    assert covers.cached(owned) is not None
    assert db.one("SELECT * FROM cover_cache WHERE work_id=?", (wanted,)) is None


def test_audiobook_cover_probe_asks_for_the_disposition_section(monkeypatch, tmp_path):
    """Regression: `stream=disposition` prints nothing and exits 0.

    `disposition` is its own ffprobe section, so asking for it as a field of `stream`
    produced empty output with a success exit code - and the guard read that as "this
    file has no attached picture". Every m4b in the library silently reported no cover
    art while ffmpeg could extract it perfectly well.
    """
    path = tmp_path / "book.m4b"
    path.write_bytes(b"pretend audiobook")
    calls = []

    class Result:
        def __init__(self, stdout=b"", text_stdout="", returncode=0):
            self.stdout = stdout or text_stdout
            self.returncode = returncode

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "ffprobe":
            return Result(text_stdout="1\n")
        return Result(stdout=JPEG)

    monkeypatch.setattr(covers.subprocess, "run", fake_run)
    assert covers._audio_cover(path) == JPEG

    probe_args = calls[0]
    assert "stream_disposition=attached_pic" in probe_args
    assert "stream=disposition" not in probe_args


def test_audiobook_without_attached_art_is_not_mined_for_a_video_frame(monkeypatch, tmp_path):
    path = tmp_path / "book.m4b"
    path.write_bytes(b"pretend audiobook")
    calls = []

    class Result:
        def __init__(self, stdout=b"", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(args, **kwargs):
        calls.append(args[0])
        return Result(stdout="")  # ffprobe reports no attached picture

    monkeypatch.setattr(covers.subprocess, "run", fake_run)
    assert covers._audio_cover(path) is None
    assert calls == ["ffprobe"], "ffmpeg must not run when there is no attached picture"


def _search_returning(docs, captured=None):
    """A stand-in for OpenLibrary's search.json."""
    def factory(**kwargs):
        def handler(request):
            if captured is not None:
                captured.append(str(request.url))
            return _httpx.Response(200, json={"docs": docs})
        return _REAL_HTTPX_CLIENT(transport=_httpx.MockTransport(handler))

    return factory


def test_cover_ids_are_resolved_for_many_isbns_in_one_query(monkeypatch):
    """The whole point: one request for a batch, not one request per book.

    OpenLibrary throttles covers looked up by ISBN to 100 per IP per 5 minutes, which
    made a 9,000-book backfill an overnight job. Resolving cover ids in batches and
    then fetching by id takes it off that path entirely.
    """
    captured = []
    docs = [
        {"cover_i": 111, "isbn": ["9780000000001", "0000000001"]},
        {"cover_i": 222, "isbn": ["9780000000002"]},
        {"cover_i": None, "isbn": ["9780000000003"]},
    ]
    monkeypatch.setattr(covers.httpx, "Client", _search_returning(docs, captured))

    result = covers.lookup_cover_ids(["9780000000001", "9780000000002", "9780000000003"])

    assert result == {"9780000000001": 111, "9780000000002": 222}
    assert len(captured) == 1, "three ISBNs must cost one request, not three"
    assert "isbn%3A" in captured[0] or "isbn:" in captured[0]


def test_lookup_batches_rather_than_sending_one_giant_query(monkeypatch):
    monkeypatch.setattr(covers, "ISBN_BATCH", 2)
    captured = []
    monkeypatch.setattr(covers.httpx, "Client", _search_returning([], captured))

    covers.lookup_cover_ids([f"978000000000{i}" for i in range(5)])

    assert len(captured) == 3, f"5 ISBNs at a batch of 2 should be 3 queries, got {len(captured)}"


def test_lookup_ignores_duplicate_and_empty_isbns(monkeypatch):
    captured = []
    monkeypatch.setattr(covers.httpx, "Client", _search_returning([], captured))
    covers.lookup_cover_ids(["9780000000001", "9780000000001", "", "9780000000001"])
    assert len(captured) == 1


def test_a_batched_cover_id_is_fetched_by_id_not_by_isbn(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9780000000001','test',1,1.0)",
            (work_id,),
        )
    asked = []

    def fake_fetch(url):
        asked.append(url)
        return JPEG

    monkeypatch.setattr(covers, "_fetch", fake_fetch)
    result = covers.resolve_cover(work_id, allow_remote=True, cover_ids={"9780000000001": 111})

    assert result["source"] == "openlibrary"
    assert asked == ["https://covers.openlibrary.org/b/id/111-L.jpg"]
    assert not any("/b/isbn/" in u for u in asked), "the throttled path must not be used"


def test_the_throttled_by_isbn_endpoint_is_never_used(monkeypatch):
    """Measured: for 25 works the batched search could not place, the by-ISBN
    endpoint found art for 0. Keeping it as a fallback would have reimposed
    OpenLibrary's 100-per-5-minute identifier limit on the entire backfill to buy
    nothing, so it is gone - and must stay gone."""
    ids = [_work(f"Book {i}") for i in range(3)]
    with db.tx() as conn:
        for i, wid in enumerate(ids):
            conn.execute(
                "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13',?,'test',1,1.0)",
                (wid, f"978000000000{i}"),
            )
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: {})
    asked = []
    monkeypatch.setattr(covers, "_fetch", lambda url: (asked.append(url), None)[1])

    covers.backfill(limit=3, allow_remote=True, pause=0)

    assert not any("/b/isbn/" in u for u in asked), f"throttled path used: {asked}"


def test_prefetch_throttling_defers_the_whole_batch(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9780000000001','test',1,1.0)",
            (work_id,),
        )

    def throttled(isbns):
        raise covers.RateLimited("429 from search.json")

    monkeypatch.setattr(covers, "lookup_cover_ids", throttled)
    result = covers.backfill(limit=5, allow_remote=True, pause=0)

    assert result["deferred"] == 1 and result["found"] == 0
    # Nothing written off on the strength of a throttle.
    assert db.one("SELECT COUNT(*) n FROM cover_cache")["n"] == 0



def _status_client(status):
    """An httpx.Client stand-in answering a given status."""
    def factory(**kwargs):
        def handler(request):
            return _httpx.Response(status, content=b"")
        return _REAL_HTTPX_CLIENT(transport=_httpx.MockTransport(handler))
    return factory


def test_a_5xx_on_one_cover_is_not_treated_as_throttling(monkeypatch):
    """The bug that livelocked the backfill at 30% for hours.

    OpenLibrary redirects cover ids to archive.org, and one file there 503'd
    indefinitely. That was classified as rate limiting, which aborted the whole
    400-book batch - and because the queue is deterministically ordered, the same
    dead file sat at the head of it and killed every run that followed.
    """
    monkeypatch.setattr(covers.httpx, "Client", _status_client(503))
    with pytest.raises(covers.UpstreamUnavailable):
        covers._fetch("https://covers.openlibrary.org/b/id/1-L.jpg")

    monkeypatch.setattr(covers.httpx, "Client", _status_client(429))
    with pytest.raises(covers.RateLimited):
        covers._fetch("https://covers.openlibrary.org/b/id/1-L.jpg")


def test_an_unavailable_cover_is_recorded_as_a_failure_not_a_deferral(monkeypatch):
    """So the seven-day retry applies and the queue moves on."""
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9780000000001','t',1,1.0)",
            (work_id,),
        )

    def unavailable(url):
        raise covers.UpstreamUnavailable("503")

    monkeypatch.setattr(covers, "_fetch", unavailable)
    result = covers.resolve_cover(work_id, allow_remote=True, cover_ids={"9780000000001": 1})

    assert result["status"] == "none"
    assert db.one("SELECT status FROM cover_cache WHERE work_id=?", (work_id,))["status"] == "none"


def test_one_bad_cover_does_not_abort_the_batch(monkeypatch):
    ids = [_work(f"Book {i}") for i in range(4)]
    mapping = {}
    with db.tx() as conn:
        for i, wid in enumerate(ids):
            isbn = f"978000000000{i}"
            mapping[isbn] = i + 1
            conn.execute(
                "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13',?,'t',1,1.0)",
                (wid, isbn),
            )

    def one_dead_object(url):
        if url.endswith("/b/id/1-L.jpg"):
            raise covers.UpstreamUnavailable("503 from archive.org")
        return JPEG

    monkeypatch.setattr(covers, "_fetch", one_dead_object)
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: mapping)
    result = covers.backfill(limit=4, allow_remote=True, pause=0)

    assert result["found"] == 3, result
    assert result["missing"] == 1, result
    assert result["deferred"] == 0


def test_a_single_throttle_does_not_abort_the_batch(monkeypatch):
    ids = [_work(f"Book {i}") for i in range(4)]
    mapping = {}
    with db.tx() as conn:
        for i, wid in enumerate(ids):
            isbn = f"978000000000{i}"
            mapping[isbn] = i + 1
            conn.execute(
                "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13',?,'t',1,1.0)",
                (wid, isbn),
            )
    calls = []

    def throttle_once(url):
        calls.append(url)
        if len(calls) == 1:
            raise covers.RateLimited("429")
        return JPEG

    monkeypatch.setattr(covers, "_fetch", throttle_once)
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: mapping)
    monkeypatch.setattr(covers, "THROTTLE_BACKOFF_SECONDS", 0)
    result = covers.backfill(limit=4, allow_remote=True, pause=0)

    assert result["found"] == 3, result
    assert result["deferred"] == 1


def test_sustained_throttling_still_stops_the_run(monkeypatch):
    ids = [_work(f"Book {i}") for i in range(6)]
    mapping = {}
    with db.tx() as conn:
        for i, wid in enumerate(ids):
            isbn = f"978000000000{i}"
            mapping[isbn] = i + 1
            conn.execute(
                "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13',?,'t',1,1.0)",
                (wid, isbn),
            )

    def always_throttled(url):
        raise covers.RateLimited("429")

    monkeypatch.setattr(covers, "_fetch", always_throttled)
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: mapping)
    monkeypatch.setattr(covers, "THROTTLE_BACKOFF_SECONDS", 0)
    result = covers.backfill(limit=6, allow_remote=True, pause=0)

    assert result["found"] == 0
    assert result["deferred"] == 6
    assert db.one("SELECT COUNT(*) n FROM cover_cache")["n"] == 0, "nothing written off under throttling"


def _google_books_returning(volumes_by_isbn, captured=None):
    """A stand-in for Google Books' volumes endpoint, keyed by the isbn: query."""
    import httpx as _h

    def factory(**kwargs):
        def handler(request):
            if captured is not None:
                captured.append(str(request.url))
            q = request.url.params.get("q") or ""
            isbn = q.split(":", 1)[1] if q.startswith("isbn:") else None
            items = volumes_by_isbn.get(isbn) or []
            return _h.Response(200, json={"items": items})
        return _REAL_HTTPX_CLIENT(transport=_h.MockTransport(handler))

    return factory


def test_google_books_is_a_last_resort_fallback_when_openlibrary_has_nothing(monkeypatch):
    """Regression: OpenReads' own Google Books enrichment shares one daily quota
    across the whole library, so a large batch import exhausts it long before
    every work is asked about, and OpenLibrary's own art coverage is not total
    either. bookdoe must be able to find a cover on its own."""
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9781668134719','test',1,1.0)",
            (work_id,),
        )
    monkeypatch.setattr(covers, "settings", dataclasses.replace(covers.settings, google_books_api_key="test-key"))
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: {})  # OpenLibrary: nothing
    volumes = {"9781668134719": [{"volumeInfo": {"imageLinks": {"thumbnail": "http://books.example/cover.jpg?zoom=1&edge=curl"}}}]}
    monkeypatch.setattr(covers.httpx, "Client", _google_books_returning(volumes))

    fetched = []
    real_fetch = covers._fetch

    def spy_fetch(url):
        fetched.append(url)
        return JPEG

    monkeypatch.setattr(covers, "_fetch", spy_fetch)
    result = covers.resolve_cover(work_id, allow_remote=True)

    assert result["source"] == "google_books"
    assert fetched == ["https://books.example/cover.jpg?zoom=0"], "must upgrade to the full-size, curl-free image"


def test_google_books_is_never_consulted_when_no_key_is_configured(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9781668134719','test',1,1.0)",
            (work_id,),
        )
    monkeypatch.setattr(covers, "settings", dataclasses.replace(covers.settings, google_books_api_key=""))
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: {})

    def explode(**kwargs):  # pragma: no cover - asserts it is never reached
        raise AssertionError("Google Books contacted with no API key configured")

    monkeypatch.setattr(covers.httpx, "Client", explode)
    assert covers.resolve_cover(work_id, allow_remote=True)["status"] == "none"


def test_google_books_rate_limit_defers_instead_of_failing(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'isbn13','9781668134719','test',1,1.0)",
            (work_id,),
        )
    monkeypatch.setattr(covers, "settings", dataclasses.replace(covers.settings, google_books_api_key="test-key"))
    monkeypatch.setattr(covers, "lookup_cover_ids", lambda isbns: {})
    monkeypatch.setattr(covers.httpx, "Client", _status_client(429))

    result = covers.resolve_cover(work_id, allow_remote=True)
    assert result["status"] == "deferred"
    assert db.one("SELECT * FROM cover_cache WHERE work_id=?", (work_id,)) is None


def test_manual_cover_is_set_and_served(monkeypatch):
    work_id = _work()
    result = covers.set_manual(work_id, PNG)
    assert result == {"work_id": work_id, "status": "cached", "source": "manual", "content_type": "image/png", "size_bytes": len(PNG)}

    with TestClient(app) as c:
        r = c.get(f"/api/v1/library/{work_id}/cover")
        assert r.content == PNG


def test_manual_cover_survives_a_forced_refresh(monkeypatch):
    """The whole point: a user override must not be silently clobbered by the
    next 'refresh metadata'/'refresh cover' pass, same as a manual text edit."""
    work_id = _work()
    covers.set_manual(work_id, PNG)

    def explode(url):  # pragma: no cover - asserts it is never reached
        raise AssertionError("a provider was contacted for a work with a manual cover")

    monkeypatch.setattr(covers, "_fetch", explode)
    result = covers.resolve_cover(work_id, force=True, allow_remote=True)

    assert result == {"work_id": work_id, "status": "cached", "source": "manual", "cached": True, "remote_used": False}
    assert covers.cover_file(work_id).read_bytes() == PNG


def test_clearing_a_manual_cover_lets_automatic_resolution_run_again(monkeypatch, tmp_path):
    work_id = _work()
    _attach_file(work_id, _epub(tmp_path / "book.epub"))
    covers.resolve_cover(work_id, allow_remote=False)
    assert covers.cached(work_id)["source"] == "embedded"

    covers.set_manual(work_id, JPEG)
    assert covers.cached(work_id)["source"] == "manual"

    covers.clear_manual(work_id)
    assert covers.cached(work_id) is None
    result = covers.resolve_cover(work_id, allow_remote=False)
    assert result["source"] == "embedded"


def test_set_manual_rejects_a_non_image_payload():
    work_id = _work()
    with pytest.raises(ValueError):
        covers.set_manual(work_id, b"not an image")


def test_set_manual_rejects_an_unknown_work():
    with pytest.raises(ValueError):
        covers.set_manual("does-not-exist", PNG)


def test_setting_a_cover_by_url_over_http(monkeypatch):
    work_id = _work()
    monkeypatch.setattr(covers, "_fetch", lambda url: JPEG)

    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/cover", json={"url": "https://example.invalid/cover.jpg"})
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "manual"
        assert c.get(f"/api/v1/library/{work_id}/cover").content == JPEG


def test_setting_a_cover_by_url_reports_an_unfetchable_url(monkeypatch):
    work_id = _work()
    monkeypatch.setattr(covers, "_fetch", lambda url: None)

    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/cover", json={"url": "https://example.invalid/missing.jpg"})
        assert r.status_code == 400


def test_setting_a_cover_by_url_for_an_unknown_work_is_404(monkeypatch):
    monkeypatch.setattr(covers, "_fetch", lambda url: JPEG)
    with TestClient(app) as c:
        r = c.put("/api/v1/library/does-not-exist/cover", json={"url": "https://example.invalid/cover.jpg"})
        assert r.status_code == 404


def test_uploading_a_cover_file_over_http():
    work_id = _work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/cover/upload", content=PNG, headers={"content-type": "image/png"})
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "manual"
        assert c.get(f"/api/v1/library/{work_id}/cover").content == PNG


def test_uploading_a_non_image_is_rejected():
    work_id = _work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/cover/upload", content=b"not an image")
        assert r.status_code == 400


def test_clearing_a_manual_cover_over_http(tmp_path):
    work_id = _work()
    _attach_file(work_id, _epub(tmp_path / "book.epub"))
    with TestClient(app) as c:
        c.put(f"/api/v1/library/{work_id}/cover/upload", content=JPEG, headers={"content-type": "image/jpeg"})
        assert c.get(f"/api/v1/library/{work_id}/cover").content == JPEG

        r = c.delete(f"/api/v1/library/{work_id}/cover")
        assert r.status_code == 200
        assert r.json()["source"] == "embedded"
        assert c.get(f"/api/v1/library/{work_id}/cover").content == PNG
