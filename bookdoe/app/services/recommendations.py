from __future__ import annotations

import logging
import re
from typing import Any

from .. import db
from . import llmclient, tagging

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 24 * 3600
_STOPWORDS = frozenset(
    "the a an of to and in on for with is was were are be been by at from as it "
    "this that his her their they he she you your our we but not into out over".split()
)

LLM_SYSTEM_PROMPT = (
    "You recommend books similar to one the reader owns. Respond with strict JSON "
    'only: {"items":[{"title":"...","author":"...","reason":"one short sentence"}]}. '
    "Suggest up to 6 real, specific published books (not the book given to you, and "
    "not a series entry they would obviously already own if they own this one). "
    "Prefer books that share tone, themes, or audience over books that merely share a genre label."
)


def _word_set(text: str) -> set[str]:
    words = re.findall(r"[a-z']+", (text or "").lower())
    return {w for w in words if len(w) > 3 and w not in _STOPWORDS}


def _heuristic_candidates(work: dict[str, Any], *, kids_only: bool, viewer: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Score every other work in the local catalog by how much it has in common.

    Series and author are the strongest signals a personal library can offer without
    an LLM; a description-word overlap breaks ties among the rest. Everything here is
    local, deterministic, and free - it is also the mandatory path for a kids-scope
    viewer, who must never be shown an LLM's open-ended, unvetted suggestions.
    """
    series_ids = {s["id"] for s in db.all("SELECT series_id id FROM series_memberships WHERE work_id=?", (work["id"],))}
    author_ids = {
        a["id"]
        for a in db.all(
            "SELECT contributor_id id FROM work_contributors WHERE work_id=? AND role='author'", (work["id"],)
        )
    }
    tag_ids = {t["id"] for t in tagging.work_tags(work["id"])}
    own_words = _word_set(f"{work['title']} {work.get('description') or ''}")

    where = "w.id!=?"
    params: list[Any] = [work["id"]]
    if kids_only:
        # The real viewer: since grants exist, a kid's shelf is per account, not one shelf.
        clause, vparams = tagging.kids_visibility(viewer or {'access_scope': 'kids', 'id': ''})
        where += f" AND {clause}"
        params = (*params, *vparams)
    candidates = db.all(f"SELECT w.* FROM works w WHERE {where} LIMIT 4000", tuple(params))  # nosec B608

    scored = []
    for c in candidates:
        score = 0.0
        c_series = {s["id"] for s in db.all("SELECT series_id id FROM series_memberships WHERE work_id=?", (c["id"],))}
        if series_ids & c_series:
            score += 5.0
        c_authors = {
            a["id"]
            for a in db.all("SELECT contributor_id id FROM work_contributors WHERE work_id=? AND role='author'", (c["id"],))
        }
        if author_ids & c_authors:
            score += 3.0
        c_tags = {t["id"] for t in tagging.work_tags(c["id"])}
        # A single shared 'kids' tag on every candidate would otherwise dominate the
        # score for every recommendation on the kids shelf and drown out everything else.
        shared_tags = (tag_ids & c_tags) - {tagging.kids_tag_id()} if tag_ids or c_tags else set()
        score += 1.5 * len(shared_tags)
        overlap = own_words & _word_set(f"{c['title']} {c.get('description') or ''}")
        score += min(len(overlap), 8) * 0.15
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:8]]


def _summary_of(work: dict[str, Any]) -> dict[str, Any]:
    authors = db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author'",
        (work["id"],),
    )
    return {"work_id": work["id"], "title": work["title"], "authors": [a["name"] for a in authors], "in_library": True}


def _match_llm_suggestion(item: dict[str, Any]) -> dict[str, Any]:
    title = (item.get("title") or "").strip()
    author = (item.get("author") or "").strip()
    match = None
    if title:
        rows = db.all(
            """SELECT DISTINCT w.* FROM works w
               LEFT JOIN work_contributors wc ON wc.work_id=w.id AND wc.role='author'
               LEFT JOIN contributors c ON c.id=wc.contributor_id
               WHERE w.title=? COLLATE NOCASE""",
            (title,),
        )
        if len(rows) == 1:
            match = rows[0]
        elif len(rows) > 1 and author:
            for r in rows:
                names = {a["name"].casefold() for a in db.all(
                    "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author'",
                    (r["id"],),
                )}
                if author.casefold() in names:
                    match = r
                    break
    if match:
        summary = _summary_of(match)
        summary["reason"] = item.get("reason")
        return summary
    return {"work_id": None, "title": title or "(untitled)", "authors": [author] if author else [], "in_library": False, "reason": item.get("reason")}


def recommend_for_work(work_id: str, *, viewer: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not work:
        raise ValueError("Work not found")

    kids_only = bool(viewer and viewer.get("access_scope") == "kids")

    if not force:
        cached = db.one("SELECT * FROM recommendation_cache WHERE work_id=?", (work_id,))
        if cached and db.now() - cached["generated_at"] < CACHE_TTL_SECONDS:
            # A kids-scope viewer must never be served an LLM-generated cache entry -
            # regenerate on the safe path rather than trust a cache written for someone else.
            if not (kids_only and cached["algorithm"] == "llm"):
                return {"algorithm": cached["algorithm"], "items": db.json_load(cached["items_json"], [])}

    algorithm = "heuristic"
    items: list[dict[str, Any]]
    if kids_only:
        items = [_summary_of(c) for c in _heuristic_candidates(work, kids_only=True, viewer=viewer)]
    else:
        llm_result = llmclient.chat_json(
            LLM_SYSTEM_PROMPT,
            f"Title: {work['title']}\nDescription: {(work.get('description') or '')[:1500]}",
        ) if llmclient.enabled() else None
        if llm_result and isinstance(llm_result.get("items"), list) and llm_result["items"]:
            algorithm = "llm"
            items = [_match_llm_suggestion(i) for i in llm_result["items"][:6] if isinstance(i, dict)]
        else:
            items = [_summary_of(c) for c in _heuristic_candidates(work, kids_only=False)]

    with db.tx() as conn:
        conn.execute(
            """INSERT INTO recommendation_cache(work_id,algorithm,items_json,generated_at) VALUES (?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET algorithm=excluded.algorithm,items_json=excluded.items_json,generated_at=excluded.generated_at""",
            (work_id, algorithm, db.json_dump(items), db.now()),
        )
    return {"algorithm": algorithm, "items": items}
