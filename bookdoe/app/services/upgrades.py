"""Opt-in upgrade search for owned assets (P5-27, Part XXIX).

media_assets.upgrade_policy is 'none' (default), 'flag_only' or 'search'. Only 'search',
set by a user on one asset, makes an owned asset whose stored quality evaluation says
'upgrade_desired' eligible for an indexer search. The search records releases and nothing
more: this module never grabs, downloads, replaces or touches a file. A user picks a
candidate and grabs it by hand; replacing the owned file is a manual file plan (P5-17).
"""
from __future__ import annotations

from typing import Any

from .. import db
from . import releases
from . import acquisition

POLICIES = ("none", "flag_only", "search")
DEFAULT_POLICY = "none"
SEARCH_LIMIT = 5        # assets searched per run; keeps indexer traffic bounded
CANDIDATES_PER_ASSET = 5

_ELIGIBLE = """a.status='owned' AND a.derived_from_asset_id IS NULL AND a.upgrade_policy='search'
               AND json_extract(a.quality_json,'$.evaluation.state')='upgrade_desired'"""
_LAST_SEARCH = "(SELECT MAX(rs.started_at) FROM release_searches rs WHERE rs.work_id=a.work_id AND rs.asset_kind=a.kind AND rs.automatic=1)"


def eligible_assets(limit: int | None = None) -> list[dict[str, Any]]:
    """Opted-in, owned, upgrade-desired assets, least recently searched first."""
    sql = f"SELECT a.* FROM media_assets a WHERE {_ELIGIBLE} ORDER BY {_LAST_SEARCH} IS NOT NULL, {_LAST_SEARCH}, a.id"  # nosec B608
    if limit is None:
        return db.all(sql)
    return db.all(sql + " LIMIT ?", (limit,))


def search_job() -> dict[str, Any]:
    """The 'upgrade-search' job body: search for eligible assets, grab nothing."""
    acquisition.ensure_enabled()
    searched, found, errors, asset_ids = 0, 0, [], []
    for a in eligible_assets(SEARCH_LIMIT):
        try:
            releases.search_releases(a["work_id"], a["kind"], automatic=True)
            searched += 1
            asset_ids.append(a["id"])
            found += bool(_candidate_releases(a))
        except Exception as exc:
            errors.append({"asset_id": a["id"], "error": str(exc)})
    return {"searched": searched, "with_candidates": found, "errors": errors, "asset_ids": asset_ids}


def _candidate_releases(asset: dict[str, Any]) -> list[dict[str, Any]]:
    """Non-rejected releases of the asset's latest automatic search, best score first."""
    search = db.one(
        "SELECT id,started_at FROM release_searches WHERE work_id=? AND asset_kind=? AND automatic=1 AND status='complete' "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (asset["work_id"], asset["kind"]),
    )
    if not search:
        return []
    return db.all(
        """SELECT id,raw_title,protocol,size_bytes,seeders,score,indexer_id,info_url,created_at FROM releases
           WHERE search_id=? AND rejected=0 ORDER BY score DESC, seeders DESC, id LIMIT ?""",
        (search["id"], CANDIDATES_PER_ASSET),
    )


def list_candidates() -> list[dict[str, Any]]:
    """Eligible assets that have at least one candidate release, for GET /api/v1/upgrades.
    An asset on policy 'none' or 'flag_only' never appears, whatever was searched before."""
    out = []
    for a in eligible_assets():
        candidates = _candidate_releases(a)
        if not candidates:
            continue
        work = db.one("SELECT title FROM works WHERE id=?", (a["work_id"],))
        evaluation = (db.json_load(a["quality_json"], {}) or {}).get("evaluation") or {}
        out.append({
            "asset_id": a["id"], "work_id": a["work_id"], "title": work["title"] if work else None,
            "kind": a["kind"], "format": a["format"], "reasons": evaluation.get("reasons") or [],
            "releases": candidates,
        })
    return out
