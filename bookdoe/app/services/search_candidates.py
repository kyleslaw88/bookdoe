from __future__ import annotations

from typing import Any

from .. import db
from . import metadata, search_providers
from .search_normalize import split_title_subtitle
from .seriesnorm import split_position

# ---------------------------------------------------------------------------
# State vocabulary (shared with matchevidence)
# ---------------------------------------------------------------------------
STATE_EXACT = "exact"
STATE_SIMILAR = "similar"
STATE_CONFLICT = "conflict"
STATE_UNAVAILABLE = "unavailable"

VALID_STATES = (STATE_EXACT, STATE_SIMILAR, STATE_CONFLICT, STATE_UNAVAILABLE)

_IDENTIFIER_NAMESPACES = ("isbn", "isbn10", "asin")

_JSON_LIST_FIELDS = ("authors", "contributors", "series", "identifiers", "cover_refs", "providers")


def decode_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Expand a search_candidates DB row's *_json columns into plain fields."""
    out = dict(row)
    for field in _JSON_LIST_FIELDS:
        out[field] = db.json_load(out.pop(f"{field}_json"), [])
    out["provider_evidence"] = db.json_load(out.pop("provider_evidence_json"), {})
    return out


def _identifier_key(identifiers: list[dict[str, Any]]) -> tuple[str, str] | None:
    for ident in identifiers:
        ns, value = ident.get("namespace"), ident.get("value")
        if ns in _IDENTIFIER_NAMESPACES and value:
            return (ns, str(value).strip().lower())
    return None


def _merge_key(raw: dict[str, Any]) -> str:
    """Group raw per-provider results that describe the same underlying book.

    A shared identifier is the strongest signal. Falling back to normalized
    title + first author lets two providers that both lack identifiers for a
    title still merge into one candidate instead of showing as duplicates.
    """
    ident_key = _identifier_key(raw.get("identifiers") or [])
    if ident_key:
        return f"id:{ident_key[0]}:{ident_key[1]}"
    title = (raw.get("title") or "").strip().casefold()
    author = ((raw.get("authors") or [None])[0] or "").strip().casefold()
    return f"ta:{title}::{author}"


def _seed_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    title = raw.get("title")
    subtitle = raw.get("subtitle")
    if not subtitle and title:
        title, subtitle = split_title_subtitle(title)

    series = list(raw.get("series") or [])
    series_position = None
    if series:
        name, position = split_position(series[0])
        if position:
            series = [name] + series[1:]
            series_position = position

    return {
        "title": title,
        "subtitle": subtitle,
        "authors": list(raw.get("authors") or []),
        "contributors": [],
        "series": series,
        "series_position": series_position,
        "identifiers": list(raw.get("identifiers") or []),
        "publication_date": raw.get("publication_date"),
        "publisher": raw.get("publisher"),
        "language": raw.get("language"),
        "cover_refs": [raw["cover_url"]] if raw.get("cover_url") else [],
    }


def _merge_identifiers(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> None:
    seen = {(i.get("namespace"), str(i.get("value"))) for i in existing}
    for ident in new:
        key = (ident.get("namespace"), str(ident.get("value")))
        if ident.get("namespace") and key not in seen:
            existing.append(ident)
            seen.add(key)


def _explain(merged: dict[str, Any]) -> str:
    parts = []
    providers = merged["providers"]
    if len(providers) > 1:
        parts.append(f"matched by {len(providers)} providers ({', '.join(providers)})")
    else:
        parts.append(f"from {providers[0]}")
    signals = (
        ("identifier match", STATE_EXACT if _identifier_key(merged["identifiers"]) else STATE_UNAVAILABLE),
        ("title match", STATE_EXACT if merged.get("title") else STATE_UNAVAILABLE),
        ("series match", STATE_EXACT if merged.get("series") else STATE_UNAVAILABLE),
    )
    # Only exact signals are worth surfacing; the state comes from the shared vocabulary.
    parts.extend(label for label, state in signals if state == STATE_EXACT)
    return "; ".join(parts)


def build_candidates(query: str, *, limit: int = 20, providers: list[str] | None = None) -> list[dict[str, Any]]:
    """Run a federated search and persist its results as ephemeral candidates.

    Per Part XVII.D, remote search results must never create a canonical Work by
    themselves -- rows land in search_candidates only, and stay there until
    promote_candidate() is called on one of them.
    """
    raw_results = search_providers.search_federated(query, limit=limit, providers=providers)

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in raw_results:
        key = _merge_key(raw)
        seed = _seed_from_raw(raw)
        provider = raw.get("provider") or "unknown"
        score = float(raw.get("match_score") or 0.0)
        if key not in merged:
            merged[key] = {**seed, "providers": [provider], "provider_evidence": {provider: raw}, "match_score": score}
            order.append(key)
            continue
        m = merged[key]
        if provider not in m["providers"]:
            m["providers"].append(provider)
        m["provider_evidence"][provider] = raw
        m["match_score"] = max(m["match_score"], score)
        for field in ("subtitle", "publication_date", "publisher", "language"):
            if not m.get(field) and seed.get(field):
                m[field] = seed[field]
        if not m.get("series") and seed.get("series"):
            m["series"] = seed["series"]
            m["series_position"] = seed["series_position"]
        if not m.get("cover_refs") and seed.get("cover_refs"):
            m["cover_refs"] = seed["cover_refs"]
        for author in seed["authors"]:
            if author not in m["authors"]:
                m["authors"].append(author)
        _merge_identifiers(m["identifiers"], seed["identifiers"])

    t = db.now()
    candidates: list[dict[str, Any]] = []
    with db.tx() as conn:
        for key in order:
            m = merged[key]
            cid = db.uid()
            conn.execute(
                """INSERT INTO search_candidates(
                     id, query, title, subtitle, authors_json, contributors_json,
                     series_json, series_position, identifiers_json, publication_date,
                     publisher, language, cover_refs_json, providers_json,
                     provider_evidence_json, match_score, match_explanation, status,
                     created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (
                    cid, query, m["title"], m["subtitle"], db.json_dump(m["authors"]),
                    db.json_dump(m["contributors"]), db.json_dump(m["series"]), m["series_position"],
                    db.json_dump(m["identifiers"]), m["publication_date"], m["publisher"], m["language"],
                    db.json_dump(m["cover_refs"]), db.json_dump(m["providers"]), db.json_dump(m["provider_evidence"]),
                    m["match_score"], _explain(m), t, t,
                ),
            )
            row = conn.execute("SELECT * FROM search_candidates WHERE id=?", (cid,)).fetchone()
            candidates.append(decode_candidate(dict(row)))

    candidates.sort(key=lambda c: -(c["match_score"] or 0.0))
    return candidates


def promote_candidate(candidate_id: str, *, monitored: bool = True, work_id: str | None = None) -> str:
    """Promote an ephemeral search candidate into a canonical Work, or link it
    into an existing one (Part XX -- mirrors adopt_candidate's work_id override
    for scan candidates: an explicit "link to existing Work" action instead of
    always minting a new one).

    This is the only path from search_candidates into works. Callers decide
    when it is reached (user select/add, resolver adoption, author/series
    discovery import, explicit automation) -- it never runs off a bare search
    response on its own. Idempotent: promoting an already-promoted candidate
    again just returns the Work it already created/linked, regardless of any
    work_id passed this time.
    """
    c = db.one("SELECT * FROM search_candidates WHERE id=?", (candidate_id,))
    if not c:
        raise ValueError("Search candidate not found")
    if c["status"] == "promoted" and c["promoted_work_id"]:
        return c["promoted_work_id"]
    if work_id is not None and not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        raise ValueError("Work not found")
    if work_id is None and not c.get("title"):
        raise ValueError("Candidate has no title to promote")

    authors = [a for a in db.json_load(c["authors_json"], []) if a]
    series = [s for s in db.json_load(c["series_json"], []) if s]
    identifiers = db.json_load(c["identifiers_json"], [])
    cover_refs = db.json_load(c["cover_refs_json"], [])

    t = db.now()
    creating_new_work = work_id is None
    with db.tx() as conn:
        if creating_new_work:
            work_id = db.uid()
            conn.execute(
                """INSERT INTO works(id,title,sort_title,subtitle,cover_url,first_publish_date,primary_language,monitored,metadata_status,metadata_source,added_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,'resolved','candidate',?,?)""",
                (
                    work_id, c["title"], metadata._sort_title(c["title"]), c["subtitle"],
                    cover_refs[0] if cover_refs else None, c["publication_date"], c["language"],
                    int(monitored), t, t,
                ),
            )
        # Reuses the exact same contributor-dedup/identifier-upsert/series-linking
        # path for both the new-Work and link-to-existing-Work cases -- not a
        # second ad hoc write path. Safe for an existing Work too: the PK on
        # work_contributors is (work_id,contributor_id,role), so INSERT OR IGNORE
        # below never collides with that Work's own existing rows, and
        # _upsert_identifier()/_link_series() are themselves upserts.
        for i, name in enumerate(authors):
            # Adopt an existing same-named contributor first, same rule upsert_work
            # uses, so a search-sourced author never mints a duplicate authors-page row.
            cid = db.adopt_unlinked_contributor(conn, name) or db.uid()
            conn.execute(
                "INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (cid, name, metadata._person_sort(name), t, t),
            )
            conn.execute(
                "INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                (work_id, cid, "author", i),
            )
        for ident in identifiers:
            metadata._upsert_identifier(conn, "work", work_id, ident)
        for idx, name in enumerate(series):
            position = c["series_position"] if idx == 0 else None
            metadata._link_series(conn, work_id, {"name": name, "position": position}, t)
        conn.execute(
            "UPDATE search_candidates SET status='promoted',promoted_work_id=?,updated_at=? WHERE id=?",
            (work_id, t, candidate_id),
        )
    metadata._ensure_assets(work_id)
    if creating_new_work:
        db.add_history(
            "library.added", f"Added {c['title']} to library from search candidate",
            work_id=work_id, data={"candidate_id": candidate_id, "providers": db.json_load(c["providers_json"], [])},
        )
    else:
        db.add_history(
            "library.linked", f"Linked search candidate {c['title']!r} into existing work",
            work_id=work_id, data={"candidate_id": candidate_id, "providers": db.json_load(c["providers_json"], [])},
        )
    return work_id


def dismiss_candidate(candidate_id: str) -> None:
    """Mark a search candidate as intentionally left unmatched (Part XX).

    Distinct from 'discarded' (the existing search-flow rejection status) --
    'dismissed' is an explicit user "leave this unmatched" choice, mirroring
    scan_candidates' 'ignored' status for the same intent on the other
    candidate type.
    """
    if not db.one("SELECT id FROM search_candidates WHERE id=?", (candidate_id,)):
        raise ValueError("Search candidate not found")
    with db.tx() as conn:
        conn.execute("UPDATE search_candidates SET status='dismissed',updated_at=? WHERE id=?", (db.now(), candidate_id))
