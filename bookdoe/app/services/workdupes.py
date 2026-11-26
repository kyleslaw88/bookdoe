"""Duplicate-Work candidates for the merge UI (Part XXVI): on a series, around one book, or for a hand-picked set.

Read-only. It reuses the P5-21a detector's vocabulary (catalogdupes.title_key / strip_series_suffix and its
position guard) instead of a second matcher, and adds what a page needs: groups, a suggested canonical
Work with a stated reason, and a card per Work (assets, files, progress, tags, identifiers) so the merge
dialog can show them side by side. Nothing here writes; merging is workmerge.execute.

A group is Works that are plausibly one book: the same title once series decoration is removed, the same
author set (an unknown author does not block), the same position (an unknown one does not block) and a
compatible language. Two Works of one series that name different positions are never grouped, however
alike their titles are ("The Sword Thief" #3 is not "The Black Circle" #5; "Book (Part 1)" is not "(Part 2)").
"""
from __future__ import annotations

import difflib
from typing import Any, Iterable

from .. import db
from . import catalogdupes, tagging

MAX_CANDIDATES = 25
FUZZY_RATIO = 0.9
CHUNK = 400


def _chunks(ids: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(ids), CHUNK):
        yield ids[i:i + CHUNK]


def _num(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v).strip().casefold()


def load_facts(work_ids: Iterable[str], series_name_by_work: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Matching facts for these Works, in a handful of queries."""
    ids = list(dict.fromkeys(work_ids))
    facts: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(ids):
        m = ",".join("?" * len(chunk))
        for r in db.all(f"SELECT id,title,primary_language,metadata_id,added_at,monitored FROM works WHERE id IN ({m})", tuple(chunk)):  # nosec B608
            facts[r["id"]] = {"id": r["id"], "title": r["title"], "language": (r["primary_language"] or "").strip().casefold(),
                              "provider_linked": bool(r["metadata_id"]), "added_at": r["added_at"], "monitored": bool(r["monitored"]),
                              "authors": {}, "series": {}, "identifiers": set(), "owned": 0, "owned_files": 0}
        for r in db.all(f"SELECT wc.work_id,c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id "  # nosec B608
                        f"WHERE wc.role='author' AND wc.work_id IN ({m}) ORDER BY wc.work_id,wc.ord", tuple(chunk)):
            facts[r["work_id"]]["authors"].setdefault(db.contributor_key(r["name"]), r["name"])
        for r in db.all(f"SELECT sm.work_id,sm.series_id,sm.position,s.name FROM series_memberships sm JOIN series s ON s.id=sm.series_id "  # nosec B608
                        f"WHERE sm.work_id IN ({m})", tuple(chunk)):
            facts[r["work_id"]]["series"][r["series_id"]] = {"name": r["name"], "position": (str(r["position"]).strip() if r["position"] is not None else "")}
        for r in db.all(f"SELECT i.namespace,i.value,CASE i.entity_type WHEN 'work' THEN i.entity_id ELSE e.work_id END work_id "  # nosec B608
                        f"FROM identifiers i LEFT JOIN editions e ON i.entity_type='edition' AND e.id=i.entity_id "
                        f"WHERE (i.entity_type='work' AND i.entity_id IN ({m})) OR (i.entity_type='edition' AND e.work_id IN ({m}))",
                        (*chunk, *chunk)):
            if r["work_id"] in facts:
                facts[r["work_id"]]["identifiers"].add((r["namespace"], str(r["value"]).casefold()))
        for r in db.all(f"SELECT a.work_id,COUNT(DISTINCT a.id) n,COUNT(f.id) files FROM media_assets a LEFT JOIN media_files f ON f.asset_id=a.id "  # nosec B608
                        f"WHERE a.status='owned' AND a.derived_from_asset_id IS NULL AND a.work_id IN ({m}) GROUP BY a.work_id", tuple(chunk)):
            facts[r["work_id"]]["owned"] = r["n"]
            facts[r["work_id"]]["owned_files"] = r["files"]
    for w in facts.values():
        sname = next(iter(w["series"].values()), {}).get("name") if w["series"] else None
        stem, suffix_pos = catalogdupes.strip_series_suffix(w["title"], (series_name_by_work or {}).get(w["id"]) or sname)
        w["stem"], w["suffix_position"] = stem, suffix_pos
        w["key"] = catalogdupes.title_key(w["title"], (series_name_by_work or {}).get(w["id"]) or sname)
        w["full_key"] = catalogdupes.full_title_key(w["title"], (series_name_by_work or {}).get(w["id"]) or sname)
        w["clean_title"] = stem == w["title"].strip()
    return facts


def positions(w: dict[str, Any], series_id: str | None = None) -> set[Any]:
    """Every position this Work claims (series membership(s) and its title decoration)."""
    out = set()
    for sid, s in w["series"].items():
        if (series_id is None or sid == series_id) and s["position"]:
            out.add(_num(s["position"]))
    if w["suffix_position"]:
        out.add(_num(w["suffix_position"]))
    return out


def compatible(a: dict[str, Any], b: dict[str, Any], series_id: str | None = None) -> tuple[bool, list[str]]:
    """(plausibly the same book, the signals that say so). Never true across different positions."""
    signals: list[str] = []
    if a["key"] != b["key"] or a["full_key"] != b["full_key"]:
        return False, signals
    signals.append("title_equal_after_series_suffix" if a["title"].strip().casefold() != b["title"].strip().casefold() else "title_equal")
    pa, pb = positions(a, series_id), positions(b, series_id)
    if pa and pb and not (pa & pb):
        return False, []
    if pa and pb:
        signals.append("same_position")
    ka, kb = set(a["authors"]), set(b["authors"])
    if ka and kb and ka != kb:
        return False, []
    if ka and kb:
        signals.append("same_authors")
    if a["language"] and b["language"] and a["language"] != b["language"]:
        return False, []
    shared = a["identifiers"] & b["identifiers"]
    if shared:
        signals.append("shared_identifier")
    return True, signals


def _group_key(w: dict[str, Any], series_id: str | None) -> Any:
    p = positions(w, series_id)
    return sorted(p, key=str)[0] if len(p) == 1 else (None if not p else "multi")


def series_groups(series_id: str) -> list[dict[str, Any]]:
    """Groups of Works in the series that look like one book. Works with no position join a group only
    when exactly one position carries that title (otherwise it is ambiguous and left alone)."""
    s = db.one("SELECT id,name FROM series WHERE id=?", (series_id,))
    if not s:
        return []
    ids = [r["work_id"] for r in db.all("SELECT work_id FROM series_memberships WHERE series_id=?", (series_id,))]
    facts = load_facts(ids, {i: s["name"] for i in ids})
    by_key: dict[str, list[dict[str, Any]]] = {}
    for w in facts.values():
        if w["key"]:
            by_key.setdefault(w["key"], []).append(w)
    groups = []
    for key, members in sorted(by_key.items()):
        if len(members) < 2:
            continue
        buckets: dict[Any, list[dict[str, Any]]] = {}
        for w in members:
            buckets.setdefault(_group_key(w, series_id), []).append(w)
        unplaced = buckets.pop(None, [])
        if len(buckets) == 1 and unplaced:
            next(iter(buckets.values())).extend(unplaced)
        elif not buckets and unplaced:
            buckets[None] = unplaced
        for ws in buckets.values():
            ws = sorted(ws, key=lambda w: w["id"])
            ok = [w for w in ws if all(compatible(w, o, series_id)[0] for o in ws if o is not w)]
            if len(ok) < 2:
                continue
            sig: set[str] = set()
            for w in ok[1:]:
                sig |= set(compatible(ok[0], w, series_id)[1])
            groups.append({"key": key, "work_ids": [w["id"] for w in ok], "titles": [w["title"] for w in ok],
                           "signals": sorted(sig), "confidence": "high" if {"same_authors", "same_position"} & sig or "shared_identifier" in sig else "probable"})
    for g in groups:
        g["suggestion"] = suggest(g["work_ids"], facts)
    groups.sort(key=lambda g: g["titles"][0].casefold())
    return groups


def suggest(work_ids: list[str], facts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Which Work should become the canonical one, and why: most owned assets, then more owned files, then the
    cleaner title, then a series position, provider link, identifiers, and the older record."""
    facts = facts or load_facts(work_ids)
    ws = [facts[i] for i in work_ids if i in facts]

    def score(w):
        return (w["owned"], w["owned_files"], w["clean_title"], sum(1 for s in w["series"].values() if s["position"]), w["provider_linked"],
                len(w["identifiers"]), -w["added_at"], w["id"])
    best = max(ws, key=score)
    others = [w for w in ws if w is not best]
    why = []
    if others and best["owned"] > max(o["owned"] for o in others):
        why.append(f"it has the most owned assets ({best['owned']} vs {max(o['owned'] for o in others)})")
    elif others and best["owned_files"] > max(o["owned_files"] for o in others):
        why.append("it has the most owned files")
    if others and best["clean_title"] and not all(o["clean_title"] for o in others):
        why.append("its title has no series decoration")
    if not why and others and any(s["position"] for s in best["series"].values()) and not all(any(s["position"] for s in o["series"].values()) for o in others):
        why.append("it has the series position")
    if not why:
        why.append("the oldest record (a tie on everything else)")
    # The nicest title wins whichever record keeps the rest: ask the merge to take it when the canonical's is decorated.
    resolutions: dict[str, Any] = {}
    clean = [w for w in ws if w["clean_title"]]
    if not best["clean_title"] and clean:
        resolutions = {"fields": {"title": "source"}}
    return {"canonical_work_id": best["id"], "reason": "Suggested because " + " and ".join(why) + ".",
            "title_after_merge": (clean[0]["title"] if resolutions else best["title"]), "resolutions": resolutions}


def work_candidates(work_id: str) -> list[dict[str, Any]]:
    """Other Works that may be the same book as this one: loose title + author, a shared identifier, or a very
    similar title by the same author."""
    me = load_facts([work_id])
    if work_id not in me:
        return []
    w = me[work_id]
    rows = db.all("SELECT id,title FROM works WHERE id<>?", (work_id,))
    series_name = next((s["name"] for s in w["series"].values()), None)
    cand: dict[str, list[str]] = {}
    for r in rows:
        k = catalogdupes.title_key(r["title"])
        if k and (k == w["key"]):
            cand.setdefault(r["id"], []).append("title_equal_after_series_suffix")
    if w["identifiers"]:
        for ns, val in w["identifiers"]:
            for r in db.all("SELECT CASE i.entity_type WHEN 'work' THEN i.entity_id ELSE e.work_id END work_id FROM identifiers i "
                            "LEFT JOIN editions e ON i.entity_type='edition' AND e.id=i.entity_id "
                            "WHERE i.namespace=? AND LOWER(i.value)=? AND i.entity_type IN ('work','edition')", (ns, val)):
                if r["work_id"] and r["work_id"] != work_id:
                    cand.setdefault(r["work_id"], []).append("shared_identifier")
    if w["authors"]:
        keys = tuple(w["authors"])
        for r in db.all("SELECT DISTINCT wc.work_id FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id "  # nosec B608
                        f"WHERE wc.role='author' AND wc.work_id<>? AND LOWER(TRIM(c.name)) IN ({','.join('?' * len(keys))})", (work_id, *keys)):
            cand.setdefault(r["work_id"], [])
    facts = load_facts(list(cand))
    out = []
    for cid, sigs in cand.items():
        o = facts.get(cid)
        if not o:
            continue
        ok, extra = compatible(w, o)
        signals = sorted(set(sigs) | set(extra))
        ratio = difflib.SequenceMatcher(None, w["key"], o["key"]).ratio() if w["key"] and o["key"] else 0.0
        same_author = bool(set(w["authors"]) & set(o["authors"]))
        if not sigs and not (same_author and ratio >= FUZZY_RATIO):
            continue
        if not sigs:
            signals.append("similar_title")
        diff = bool(positions(w) and positions(o) and not (positions(w) & positions(o)))
        if diff and "shared_identifier" not in signals:
            continue   # different volumes of a series
        out.append({"work_id": cid, "title": o["title"], "authors": list(o["authors"].values()), "signals": signals, "title_similarity": round(ratio, 3),
                    "owned_assets": o["owned"], "position_conflict": diff,
                    "confidence": "high" if "shared_identifier" in signals or ok and "same_authors" in signals else "probable"})
    out.sort(key=lambda c: (c["confidence"] != "high", -c["title_similarity"], c["title"].casefold()))
    return out[:MAX_CANDIDATES]


def work_cards(work_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Everything the merge dialog shows for each Work, in the order asked."""
    ids = list(dict.fromkeys(work_ids))
    facts = load_facts(ids)
    cards: list[dict[str, Any]] = []
    for wid in ids:
        f = facts.get(wid)
        if not f:
            continue
        w = db.one("SELECT id,title,subtitle,description,first_publish_date,primary_language,monitored,added_at,cover_url FROM works WHERE id=?", (wid,))
        assets = db.all("SELECT id,kind,format,status,monitored,duration_seconds,derived_from_asset_id,root_folder_id FROM media_assets WHERE work_id=? ORDER BY kind,format", (wid,))
        for a in assets:
            files = db.all("SELECT id,path,size_bytes,duration_seconds,part_number,missing FROM media_files WHERE asset_id=? ORDER BY part_number,path", (a["id"],))
            a["files"] = len(files)
            a["size_bytes"] = sum(x["size_bytes"] or 0 for x in files)
            a["file_duration_seconds"] = sum(x["duration_seconds"] or 0 for x in files) or a["duration_seconds"]
            a["owned"] = a["status"] == "owned"
            a["derived"] = bool(a["derived_from_asset_id"])
        progress = db.all("SELECT mp.user_id,u.username,mp.kind,mp.percent,mp.position,mp.updated_at FROM media_progress mp LEFT JOIN users u ON u.id=mp.user_id "
                          "WHERE mp.work_id=? ORDER BY u.username,mp.kind", (wid,))
        tags = db.all("SELECT t.id,t.name FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? ORDER BY t.name", (wid,))
        cards.append({
            "id": wid, "title": w["title"], "subtitle": w["subtitle"], "authors": list(f["authors"].values()), "language": w["primary_language"],
            "series": [{"id": sid, "name": s["name"], "position": s["position"] or None} for sid, s in f["series"].items()],
            "cover_url": f"/api/v1/library/{wid}/cover", "added_at": w["added_at"], "monitored": bool(w["monitored"]),
            "first_publish_date": w["first_publish_date"], "clean_title": f["clean_title"], "stem": f["stem"], "suffix_position": f["suffix_position"],
            "assets": assets, "owned_assets": f["owned"], "owned_files": f["owned_files"],
            "total_bytes": sum(a["size_bytes"] for a in assets), "progress": progress,
            "tags": [{"id": t["id"], "name": t["name"], "label": tagging.tag_label(t["name"])} for t in tags],
            "kids_shelf": any(t["name"] == tagging.KIDS_TAG_NAME for t in tags),
            "identifiers": sorted([{"namespace": ns, "value": v} for ns, v in f["identifiers"]], key=lambda i: (i["namespace"], i["value"])),
            "provider_linked": f["provider_linked"],
        })
    return cards


def describe(work_ids: list[str]) -> dict[str, Any]:
    """A hand-picked set (library select mode, a group's review): cards, the suggested canonical, and warnings
    when the pick does not look like one book."""
    ids = list(dict.fromkeys(work_ids))
    facts = load_facts(ids)
    ids = [i for i in ids if i in facts]
    warnings: list[dict[str, Any]] = []
    if len(ids) >= 2:
        first = facts[ids[0]]
        for oid in ids[1:]:
            o = facts[oid]
            ok, _ = compatible(first, o)
            shared = first["identifiers"] & o["identifiers"]
            if not ok and not shared:
                warnings.append({"code": "different_books", "work_ids": [first["id"], oid],
                                 "message": f"\"{first['title']}\" and \"{o['title']}\" do not look like the same book (different title, author, position or language)."})
    return {"work_ids": ids, "cards": work_cards(ids), "suggestion": suggest(ids, facts) if ids else None, "warnings": warnings}
