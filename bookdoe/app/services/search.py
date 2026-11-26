from __future__ import annotations

from typing import Any

from .. import db
from .search_normalize import normalize_query

# Lower is better. Identifier hits are the most specific signal available; author and
# series matches are the least specific since they surface every work tied to the name.
RANK_IDENTIFIER = 0
RANK_TITLE_EXACT = 1
RANK_TITLE_PARTIAL = 2
RANK_AUTHOR = 3
RANK_SERIES = 4

# Tie-break within a rank: owned beats wanted beats everything else, matching the
# Library's own owned > wanted > catalog precedence (see main.STATUS_ON_INTENT).
TIER_OWNED = 0
TIER_WANTED = 1
TIER_OTHER = 2


def _tier(owned_count: int, wanted_count: int) -> int:
    if owned_count > 0:
        return TIER_OWNED
    if wanted_count > 0:
        return TIER_WANTED
    return TIER_OTHER


def _work_result(work_id: str, rank: int) -> dict[str, Any] | None:
    w = db.one("SELECT id, title FROM works WHERE id=?", (work_id,))
    if not w:
        return None
    authors = db.all(
        """SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id
           WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord""",
        (work_id,),
    )
    series = db.all(
        """SELECT s.name FROM series_memberships sm JOIN series s ON s.id=sm.series_id
           WHERE sm.work_id=?""",
        (work_id,),
    )
    identifiers = db.all(
        "SELECT namespace, value FROM identifiers WHERE entity_type='work' AND entity_id=?",
        (work_id,),
    )
    counts = db.one(
        """SELECT COUNT(*) media_count,
                  SUM(CASE WHEN status='owned' THEN 1 ELSE 0 END) owned_count,
                  SUM(CASE WHEN has_acquisition_intent=1 AND monitored=1 THEN 1 ELSE 0 END) wanted_count
           FROM media_assets WHERE work_id=? AND derived_from_asset_id IS NULL""",
        (work_id,),
    )
    owned_count = counts["owned_count"] or 0
    wanted_count = counts["wanted_count"] or 0
    return {
        "id": work_id,
        "type": "work",
        "title": w["title"],
        "authors": [a["name"] for a in authors],
        "series": [s["name"] for s in series],
        "identifiers": [{"namespace": i["namespace"], "value": i["value"]} for i in identifiers],
        "media_count": counts["media_count"] or 0,
        "owned_count": owned_count,
        "wanted_count": wanted_count,
        "_rank": rank,
        "_tier": _tier(owned_count, wanted_count),
    }


def _author_result(contributor_id: str, name: str) -> dict[str, Any]:
    counts = db.one(
        """SELECT COUNT(DISTINCT wc.work_id) media_count,
                  COUNT(DISTINCT CASE WHEN a.status='owned' THEN wc.work_id END) owned_count,
                  COUNT(DISTINCT CASE WHEN a.has_acquisition_intent=1 AND a.monitored=1 THEN wc.work_id END) wanted_count
           FROM work_contributors wc LEFT JOIN media_assets a ON a.work_id=wc.work_id
           WHERE wc.contributor_id=? AND wc.role='author'""",
        (contributor_id,),
    )
    owned_count = counts["owned_count"] or 0
    wanted_count = counts["wanted_count"] or 0
    return {
        "id": contributor_id,
        "type": "author",
        "title": name,
        "authors": [],
        "series": [],
        "identifiers": [],
        "media_count": counts["media_count"] or 0,
        "owned_count": owned_count,
        "wanted_count": wanted_count,
        "_rank": RANK_AUTHOR,
        "_tier": _tier(owned_count, wanted_count),
    }


def _series_result(series_id: str, name: str) -> dict[str, Any]:
    counts = db.one(
        """SELECT COUNT(DISTINCT sm.work_id) media_count,
                  COUNT(DISTINCT CASE WHEN a.status='owned' THEN sm.work_id END) owned_count,
                  COUNT(DISTINCT CASE WHEN a.has_acquisition_intent=1 AND a.monitored=1 THEN sm.work_id END) wanted_count
           FROM series_memberships sm LEFT JOIN media_assets a ON a.work_id=sm.work_id
           WHERE sm.series_id=?""",
        (series_id,),
    )
    owned_count = counts["owned_count"] or 0
    wanted_count = counts["wanted_count"] or 0
    return {
        "id": series_id,
        "type": "series",
        "title": name,
        "authors": [],
        "series": [],
        "identifiers": [],
        "media_count": counts["media_count"] or 0,
        "owned_count": owned_count,
        "wanted_count": wanted_count,
        "_rank": RANK_SERIES,
        "_tier": _tier(owned_count, wanted_count),
    }


def search_local(query: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Search the owned library, wanted/followed catalog, local catalog, authors,
    series, and identifiers -- provider-independent, no federation.

    The query is normalized (punctuation, unicode, initials, volume markers, etc.)
    before matching. Results are ranked identifier match > exact title > partial
    title > author > series, with owned works breaking ties ahead of wanted, ahead
    of everything else.
    """
    q = normalize_query(query)
    if not q:
        return {"results": [], "total": 0, "limit": limit, "offset": offset}
    like = f"%{q}%"

    work_ranks: dict[str, int] = {}

    def note(work_id: str, rank: int) -> None:
        if work_id not in work_ranks or rank < work_ranks[work_id]:
            work_ranks[work_id] = rank

    for r in db.all("SELECT entity_type, entity_id FROM identifiers WHERE value=?", (q,)):
        if r["entity_type"] == "work":
            note(r["entity_id"], RANK_IDENTIFIER)
        elif r["entity_type"] == "edition":
            edition = db.one("SELECT work_id FROM editions WHERE id=?", (r["entity_id"],))
            if edition:
                note(edition["work_id"], RANK_IDENTIFIER)

    for r in db.all("SELECT id FROM works WHERE title=? COLLATE NOCASE", (q,)):
        note(r["id"], RANK_TITLE_EXACT)

    for r in db.all("SELECT id FROM works WHERE title LIKE ?", (like,)):
        note(r["id"], RANK_TITLE_PARTIAL)

    for r in db.all(
        """SELECT DISTINCT wc.work_id FROM work_contributors wc
           JOIN contributors c ON c.id=wc.contributor_id
           WHERE wc.role='author' AND c.name LIKE ?""",
        (like,),
    ):
        note(r["work_id"], RANK_AUTHOR)

    for r in db.all(
        """SELECT DISTINCT sm.work_id FROM series_memberships sm
           JOIN series s ON s.id=sm.series_id WHERE s.name LIKE ?""",
        (like,),
    ):
        note(r["work_id"], RANK_SERIES)

    results = [res for wid, rank in work_ranks.items() if (res := _work_result(wid, rank))]
    results += [_author_result(r["id"], r["name"]) for r in db.all("SELECT id, name FROM contributors WHERE name LIKE ?", (like,))]
    results += [_series_result(r["id"], r["name"]) for r in db.all("SELECT id, name FROM series WHERE name LIKE ?", (like,))]

    results.sort(key=lambda r: (r["_rank"], r["_tier"], r["title"].casefold()))
    total = len(results)
    page = results[offset : offset + limit]
    for r in page:
        del r["_rank"]
        del r["_tier"]
    return {"results": page, "total": total, "limit": limit, "offset": offset}
