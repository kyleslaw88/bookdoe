from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

import db
import openlibrary as ol

log = logging.getLogger(__name__)
import resources as res
import v2db
import llm
import googlebooks as gb
import audiobookdb as abdb

router = APIRouter(prefix="/api/v2", tags=["OpenReads v2"])
API_KEY = os.getenv("OPENREADS_V2_API_KEY", "")


def require_api_key(authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None)):
    if not API_KEY:
        return
    supplied = x_api_key or ((authorization or "").removeprefix("Bearer ") if authorization else "")
    if supplied != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


class ResolveRequest(BaseModel):
    identifiers: dict[str, str] = Field(default_factory=dict)
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    # Callers doing a large batch can turn the upstream lookup off to stay purely on
    # cached data.
    upstream: bool = True


class DecisionRequest(BaseModel):
    value: Any
    lock: bool = True
    source: str = "user"
    method: str = "manual"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    audit_id: str | None = None


class LLMResolveRequest(BaseModel):
    subject: dict[str, Any]
    candidates: list[dict[str, Any]]


@router.get("/health")
def health(_: None = Depends(require_api_key)):
    return {"status": "ok", "version": 2, "llm_enabled": llm.ENABLED}


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=50), _: None = Depends(require_api_key)):
    local = v2db.search_local(q, limit=limit)
    # Short-circuit only when the cache can actually satisfy what was asked for. The
    # old rule capped at 8, so a caller asking for 30 got the same 11 cached rows no
    # matter what - which made a "More results" control impossible to honour, because
    # raising the limit never reached upstream.
    if len(local) >= limit:
        return {"items": local[:limit], "source": "local"}
    try:
        # Ask upstream for as much as the caller wants, not a fixed 20: after
        # de-duplication against the cache a 20-doc page routinely collapses to
        # fewer than the requested limit, which stalls paging short of the answer.
        docs = ol.search(q, limit=min(limit, 50))
    except Exception as exc:
        if local:
            return {"items": local[:limit], "source": "local", "warning": f"upstream search failed: {exc}"}
        raise HTTPException(status_code=502, detail=f"Open Library search failed: {exc}")
    seen = {x["id"] for x in local if x}
    items = list(local)

    # Search does NOT hydrate each result from Open Library. Doing so cost a round
    # trip per result - twenty seconds for an uncached search of a prolific author -
    # to fetch a description and series that a result list does not even show.
    # (Measured: running those fetches concurrently is *slower* still, 5.0s against
    # 1.9s for eight works, because Open Library throttles parallel reads.) The search
    # document already carries the title and authors, which is what the list needs;
    # get_work() hydrates the one the user actually opens.
    for item in ingest_search_docs(docs, max(0, limit - len(items))):
        if item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)
    return {"items": items[:limit], "source": "local+openlibrary"}


def ingest_search_docs(docs, limit: int) -> list[dict]:
    """Turn Open Library search documents into canonical works.

    Shared by search and resolve. Creates the local rows and mirrors them; it does
    NOT hydrate each work from Open Library - see the note in search().
    """
    wanted = []
    for doc in docs:
        if len(wanted) >= limit:
            break
        key = doc.get("key")
        if not key:
            continue
        legacy_work_id = db.get_or_create_work(key.rstrip("/").split("/")[-1], doc.get("title"))
        author_keys = doc.get("author_key") or []
        author_names = doc.get("author_name") or []
        for i, akey in enumerate(author_keys):
            aid = db.get_or_create_author(akey, author_names[i] if i < len(author_names) else None)
            db.link_work_author(legacy_work_id, aid)
        wanted.append(legacy_work_id)

    out = []
    for legacy_work_id in wanted:
        cid = v2db.sync_work(legacy_work_id)
        item = v2db.canonical_work(cid) if cid else None
        if item:
            out.append(item)
    return out




@router.get("/contributors/{contributor_id}")
def get_contributor(contributor_id: str, refresh: bool = False, _: None = Depends(require_api_key)):
    item = v2db.canonical_contributor(contributor_id)
    if not item:
        raise HTTPException(status_code=404, detail="Contributor not found")
    try:
        with db.get_conn() as conn:
            legacy_row = conn.execute("SELECT legacy_id FROM v2_legacy_map WHERE entity_type='contributor' AND canonical_id=?", (contributor_id,)).fetchone()
        if legacy_row:
            res.ensure_author(legacy_row["legacy_id"], force=refresh)
            res.ensure_author_works(legacy_row["legacy_id"], force=refresh)
            for work in db.works_for_author(legacy_row["legacy_id"]):
                try:
                    res.ensure_work(work["id"])
                except Exception:
                    pass
                v2db.sync_work(work["id"])
            item = v2db.canonical_contributor(contributor_id) or item
    except Exception:
        pass
    return item


@router.get("/contributors/{contributor_id}/works")
def contributor_works(contributor_id: str, refresh: bool = False, _: None = Depends(require_api_key)):
    get_contributor(contributor_id, refresh=refresh, _=_)
    return {"items": v2db.works_for_contributor(contributor_id)}

@router.get("/works/{work_id}")
def get_work(work_id: str, _: None = Depends(require_api_key)):
    item = v2db.canonical_work(work_id)
    if not item:
        raise HTTPException(status_code=404, detail="Work not found")
    # Hydrate legacy editions lazily, preserving the mature v1 caching/rate-limit
    # behavior, then mirror them into canonical v2 identities.
    try:
        with db.get_conn() as conn:
            legacy_row = conn.execute("SELECT legacy_id FROM v2_legacy_map WHERE entity_type='work' AND canonical_id=?", (work_id,)).fetchone()
        if legacy_row:
            res.ensure_work(legacy_row["legacy_id"])
            # Mirror the work itself, not just its editions. ensure_work() can fill a
            # description (from Open Library, or from Google Books when OL has none),
            # and without this the canonical record never sees it - the enrichment
            # landed in the legacy row and stopped there.
            v2db.sync_work(legacy_row["legacy_id"])
            res.ensure_work_editions(legacy_row["legacy_id"])
            for ed in db.editions_for_work(legacy_row["legacy_id"]):
                v2db.sync_edition(ed["id"])
            item = v2db.canonical_work(work_id) or item
    except Exception:
        pass
    item["editions"] = v2db.editions_for_work(work_id)
    return item


@router.post("/works/{work_id}/audience")
def work_audience(work_id: str, force: bool = False, _: None = Depends(require_api_key)):
    """Fetch (once per provider, cached) and return a work's audience signals: Open Library
    subjects and Google Books categories / maturityRating. The one call bookdoe's
    audience backfill makes per work."""
    with db.get_conn() as conn:
        legacy_row = conn.execute("SELECT legacy_id FROM v2_legacy_map WHERE entity_type='work' AND canonical_id=?", (work_id,)).fetchone()
    if not legacy_row:
        raise HTTPException(status_code=404, detail="Work not found")
    result = res.ensure_work_audience(legacy_row["legacy_id"], force=force)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Work not found")
    return result


@router.get("/works/{work_id}/editions")
def get_editions(work_id: str, _: None = Depends(require_api_key)):
    get_work(work_id, _)
    return {"items": v2db.editions_for_work(work_id)}


@router.post("/resolve")
def resolve(req: ResolveRequest, _: None = Depends(require_api_key)):
    # Exact identifiers win. No LLM participates in identifier lookup.
    for namespace in ("isbn13", "isbn10", "asin", "audible", "openlibrary_edition", "openlibrary_work"):
        value = req.identifiers.get(namespace)
        if not value:
            continue
        hit = v2db.find_identifier(namespace, value)
        if hit:
            if hit["entity_type"] == "work":
                return {"match": v2db.canonical_work(hit["entity_id"]), "method": "exact_identifier", "confidence": 1.0}
            if hit["entity_type"] == "edition":
                with db.get_conn() as conn:
                    ed = conn.execute("SELECT work_id FROM v2_editions WHERE id=?", (hit["entity_id"],)).fetchone()
                if ed:
                    return {"match": v2db.canonical_work(ed["work_id"]), "edition_id": hit["entity_id"], "method": "exact_identifier", "confidence": 1.0}
    if req.title:
        def score_all(candidates):
            scored = []
            for c in candidates:
                # A tolerant fuzzy comparison, not an exact-string one: an exact
                # title/author match still scores exactly as before (1.0 -> 0.75,
                # plus 0.25 for the author), but a real partial match - a study
                # guide, an omnibus, "J.K. Rowling" vs "J. K. Rowling" - now
                # earns credit too, instead of tying with pure noise at 0.
                score = 0.75 * abdb._title_score(req.title, c["title"] or "")
                names = [x["name"] for x in c.get("contributors", []) if x.get("role") == "author"]
                if any(abdb.same_person(a, n) for a in req.authors for n in names):
                    score += 0.25
                scored.append((score, c))
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored

        scored = score_all(v2db.search_local(req.title, limit=10))

        # Resolution used to consult the local cache and nothing else, so a book that
        # had never been searched for resolved to "no candidates" - which is why an
        # entire imported library sat unresolved with nothing to choose from. When the
        # cache cannot produce a confident answer, ask upstream once.
        if req.upstream and (not scored or scored[0][0] < 0.95):
            query = " ".join(x for x in [req.title, next(iter(req.authors), None)] if x)
            try:
                docs = ol.search(query, limit=10)
                fresh = ingest_search_docs(docs, 10)
            except Exception as exc:
                fresh = []
                log.warning("upstream resolve search failed for %r: %s", query, exc)
            if fresh:
                seen_ids = {c["id"] for _, c in scored}
                scored = score_all([c for _, c in scored] + [f for f in fresh if f["id"] not in seen_ids])

        if scored and scored[0][0] >= 0.95:
            return {"match": scored[0][1], "method": "deterministic", "confidence": scored[0][0]}
        # Below this, a candidate is indistinguishable from two unrelated titles
        # that merely share common words ("the", "a", "of") - measured against a
        # set of genuinely unrelated title pairs, the highest such coincidental
        # score was 0.32 (0.43 raw ratio); every real partial match measured
        # (study guides, omnibuses, alternate editions) scored 0.48+. OpenLibrary's
        # own search falls back to some loosely "relevant" result rather than an
        # empty one when nothing good exists (a query for an obscure self-published
        # title once came back with an unrelated 1884 local-history book) -
        # surfacing that as a "suggested candidate" is worse than surfacing
        # nothing, so anything below the real-match floor is dropped rather than
        # shown as if it were weak evidence.
        MIN_CANDIDATE_SCORE = 0.35
        candidates = [c for score, c in scored if score >= MIN_CANDIDATE_SCORE][:5]
        return {"match": None, "method": "review", "confidence": scored[0][0] if scored else 0, "candidates": candidates}
    return {"match": None, "method": "no_evidence", "confidence": 0}


@router.get("/metadata/{entity_type}/{entity_id}")
def get_metadata(entity_type: str, entity_id: str, _: None = Depends(require_api_key)):
    return v2db.metadata(entity_type, entity_id)


@router.put("/metadata/{entity_type}/{entity_id}/{field}")
def put_metadata(entity_type: str, entity_id: str, field: str, req: DecisionRequest, _: None = Depends(require_api_key)):
    v2db.set_decision(entity_type, entity_id, field, req.value, lock=req.lock, source=req.source, method=req.method, confidence=req.confidence)
    if req.audit_id:
        v2db.mark_llm_audit_accepted(req.audit_id)
    return {"ok": True, "metadata": v2db.metadata(entity_type, entity_id)}




@router.get("/providers")
def providers(_: None = Depends(require_api_key)):
    return {
        "items": [
            {"id": "openlibrary", "name": "Open Library", "enabled": True, "role": "graph + primary bibliographic source"},
            {"id": "google_books", "name": "Google Books", "enabled": bool(gb.API_KEY), "role": "edition enrichment"},
            {"id": "audiobookdb", "name": "AudiobookDB", "enabled": bool(abdb.ENABLED), "role": "audiobook series enrichment"},
            {"id": "local_llm", "name": "Local LLM", "enabled": bool(llm.ENABLED and llm.MODEL), "role": "non-authoritative reconciliation"},
        ]
    }


@router.get("/stats")
def stats(_: None = Depends(require_api_key)):
    with db.get_conn() as conn:
        counts = {
            "contributors": conn.execute("SELECT COUNT(*) FROM v2_contributors").fetchone()[0],
            "works": conn.execute("SELECT COUNT(*) FROM v2_works").fetchone()[0],
            "editions": conn.execute("SELECT COUNT(*) FROM v2_editions").fetchone()[0],
            "series": conn.execute("SELECT COUNT(*) FROM v2_series").fetchone()[0],
            "observations": conn.execute("SELECT COUNT(*) FROM v2_metadata_observations").fetchone()[0],
            "locked_fields": conn.execute("SELECT COUNT(*) FROM v2_metadata_decisions WHERE locked=1").fetchone()[0],
        }
    return counts


@router.post("/metadata/{entity_type}/{entity_id}/suggest")
def suggest_metadata(entity_type: str, entity_id: str, _: None = Depends(require_api_key)):
    meta = v2db.metadata(entity_type, entity_id)
    if not meta["observations"]:
        return {"enabled": llm.ENABLED, "suggestions": [], "review_required": True, "summary": "No observations available."}
    entity = {"entity_type": entity_type, "entity_id": entity_id}
    if entity_type == "work":
        entity["record"] = v2db.canonical_work(entity_id)
    result = llm.suggest_observations(entity, meta["observations"])
    if result.get("enabled"):
        audit_id = v2db.record_llm_audit(entity_type, entity_id, llm.MODEL, "metadata_observation_selection", {"entity": entity, "observations": meta["observations"]}, result)
        result["audit_id"] = audit_id
    return result

@router.post("/reconcile/llm")
def reconcile_with_llm(req: LLMResolveRequest, _: None = Depends(require_api_key)):
    return llm.reconcile(req.subject, req.candidates)
