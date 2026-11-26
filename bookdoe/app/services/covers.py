from __future__ import annotations

import logging
import subprocess
import time
import zipfile
from pathlib import Path
from posixpath import dirname as posix_dirname, join as posix_join, normpath as posix_normpath
from typing import Any
from defusedxml.ElementTree import fromstring as safe_fromstring

import httpx

from .. import db
from ..config import settings

log = logging.getLogger(__name__)

# Covers are cached as bytes on disk and served by bookdoe itself rather than
# handing the browser a third-party URL. A remote <img src> would leak every page
# view of a private library to the cover host, and would break whenever that host
# is unreachable. The bytes are the cache; cover_cache only records provenance.
MAX_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 20.0
OPENLIBRARY_COVERS = "https://covers.openlibrary.org"

# A failed lookup is remembered so the backfill job does not re-ask a provider for
# a cover it has already said it does not have.
MAX_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 7 * 86400

# OpenLibrary throttles covers looked up *by identifier* - /b/isbn/... - to 100
# requests per IP per 5 minutes, which made a 9,000-book backfill an overnight job.
# One search query instead resolves up to ISBN_BATCH ISBNs to cover ids, and the art
# is fetched by id, which is not on that throttled path.
#
# The by-ISBN endpoint is deliberately not kept as a fallback: measured against 25
# works the batched search could not place, it found art for 0 of them. Keeping it
# would have reimposed the rate limit on the whole job to buy nothing.
DEFAULT_BATCH = 400
ISBN_BATCH = 40
OPENLIBRARY_SEARCH = "https://openlibrary.org/search.json"

# One throttled response is a blip, not a reason to abandon 399 other books. Only a
# run of them means the provider actually wants us to stop.
THROTTLE_TOLERANCE = 3
THROTTLE_BACKOFF_SECONDS = 2.0


class RateLimited(Exception):
    """The provider refused the request for volume, not because art is missing.

    Kept distinct from "no cover": recording a throttled response as a failure
    would burn one of the work's three attempts and eventually mark a book that
    has perfectly good art as having none.
    """


class UpstreamUnavailable(Exception):
    """One object could not be served right now - a 5xx or a dropped connection.

    Emphatically NOT throttling. OpenLibrary redirects cover ids to archive.org, and
    individual files there can 503 indefinitely. Treating that as "slow down" aborted
    the whole batch, and because the queue is ordered deterministically the same dead
    file sat at the head of it and killed every subsequent run - the backfill livelocked
    at 30% for hours. A bad object is a problem with that book, not with the provider.
    """

EBOOK_PREFERRED = (".epub",)
AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac"}

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def cover_dir() -> Path:
    path = settings.cover_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def cover_file(work_id: str) -> Path:
    return cover_dir() / work_id


def sniff(data: bytes) -> str | None:
    """Identify an image by its bytes.

    Content-Type headers are not trusted: a provider that answers a missing cover
    with an HTML error page still labels it image/jpeg often enough to poison the
    cache with unrenderable files.
    """
    if not data:
        return None
    for magic, content_type in _MAGIC:
        if data.startswith(magic):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def has_cached(work_id: str) -> bool:
    """Whether a work has art, without touching the filesystem.

    Separate from cached() because that one reconciles a stale row against the disk
    and deletes it if the bytes are gone. That is right when serving one cover and
    wrong when rendering a page of a hundred, where it would be a hundred stat calls
    and a write on the render path.
    """
    return db.one("SELECT 1 FROM cover_cache WHERE work_id=? AND status='cached'", (work_id,)) is not None


def cached(work_id: str) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM cover_cache WHERE work_id=? AND status='cached'", (work_id,))
    if not row:
        return None
    path = cover_file(work_id)
    if not path.exists():
        # The row outlived its bytes (restored DB, wiped cache dir). Treat as absent
        # so the next resolve re-fetches instead of serving a 404 forever.
        with db.tx() as conn:
            conn.execute("DELETE FROM cover_cache WHERE work_id=?", (work_id,))
        return None
    return {**row, "path": path}


def _store(work_id: str, data: bytes, source: str) -> dict[str, Any]:
    content_type = sniff(data)
    if not content_type:
        raise ValueError("payload is not a recognised image")
    if len(data) > MAX_BYTES:
        raise ValueError(f"cover exceeds {MAX_BYTES} bytes")
    path = cover_file(work_id)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO cover_cache(work_id,status,source,content_type,size_bytes,attempts,error,updated_at)
               VALUES (?,'cached',?,?,?,0,NULL,?)
               ON CONFLICT(work_id) DO UPDATE SET status='cached',source=excluded.source,content_type=excluded.content_type,
                 size_bytes=excluded.size_bytes,attempts=0,error=NULL,updated_at=excluded.updated_at""",
            (work_id, source, content_type, len(data), db.now()),
        )
    return {"work_id": work_id, "status": "cached", "source": source, "content_type": content_type, "size_bytes": len(data)}


def _record_failure(work_id: str, error: str | None) -> dict[str, Any]:
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO cover_cache(work_id,status,source,content_type,size_bytes,attempts,error,updated_at)
               VALUES (?,'none',NULL,NULL,0,1,?,?)
               ON CONFLICT(work_id) DO UPDATE SET status='none',attempts=cover_cache.attempts+1,
                 error=excluded.error,updated_at=excluded.updated_at""",
            (work_id, error, db.now()),
        )
    return {"work_id": work_id, "status": "none", "error": error}


def _fetch(url: str) -> bytes | None:
    """Fetch an image with the application's own HTTP client.

    httpx rather than urllib deliberately: cover and metadata hosts sit behind CDNs
    that answer urllib's user agent with 403 (see the OIDC JWKS fix for the same
    failure in a library that did its own HTTP).
    """
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": "bookdoe/0.1"}) as client:
            with client.stream("GET", url) as response:
                if response.status_code in (403, 429):
                    raise RateLimited(f"{response.status_code} from {url}")
                if response.status_code >= 500:
                    raise UpstreamUnavailable(f"{response.status_code} from {url}")
                if response.status_code != 200:
                    return None
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        log.warning("cover at %s exceeds %d bytes; discarded", url, MAX_BYTES)
                        return None
                    chunks.append(chunk)
        data = b"".join(chunks)
        return data if sniff(data) else None
    except (RateLimited, UpstreamUnavailable):
        raise
    except httpx.TransportError as exc:
        # Reset connections and timeouts are the same class of problem as a 5xx.
        raise UpstreamUnavailable(f"{type(exc).__name__} from {url}") from exc
    except Exception as exc:
        log.warning("cover fetch failed for %s: %s", url, exc)
        return None


# ---------- sources ----------
def _from_metadata_url(work: dict[str, Any]) -> tuple[bytes, str] | None:
    url = (work.get("cover_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    data = _fetch(url)
    return (data, "metadata") if data else None


def _work_files(work_id: str) -> list[Path]:
    rows = db.all(
        """SELECT f.path FROM media_files f JOIN media_assets a ON a.id=f.asset_id
           WHERE a.work_id=? AND f.missing=0 ORDER BY COALESCE(f.part_number,1),f.path""",
        (work_id,),
    )
    paths = [Path(r["path"]) for r in rows]
    # An epub carries its cover in a zip entry: cheap and exact. Pulling the art out
    # of an audiobook means spawning ffmpeg, so it is only tried when nothing else works.
    return sorted(paths, key=lambda p: 0 if p.suffix.lower() in EBOOK_PREFERRED else 1)


def _epub_cover(path: Path) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as zf:
            container = safe_fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                return None
            opf_name = rootfile.attrib["full-path"]
            opf = safe_fromstring(zf.read(opf_name))
            base = posix_dirname(opf_name)
            manifest = {item.get("id"): item for item in opf.findall(".//{*}manifest/{*}item")}

            candidates: list[str] = []
            for meta in opf.findall(".//{*}metadata/{*}meta"):
                if (meta.get("name") or "").lower() == "cover" and meta.get("content") in manifest:
                    href = manifest[meta.get("content")].get("href")
                    if href:
                        candidates.append(href)
            for item in manifest.values():
                properties = (item.get("properties") or "").lower()
                href, media_type = item.get("href"), (item.get("media-type") or "").lower()
                if not href or not media_type.startswith("image/"):
                    continue
                if "cover-image" in properties:
                    candidates.insert(0, href)
                elif "cover" in href.lower():
                    candidates.append(href)

            names = set(zf.namelist())
            for href in candidates:
                entry = posix_normpath(posix_join(base, href)) if base else posix_normpath(href)
                if entry not in names:
                    continue
                with zf.open(entry) as fh:
                    data = fh.read(MAX_BYTES + 1)
                if sniff(data) and len(data) <= MAX_BYTES:
                    return data
    except Exception as exc:
        log.debug("epub cover extraction failed for %s: %s", path, exc)
    return None


# `disposition` is its own ffprobe section, not a field of `stream`: asking for
# "stream=disposition" prints nothing at all and exits 0, which silently turned the
# guard below into "no audiobook ever has a cover". The section name is required.
ATTACHED_PIC_ENTRY = "stream_disposition=attached_pic"


def _audio_cover(path: Path) -> bytes | None:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", ATTACHED_PIC_ENTRY, "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        # Only an attached picture is a cover. Without this check a file that happens
        # to carry a real video stream would have an arbitrary frame cached as its art.
        if probe.returncode != 0 or "1" not in (probe.stdout or ""):
            return None
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-c", "copy", "-f", "image2pipe", "-"],
            capture_output=True, timeout=60, check=False,
        )
        data = out.stdout or b""
        if out.returncode == 0 and sniff(data) and len(data) <= MAX_BYTES:
            return data
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("audio cover extraction failed for %s: %s", path, exc)
    return None


def _from_embedded(work_id: str) -> tuple[bytes, str] | None:
    for path in _work_files(work_id):
        if not path.exists():
            continue
        data = _epub_cover(path) if path.suffix.lower() == ".epub" else (_audio_cover(path) if path.suffix.lower() in AUDIO_EXTS else None)
        if data:
            return data, "embedded"
    return None


def work_isbns(work_id: str) -> list[str]:
    rows = db.all(
        "SELECT namespace,value FROM identifiers WHERE entity_type='work' AND entity_id=? AND namespace IN ('isbn13','isbn10')",
        (work_id,),
    )
    out = []
    for row in sorted(rows, key=lambda r: 0 if r["namespace"] == "isbn13" else 1):
        isbn = "".join(ch for ch in str(row["value"]) if ch.isalnum()).upper()
        if len(isbn) in (10, 13):
            out.append(isbn)
    return out


def lookup_cover_ids(isbns: list[str]) -> dict[str, int]:
    """Resolve many ISBNs to OpenLibrary cover ids in as few queries as possible.

    The old /api/books bulk endpoint now 404s; search.json accepts a Solr-style
    disjunction and will return the `cover_i` of each matching edition group, so one
    request covers ISBN_BATCH books. A document lists every ISBN of every edition it
    covers, which is why the mapping back is a set membership test rather than an
    index lookup.
    """
    wanted = [i for i in dict.fromkeys(isbns) if i]
    found: dict[str, int] = {}
    for start in range(0, len(wanted), ISBN_BATCH):
        chunk = wanted[start:start + ISBN_BATCH]
        query = "isbn:(" + " OR ".join(chunk) + ")"
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": "bookdoe/0.1"}) as client:
                response = client.get(OPENLIBRARY_SEARCH, params={"q": query, "limit": ISBN_BATCH * 2, "fields": "isbn,cover_i"})
            if response.status_code in (403, 429):
                raise RateLimited(f"{response.status_code} from search.json")
            if response.status_code != 200:
                continue
            docs = response.json().get("docs") or []
        except RateLimited:
            raise
        except Exception as exc:
            log.warning("cover id lookup failed: %s", exc)
            continue
        remaining = set(chunk)
        for doc in docs:
            cover_id = doc.get("cover_i")
            if not cover_id:
                continue
            listed = {str(x).upper() for x in (doc.get("isbn") or [])}
            for isbn in list(remaining):
                if isbn in listed:
                    found[isbn] = int(cover_id)
                    remaining.discard(isbn)
    return found


def _from_openlibrary(work_id: str, cover_ids: dict[str, int] | None = None) -> tuple[bytes, str] | None:
    isbns = work_isbns(work_id)
    for isbn in isbns:
        cover_id = (cover_ids or {}).get(isbn)
        if not cover_id:
            continue
        data = _fetch(f"{OPENLIBRARY_COVERS}/b/id/{cover_id}-L.jpg")
        if data:
            return data, "openlibrary"
    return None


def _google_books_image_url(volume: dict[str, Any]) -> str | None:
    links = (volume.get("volumeInfo") or {}).get("imageLinks") or {}
    url = links.get("extraLarge") or links.get("large") or links.get("medium") or links.get("thumbnail") or links.get("smallThumbnail")
    if not url:
        return None
    # Google serves a small, curl-cornered thumbnail by default; zoom=0 with the
    # curl edge stripped is the same well-known trick every Google Books cover
    # scraper uses to get the actual jacket art instead of a postage stamp of it.
    return url.replace("http://", "https://").replace("zoom=1", "zoom=0").replace("&edge=curl", "")


def _from_google_books(work_id: str) -> tuple[bytes, str] | None:
    """Independent of whatever OpenReads has (or has not yet) enriched an edition
    with: OpenReads' own Google Books enrichment shares one API key's daily quota
    across the whole library, so a large batch import can exhaust it long before
    every work has been asked about - see the BOOKDOE_GOOGLE_BOOKS_API_KEY
    comment in config.py. This asks again, independently, by ISBN only (never a
    fuzzy title/author search - a wrong cover is worse than a missing one).
    """
    if not settings.google_books_api_key:
        return None
    for isbn in work_isbns(work_id):
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT, headers={"User-Agent": "bookdoe/0.1"}) as client:
                response = client.get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params={"q": f"isbn:{isbn}", "key": settings.google_books_api_key},
                )
        except httpx.TransportError as exc:
            raise UpstreamUnavailable(f"{type(exc).__name__} from Google Books") from exc
        if response.status_code in (403, 429):
            raise RateLimited(f"{response.status_code} from Google Books")
        if response.status_code >= 500:
            raise UpstreamUnavailable(f"{response.status_code} from Google Books")
        if response.status_code != 200:
            continue
        items = response.json().get("items") or []
        if not items:
            continue
        image_url = _google_books_image_url(items[0])
        if not image_url:
            continue
        data = _fetch(image_url)
        if data:
            return data, "google_books"
    return None


def _has_remote_source(work: dict[str, Any]) -> bool:
    if (work.get("cover_url") or "").startswith(("http://", "https://")):
        return True
    return bool(work_isbns(work["id"]))


def resolve_cover(
    work_id: str,
    *,
    force: bool = False,
    allow_remote: bool = True,
    cover_ids: dict[str, int] | None = None,
) -> dict[str, Any]:
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not work:
        raise ValueError("Work not found")
    hit = cached(work_id)
    if hit and (not force or hit["source"] == "manual"):
        # A manually-set cover (see set_manual/main.py's cover-upload endpoints)
        # is never silently replaced by a "refresh metadata"/"refresh cover" pass -
        # the user has to explicitly clear it first, same as a manual text edit.
        return {"work_id": work_id, "status": "cached", "source": hit["source"], "cached": True, "remote_used": False}

    # Art embedded in a file the user already owns is free, needs no network, and is
    # the jacket of the edition they actually have - so it is tried before any
    # provider, not after.
    sources: list[Any] = [lambda wid: _from_embedded(wid)]
    if allow_remote:
        sources.append(lambda wid: _from_metadata_url(work))
        sources.append(lambda wid: _from_openlibrary(wid, cover_ids if cover_ids is not None else lookup_cover_ids(work_isbns(wid))))
        sources.append(lambda wid: _from_google_books(wid))

    remote_used = False
    for index, source in enumerate(sources):
        try:
            found = source(work_id)
        except RateLimited as exc:
            # Leave no cover_cache row at all, so the work stays in the untried
            # queue and the next run picks it up rather than counting a strike.
            log.info("cover lookup deferred for %s: %s", work_id, exc)
            return {"work_id": work_id, "status": "deferred", "error": str(exc), "rate_limited": True, "remote_used": True}
        except UpstreamUnavailable as exc:
            # This source cannot serve this book. Try the next one, and if nothing
            # works record an ordinary failure so the seven-day retry applies - do
            # not hold up the other 399 books in the batch.
            log.info("cover source unavailable for %s: %s", work_id, exc)
            remote_used = True
            continue
        except Exception as exc:
            log.warning("cover source failed for %s: %s", work_id, exc)
            continue
        if index > 0:
            remote_used = True
        if found:
            data, name = found
            return {**_store(work_id, data, name), "remote_used": remote_used}

    if not allow_remote and _has_remote_source(work):
        # Never seen by a provider that might well have its cover. Recording a failure
        # here would spend an attempt the work never actually had.
        return {"work_id": work_id, "status": "deferred", "error": "remote lookup not attempted", "remote_used": False}
    return {**_record_failure(work_id, "no cover found from any source"), "remote_used": remote_used}


def backfill(limit: int = DEFAULT_BATCH, *, allow_remote: bool = True, pause: float = 0.2) -> dict[str, Any]:
    """Resolve covers for works that have never been tried, oldest failures last.

    Works the user actually owns come first. Their art is embedded in a file already
    on disk, so it resolves instantly, without a provider call, and essentially always
    succeeds - where a remote ISBN lookup finds a cover about a third of the time. The
    books on the shelf therefore get their jackets in the first hour rather than after
    the whole wanted list has been asked about.
    """
    cutoff = db.now() - RETRY_AFTER_SECONDS
    rows = db.all(
        """SELECT w.id FROM works w LEFT JOIN cover_cache cc ON cc.work_id=w.id
           WHERE cc.work_id IS NULL
              OR (cc.status='none' AND cc.attempts<? AND cc.updated_at<?)
           ORDER BY
             cc.updated_at IS NOT NULL,
             EXISTS(SELECT 1 FROM media_assets a JOIN media_files f ON f.asset_id=a.id
                    WHERE a.work_id=w.id AND f.missing=0) DESC,
             w.updated_at DESC
           LIMIT ?""",
        (MAX_ATTEMPTS, cutoff, limit),
    )
    # One search round-trip per ISBN_BATCH works, before touching any of them, so the
    # per-work loop mostly fetches art by cover id and never has to ask the throttled
    # by-ISBN endpoint.
    cover_ids: dict[str, int] = {}
    if allow_remote and rows:
        try:
            cover_ids = lookup_cover_ids([isbn for row in rows for isbn in work_isbns(row["id"])])
        except RateLimited as exc:
            log.info("cover id prefetch deferred: %s", exc)
            return {"considered": len(rows), "found": 0, "missing": 0, "deferred": len(rows), "cover_ids": 0}

    found = missing = deferred = remote_calls = 0
    throttles = 0
    for row in rows:
        try:
            result = resolve_cover(row["id"], allow_remote=allow_remote, cover_ids=cover_ids)
        except Exception as exc:
            log.warning("cover backfill failed for %s: %s", row["id"], exc)
            missing += 1
            continue
        status = result.get("status")
        if status == "cached":
            found += 1
            throttles = 0
        elif status == "deferred":
            deferred += 1
            if result.get("rate_limited"):
                throttles += 1
                if throttles >= THROTTLE_TOLERANCE:
                    # Genuinely being asked to slow down. Stop the run; the remaining
                    # works keep their place in the queue and nothing is written off.
                    log.info("cover backfill stopping after %d throttled responses", throttles)
                    deferred = len(rows) - (found + missing)
                    break
                time.sleep(THROTTLE_BACKOFF_SECONDS * throttles)
            continue
        else:
            missing += 1
            throttles = 0
        if result.get("remote_used"):
            remote_calls += 1
            if pause:
                time.sleep(pause)
    return {
        "considered": len(rows), "found": found, "missing": missing,
        "deferred": deferred, "cover_ids": len(cover_ids), "remote_calls": remote_calls,
    }


def set_manual(work_id: str, data: bytes) -> dict[str, Any]:
    """User-supplied cover art: from a URL they found themselves or a file they
    uploaded, for a book none of the automatic sources found (or found wrong).
    Stored with source='manual' so resolve_cover() never silently replaces it."""
    if not db.one("SELECT 1 FROM works WHERE id=?", (work_id,)):
        raise ValueError("Work not found")
    return _store(work_id, data, "manual")


def fetch_manual(url: str) -> bytes:
    """Fetch a user-supplied cover URL. Raises ValueError on anything that is not
    a usable image, so the caller can report it back rather than caching junk."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must be http(s)")
    data = _fetch(url)
    if not data:
        raise ValueError("Could not fetch a usable image from that URL")
    return data


def clear_manual(work_id: str) -> None:
    """Remove a manually-set cover and let it fall back to automatic resolution
    on the next refresh."""
    with db.tx() as conn:
        conn.execute("DELETE FROM cover_cache WHERE work_id=? AND source='manual'", (work_id,))
    path = cover_file(work_id)
    if path.exists():
        path.unlink()


def stats() -> dict[str, Any]:
    row = db.one(
        """SELECT (SELECT COUNT(*) FROM works) works,
                  (SELECT COUNT(*) FROM cover_cache WHERE status='cached') cached,
                  (SELECT COUNT(*) FROM cover_cache WHERE status='none') without"""
    )
    return row or {}


THUMB_SIZES = (96, 192, 384, 768)


def thumbnail(work_id: str, source: Path, size: int) -> Path | None:
    """A scaled copy of the cover (longest side `size`, snapped to THUMB_SIZES) for
    constrained devices, produced once with ffmpeg (present in the image) and kept
    beside the original. None when ffmpeg is unavailable or fails: the caller serves
    the original, so a missing scaler never costs the cover itself."""
    import shutil
    import subprocess

    snapped = next((s for s in THUMB_SIZES if s >= size), THUMB_SIZES[-1])
    target = cover_dir() / f"{work_id}.{snapped}.jpg"
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    if not shutil.which("ffmpeg"):
        return None
    tmp = target.with_suffix(".tmp.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-vf", f"scale='min({snapped},iw)':'min({snapped},ih)':force_original_aspect_ratio=decrease", "-q:v", "4", "-f", "mjpeg", str(tmp)],
            check=True, capture_output=True, timeout=20,
        )
        tmp.replace(target)
        return target
    except (OSError, subprocess.SubprocessError):
        tmp.unlink(missing_ok=True)
        return None
