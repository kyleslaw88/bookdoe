from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .. import db
from . import metadata


# Roles browsable through the authors endpoints (Part XXII). Monitor/refresh stay
# author-only; a role other than 'author' only lists and details contributors.
BROWSE_ROLES = ("author", "narrator", "editor", "translator", "illustrator")


def list_authors(role: str = "author", q: str | None = None) -> list[dict[str, Any]]:
    # Server-side name filter (matches the client-side substring filter it replaces).
    where, params = "", []
    if q and q.strip():
        where = "WHERE c.name LIKE ? ESCAPE '\\'"
        params = [db.like_pattern(q.strip())]
    if role != "author":
        # Unlike the author listing (every contributor row), only people who hold the role.
        return db.all(
            f"""SELECT c.*,NULL policy,NULL monitor_ebook,NULL monitor_audiobook,NULL monitor_started_at,
                      NULL last_refresh_at,NULL last_error,
                      COUNT(DISTINCT wc.work_id) work_count,
                      COUNT(DISTINCT CASE WHEN a.kind='audiobook' AND a.status='owned' THEN a.work_id END) owned_audio,
                      COUNT(DISTINCT CASE WHEN a.kind='ebook' AND a.status='owned' THEN a.work_id END) owned_ebooks,
                      COUNT(DISTINCT CASE WHEN a.status='owned' THEN a.work_id END) owned_works
               FROM contributors c
               JOIN work_contributors wc ON wc.contributor_id=c.id AND wc.role=?
               LEFT JOIN media_assets a ON a.work_id=wc.work_id
               {where}
               GROUP BY c.id ORDER BY COALESCE(c.sort_name,c.name)""",  # nosec B608
            (role, *params),
        )
    rows = db.all(
        f"""SELECT c.*,am.policy,am.ebook monitor_ebook,am.audiobook monitor_audiobook,am.started_at monitor_started_at,
                  am.last_refresh_at,am.last_error,
                  COUNT(DISTINCT wc.work_id) work_count,
                  COUNT(DISTINCT CASE WHEN a.kind='audiobook' AND a.status='owned' THEN a.work_id END) owned_audio,
                  COUNT(DISTINCT CASE WHEN a.kind='ebook' AND a.status='owned' THEN a.work_id END) owned_ebooks,
                  COUNT(DISTINCT CASE WHEN a.status='owned' THEN a.work_id END) owned_works
           FROM contributors c
           LEFT JOIN author_monitors am ON am.contributor_id=c.id
           LEFT JOIN work_contributors wc ON wc.contributor_id=c.id AND wc.role='author'
           LEFT JOIN media_assets a ON a.work_id=wc.work_id
           {where}
           GROUP BY c.id ORDER BY COALESCE(c.sort_name,c.name)""",  # nosec B608
        tuple(params),
    )
    return rows


def author_detail(contributor_id: str, role: str = "author") -> dict[str, Any] | None:
    a = db.one("SELECT c.*,am.policy,am.ebook monitor_ebook,am.audiobook monitor_audiobook,am.started_at monitor_started_at,am.last_refresh_at,am.last_error FROM contributors c LEFT JOIN author_monitors am ON am.contributor_id=c.id WHERE c.id=?", (contributor_id,))
    if not a:
        return None
    if role != "author":
        for k in ("policy", "monitor_ebook", "monitor_audiobook", "monitor_started_at", "last_refresh_at", "last_error"):
            a[k] = None
    a["works"] = db.all(
        """SELECT w.id,w.title,w.cover_url,w.first_publish_date,w.monitored,w.metadata_status
           FROM work_contributors wc JOIN works w ON w.id=wc.work_id
           WHERE wc.contributor_id=? AND wc.role=? ORDER BY w.first_publish_date DESC,w.sort_title,w.title""",
        (contributor_id, role),
    )
    for w in a["works"]:
        w["assets"] = db.all("SELECT id,kind,format,status,monitored FROM media_assets WHERE work_id=? AND derived_from_asset_id IS NULL ORDER BY kind", (w["id"],))
    return a


def set_author_monitor(contributor_id: str, *, policy: str, ebook: bool, audiobook: bool) -> None:
    if policy not in {"all", "future", "latest", "none"}:
        raise ValueError("Invalid author monitor policy")
    if not db.one("SELECT id FROM contributors WHERE id=?", (contributor_id,)):
        raise ValueError("Contributor not found")
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO author_monitors(contributor_id,policy,ebook,audiobook,started_at)
               VALUES (?,?,?,?,?) ON CONFLICT(contributor_id) DO UPDATE SET policy=excluded.policy,ebook=excluded.ebook,audiobook=excluded.audiobook""",
            (contributor_id, policy, int(ebook), int(audiobook), db.now()),
        )


def refresh_author(contributor_id: str) -> dict[str, Any]:
    monitor = db.one("SELECT * FROM author_monitors WHERE contributor_id=?", (contributor_id,))
    contributor = db.one("SELECT * FROM contributors WHERE id=?", (contributor_id,))
    if not monitor or not contributor or not contributor.get("metadata_id"):
        raise ValueError("Author is not mapped to OpenReads or is not monitored")
    prior_refresh = monitor.get("last_refresh_at")
    try:
        works = metadata.contributor_works(contributor["metadata_id"], refresh=True)
        added = monitored = 0
        candidates = []
        for mw in works:
            existing = db.one("SELECT id FROM works WHERE metadata_id=?", (mw["id"],))
            should_add = _policy_select(monitor, mw, is_new=existing is None, prior_refresh=prior_refresh)
            if not existing and not should_add and monitor["policy"] != "all":
                continue
            wid = metadata.upsert_work(mw, monitored=should_add or bool(existing))
            if not existing:
                added += 1
            _apply_asset_monitoring(wid, monitor, should_add)
            if should_add:
                monitored += 1
            candidates.append(wid)
        with db.tx() as conn:
            conn.execute("UPDATE author_monitors SET last_refresh_at=?,last_error=NULL WHERE contributor_id=?", (db.now(), contributor_id))
        return {"author": contributor["name"], "discovered": len(works), "added": added, "monitored": monitored}
    except Exception as exc:
        with db.tx() as conn:
            conn.execute("UPDATE author_monitors SET last_error=? WHERE contributor_id=?", (str(exc), contributor_id))
        raise


def refresh_all_authors(limit: int = 20) -> dict[str, Any]:
    rows = db.all("SELECT contributor_id FROM author_monitors WHERE policy!='none' ORDER BY COALESCE(last_refresh_at,0) LIMIT ?", (limit,))
    ok, errors = [], []
    for row in rows:
        try:
            ok.append(refresh_author(row["contributor_id"]))
        except Exception as exc:
            errors.append({"contributor_id": row["contributor_id"], "error": str(exc)})
    return {"refreshed": len(ok), "results": ok, "errors": errors}


def _policy_select(monitor: dict[str, Any], mw: dict[str, Any], *, is_new: bool, prior_refresh: float | None) -> bool:
    policy = monitor["policy"]
    if policy == "none":
        return False
    if policy == "all":
        return True
    if policy == "latest":
        # Caller receives works newest-first from OpenReads; exact top-N selection
        # is handled conservatively by date. Anything in the current/next year is monitored.
        year = _year(mw.get("first_publish_date"))
        return bool(year and year >= date.today().year - 1)
    # Future policy: on initial adoption only genuinely future-dated works are
    # selected. On later refreshes, a newly appearing metadata work is considered
    # a new discovery even if its provider date is incomplete.
    if prior_refresh and is_new:
        return True
    year = _year(mw.get("first_publish_date"))
    return bool(year and year >= date.today().year)


def _year(value: Any) -> int | None:
    import re
    m = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(m.group()) if m else None


# Kept identical to main.STATUS_ON_INTENT and series.STATUS_ON_INTENT: a slot is
# 'missing' exactly while something wants it, and 'none' otherwise.
STATUS_ON_INTENT = "status=CASE WHEN status='owned' THEN status WHEN ?=1 THEN 'missing' ELSE 'none' END"


def _apply_asset_monitoring(work_id: str, monitor: dict[str, Any], should_monitor: bool) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE works SET monitored=?,updated_at=? WHERE id=?", (int(should_monitor), db.now(), work_id))
        for kind, enabled in (("ebook", bool(monitor["ebook"])), ("audiobook", bool(monitor["audiobook"]))):
            # A Follow policy electing to acquire this medium is acquisition intent;
            # without setting it the asset could never reach the Wanted list.
            want = int(should_monitor and enabled)
            conn.execute(
                "UPDATE media_assets SET monitored=?,has_acquisition_intent=?," + STATUS_ON_INTENT + ",updated_at=? WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL",  # nosec B608
                (want, want, want, db.now(), work_id, kind),
            )
