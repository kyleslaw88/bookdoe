"""openreads: a drop-in metadata backend for Readarr, API-compatible with
rreading-glasses' contract, backed by Open Library + Google Books instead
of the now-unreliable Goodreads AppSync API.

Endpoints mirror internal.* schemas from rreading-glasses' own swagger.json
(fetched live from a running instance to get field names/types exactly
right) -- see /search, /bulk, /author/{id}, /work/{id}, /book/{id},
/book/isbn/{isbn}, /book/asin/{asin}, /series/{id}.
"""
import logging
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import db
import openlibrary as ol
import resources as res
import v2
import v2db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openreads")

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    v2db.init_db()
    log.info("openreads started, legacy and v2 stores initialized")
    yield


app = FastAPI(title="openreads", description="Provider-independent bibliographic metadata platform with Readarr compatibility", lifespan=lifespan)
app.include_router(v2.router)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/ui/assets", StaticFiles(directory=STATIC_DIR), name="ui-assets")

@app.get("/ui", include_in_schema=False)
def metadata_studio():
    return FileResponse(STATIC_DIR / "index.html")



@app.get("/health")
def health():
    return {"status": "ok"}


def _get_or_create_edition_for_search_doc(doc: dict, work_id: int) -> int:
    edition_key = doc.get("cover_edition_key") or (doc.get("edition_key") or [None])[0]
    if edition_key:
        return db.get_or_create_edition(edition_key, work_id, doc.get("title"))
    # No edition in the search index yet -- synthesize a placeholder tied
    # to the work so downstream /bulk and /book/{id} calls still resolve.
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM editions WHERE work_id=? AND ol_key IS NULL", (work_id,)
        ).fetchone()
    if row:
        return row["id"]
    return db.get_or_create_edition(None, work_id, doc.get("title"))


@app.get("/search")
def search(q: str = Query(..., description="the query string")):
    try:
        # Every result here becomes a full sequential author+work hydration
        # by the Readarr client (no batching on that side) -- for a common
        # phrase like "war and peace" that resolves to a dozen-plus distinct
        # authors, 20 results means 20+ round trips to Open Library, one at
        # a time. Capping lower keeps interactive search inside the client's
        # own timeout at the cost of some long-tail candidates.
        docs = ol.search(q, limit=8)
    except Exception as e:
        log.warning("search upstream error for %r: %s", q, e)
        raise HTTPException(status_code=502, detail=f"Open Library search failed: {e}")

    out = []
    for doc in docs:
        work_key = doc.get("key")
        if not work_key:
            continue
        work_id = db.get_or_create_work(work_key.rstrip("/").split("/")[-1], doc.get("title"))

        author_keys = doc.get("author_key") or []
        author_names = doc.get("author_name") or []
        if not author_keys:
            # Readarr's client does a non-nullable GroupBy on author.id
            # across the whole result set -- one entry with no author
            # throws a NullReferenceException that fails the ENTIRE
            # search, not just this row. Skip works OL has no author for.
            continue
        author_id = db.get_or_create_author(
            author_keys[0], author_names[0] if author_names else None
        )
        db.link_work_author(work_id, author_id)

        edition_id = _get_or_create_edition_for_search_doc(doc, work_id)

        out.append(
            {
                "author": {"id": author_id},
                "bookId": edition_id,
                "workId": work_id,
            }
        )
    return out


def _parse_ids(id: Optional[List[str]]) -> List[int]:
    """Accept both repeated ?id=1&id=2 and a single csv ?id=1,2,3 -- real
    Readarr builds have used both conventions across versions."""
    if not id:
        return []
    out = []
    for v in id:
        for part in str(v).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out


def _bulk_handler(id):
    ids = _parse_ids(id)
    works_out = res.build_works_bulk(ids)
    authors_seen = {}
    for w in works_out:
        for a in w["Authors"]:
            authors_seen[a["ForeignId"]] = a
    res._refresh_series_titles(works_out)
    return {
        "Authors": list(authors_seen.values()),
        "Series": res.merge_series(w["Series"] for w in works_out),
        "Works": works_out,
    }


@app.get("/bulk")
def bulk(id: Optional[List[str]] = Query(None)):
    return _bulk_handler(id)


@app.get("/book/bulk")
def book_bulk(id: Optional[List[str]] = Query(None)):
    """Alias -- some Readarr builds call /book/bulk instead of /bulk."""
    return _bulk_handler(id)


async def _extract_posted_ids(request: Request) -> List[str]:
    """Readarr's BookInfoProxy POSTs to /book/bulk rather than GET-ing it.
    Be liberal about where the ids might be: query string, form body, or
    a raw JSON array/object body -- observed client behavior varies by
    Readarr build."""
    ids = list(request.query_params.getlist("id"))
    if ids:
        return ids
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, list):
            return [str(x) for x in body]
        if isinstance(body, dict) and "id" in body:
            v = body["id"]
            return v if isinstance(v, list) else [str(v)]
    try:
        form = await request.form()
        if form:
            return list(form.getlist("id"))
    except Exception:
        pass
    body_bytes = await request.body()
    if body_bytes:
        return [p for p in body_bytes.decode(errors="ignore").split(",") if p.strip()]
    return []


@app.post("/bulk")
async def bulk_post(request: Request):
    ids = await _extract_posted_ids(request)
    return _bulk_handler(ids)


@app.post("/book/bulk")
async def book_bulk_post(request: Request):
    ids = await _extract_posted_ids(request)
    return _bulk_handler(ids)


@app.get("/author/{author_id}")
def get_author(author_id: int):
    a = res.build_author_resource(author_id)
    if a is None:
        raise HTTPException(status_code=404, detail="author not found")
    return a


@app.delete("/author/{author_id}")
def refresh_author(author_id: int, full: bool = False):
    row = db.author_by_id(author_id)
    if row is None:
        raise HTTPException(status_code=404, detail="author not found")
    author_id = row["id"]  # canonical id for duplicate-key authors
    db.clear_author_cache(author_id)
    res.build_author_resource(author_id, force=True)
    return {"status": "refreshed"}


@app.get("/work/{work_id}")
def get_work(work_id: int):
    w = res.build_work_resource(work_id, hydrate_editions=True)
    if w is None:
        raise HTTPException(status_code=404, detail="work not found")
    return w


@app.get("/book/{edition_id}")
def get_book(edition_id: int):
    """'Confusingly an edition's metadata is the same format as a work's.'
    -- rreading-glasses' own docs. We resolve the edition's parent work
    and return the full work shape, matching that behavior."""
    e = db.edition_by_id(edition_id)
    if e is None:
        raise HTTPException(status_code=404, detail="edition not found")
    w = res.build_work_resource(e["work_id"], hydrate_editions=True)
    if w is None:
        raise HTTPException(status_code=404, detail="work not found")
    return w


@app.get("/book/isbn/{isbn}")
def book_by_isbn(isbn: str):
    e = db.edition_by_isbn(isbn)
    if e:
        return e["id"]
    data = ol.find_edition_by_isbn(isbn)
    if not data:
        raise HTTPException(status_code=404, detail="isbn not found")
    work_id = None
    if data.get("work_ol_key"):
        work_id = db.get_or_create_work(data["work_ol_key"])
    edition_id = db.get_or_create_edition(data["ol_key"], work_id, data["title"])
    db.update_edition(
        edition_id,
        isbn13=data.get("isbn13"),
        isbn10=data.get("isbn10"),
        publisher=data.get("publisher"),
        release_date=data.get("release_date"),
        num_pages=data.get("num_pages"),
        image_url=data.get("image_url"),
    )
    return edition_id


@app.get("/book/asin/{asin}")
def book_by_asin(asin: str):
    e = db.edition_by_asin(asin)
    if e:
        return e["id"]
    raise HTTPException(status_code=404, detail="asin not found (Open Library has no ASIN index)")


@app.get("/series/{series_id}")
def get_series(series_id: int):
    s = res.build_series_resource(series_id)
    if s is None:
        raise HTTPException(status_code=404, detail="series not found")
    return s


@app.get("/recommended")
def recommended():
    return {"workIds": []}
