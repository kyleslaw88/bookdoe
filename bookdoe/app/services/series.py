from __future__ import annotations

import re
from typing import Any

from .. import db
from . import metadata as metadata_service
from . import ownership, seriesnorm
from . import tagging
from .series_cleanup import last_cleanup, reparse_all, undo_last_cleanup  # noqa: F401 - the cleanup API lives here


def list_series(user: dict[str, Any] | None = None, *, include_uncertain: bool = False) -> list[dict[str, Any]]:
    return list_series_page(user, include_uncertain=include_uncertain)["items"]


def list_series_page(
    user: dict[str, Any] | None = None,
    *,
    include_uncertain: bool = False,
    q: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """{items, total}; total is the count after the q and uncertain filters, before paging.

    The uncertain filter lives in HAVING so total and the page agree and only the
    returned page needs gap_count (computed in one pass, not one query per series)."""
    visible, vparams = tagging.kids_visibility(user)
    # The visibility predicate sits in the works JOIN, before the name filter's `?`.
    where, params = "", list(vparams)
    if q and q.strip():
        where = "WHERE s.name LIKE ? ESCAPE '\\'"
        params.append(db.like_pattern(q.strip()))
    # Suppress only a single-book series whose *only* evidence is uncertain/rejected.
    # No assertions at all (manually-created series) is not negative evidence, so it
    # must stay visible; a second member work is corroboration enough on its own
    # regardless of what any one assertion says.
    having = "" if include_uncertain else "HAVING work_count != 1 OR has_assertions=0 OR has_corroborated_assertion=1"
    base = f"""SELECT s.id,s.name,sm.policy,sm.ebook monitor_ebook,sm.audiobook monitor_audiobook,sm.last_refresh_at,
                   COALESCE(sm.audiobook_policy,CASE WHEN sm.audiobook=1 THEN sm.policy END,CASE WHEN sm.policy IS NOT NULL THEN 'none' END) audiobook_policy,
                   COALESCE(sm.ebook_policy,CASE WHEN sm.ebook=1 THEN sm.policy END,CASE WHEN sm.policy IS NOT NULL THEN 'none' END) ebook_policy,
                   COUNT(DISTINCT smem.work_id) work_count,
                   COUNT(DISTINCT CASE WHEN a.status='owned' THEN smem.work_id END) owned_count,
                   COUNT(DISTINCT CASE WHEN {ownership.OWNS_ANY} AND {ownership.WANTS_MISSING} THEN smem.work_id END) partial_count,
                   EXISTS(SELECT 1 FROM series_assertions sa WHERE sa.series_id=s.id) has_assertions,
                   EXISTS(SELECT 1 FROM series_assertions sa WHERE sa.series_id=s.id
                          AND sa.status NOT IN ('uncertain','rejected')) has_corroborated_assertion,
                   s.sort_name
           FROM series s
           JOIN series_memberships smem ON smem.series_id=s.id
           JOIN works w ON w.id=smem.work_id AND {visible}
           LEFT JOIN series_monitors sm ON sm.series_id=s.id
           LEFT JOIN media_assets a ON a.work_id=smem.work_id
           {where}
           GROUP BY s.id {having}"""  # nosec B608
    total = db.one(f"SELECT COUNT(*) n FROM ({base})", tuple(params))["n"]  # nosec B608
    sql = f"{base} ORDER BY s.sort_name COLLATE NOCASE, s.name COLLATE NOCASE"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, max(offset, 0)]
    elif offset:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    rows = db.all(sql, tuple(params))
    # Gaps are computed over every member, hidden ones included, so a kid account must
    # not see them: a "gap" would reveal that a hidden book exists at that position.
    kid = (user or {}).get("access_scope") == "kids"
    gaps = {} if kid else _gap_counts([r["id"] for r in rows])
    for r in rows:
        r["gap_count"] = gaps.get(r["id"], 0)
        del r["has_assertions"]
        del r["has_corroborated_assertion"]
        del r["sort_name"]
    return {"items": rows, "total": total}


def _gap_counts(series_ids: list[str]) -> dict[str, int]:
    """gap_count for many series from one membership scan per 500 ids."""
    positions: dict[str, list[str | None]] = {}
    cap = seriesnorm.load_options().max_position
    for i in range(0, len(series_ids), 500):
        chunk = series_ids[i:i + 500]
        for r in db.all(
            f"SELECT series_id,position FROM series_memberships WHERE series_id IN ({','.join('?' * len(chunk))})",  # nosec B608
            tuple(chunk),
        ):
            positions.setdefault(r["series_id"], []).append(r["position"])
    return {sid: len(_gaps_from_positions(ps, cap)) for sid, ps in positions.items()}


def _position_number(value: str | None, max_position: int | None = None) -> float | None:
    """The leading number of a SANE position (see seriesnorm.position_problem); None for a
    publisher catalog number, a year, an ISBN fragment or text without a number."""
    return seriesnorm.sane_position_value(value, max_position)


def _gaps_for(series_id: str) -> list[int]:
    """Integer positions between the lowest and highest known entries with no work.

    Purely a statement about what this install already knows: it cannot tell you
    position 8 exists if nothing in the local catalog or its metadata graph has ever
    mentioned it. See discover_more() for the best-effort provider-assisted check.
    """
    return _gaps_from_positions(
        [r["position"] for r in db.all("SELECT position FROM series_memberships WHERE series_id=?", (series_id,))],
        seriesnorm.load_options().max_position,
    )


def _gaps_from_positions(raw_positions: list[str | None], max_position: int | None = None) -> list[int]:
    """Missing integers between the lowest and highest SANE position. Positions that look
    like catalog numbers/years are ignored, a series needs two positioned members to
    have a numbering at all, and the range never exceeds the plausible maximum."""
    cap = max_position or seriesnorm.DEFAULT_MAX_POSITION
    values = [v for v in (_position_number(r, cap) for r in raw_positions) if v is not None]
    if len(values) < 2:
        return []
    positions = sorted({int(v) for v in values if float(v).is_integer() and 0 <= v <= cap})
    if len(positions) < 2:
        return []
    have = set(positions)
    return [n for n in range(positions[0], min(positions[-1], cap) + 1) if n not in have]


def series_detail(series_id: str, user: dict[str, Any] | None = None) -> dict[str, Any] | None:
    s = db.one(
        """SELECT s.*,sm.policy,sm.ebook monitor_ebook,sm.audiobook monitor_audiobook,sm.last_refresh_at,
                  COALESCE(sm.audiobook_policy,CASE WHEN sm.audiobook=1 THEN sm.policy END,CASE WHEN sm.policy IS NOT NULL THEN 'none' END) audiobook_policy,
                  COALESCE(sm.ebook_policy,CASE WHEN sm.ebook=1 THEN sm.policy END,CASE WHEN sm.policy IS NOT NULL THEN 'none' END) ebook_policy
           FROM series s LEFT JOIN series_monitors sm ON sm.series_id=s.id WHERE s.id=?""",
        (series_id,),
    )
    if not s:
        return None
    visible, vparams = tagging.kids_visibility(user)
    works = db.all(
        f"""SELECT w.id,w.title,w.cover_url,w.first_publish_date,w.monitored,w.metadata_status,smem.position
           FROM series_memberships smem JOIN works w ON w.id=smem.work_id AND {visible}
           WHERE smem.series_id=? ORDER BY
             CASE WHEN smem.position GLOB '*[0-9]*' THEN CAST(smem.position AS REAL) ELSE 999999 END,
             w.sort_title,w.title""",  # nosec B608
        (*vparams, series_id),
    )
    for w in works:
        w["assets"] = db.all("SELECT id,kind,format,status,monitored FROM media_assets WHERE work_id=? AND derived_from_asset_id IS NULL ORDER BY kind", (w["id"],))
        w["authors"] = db.all(
            "SELECT c.id,c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord",
            (w["id"],),
        )
        w.update(ownership.summarize(w["assets"], bool(w.get("monitored", 1))))
    # Counted per Work, never per asset: a book that owns an ebook and an audiobook is one owned book.
    s["owned_count"] = sum(1 for w in works if w["owned_kinds"])
    s["partial_count"] = sum(1 for w in works if w["state"] == "partial")
    s["works"] = works
    s["max_position"] = seriesnorm.load_options().max_position  # the numbering slots on the page stop here
    if (user or {}).get("access_scope") == "kids":
        # Numbering gaps are derived from ALL members; for a kid they would point at
        # hidden books. Visible members only, no "missing" positions.
        s["gaps"] = []
        s["work_count_visible"] = len(works)
    else:
        s["gaps"] = _gaps_for(series_id)
        _attach_shelf_state(works)
    return s


def _attach_shelf_state(works: list[dict[str, Any]]) -> None:
    """Admin/adult series page: each work's tags and whether it is on the kids shelf, in one
    query per 500 works, so the effect of a bulk tag action is visible per book."""
    for w in works:
        w["tags"] = []
    by_id = {w["id"]: w for w in works}
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in db.all(
            f"SELECT wt.work_id,t.id,t.name,t.kind FROM work_tags wt JOIN tags t ON t.id=wt.tag_id "  # nosec B608
            f"WHERE wt.work_id IN ({','.join('?' * len(chunk))}) ORDER BY t.name",
            tuple(chunk),
        ):
            by_id[r["work_id"]]["tags"].append({"id": r["id"], "name": r["name"], "kind": r["kind"], "label": tagging.tag_label(r["name"])})
    for w in works:
        w["on_kids_shelf"] = any(t["name"] == tagging.KIDS_TAG_NAME for t in w["tags"])


# Kept identical to main.STATUS_ON_INTENT - see the note there.
STATUS_ON_INTENT = "status=CASE WHEN status='owned' THEN status WHEN ?=1 THEN 'missing' ELSE 'none' END"


KIND_POLICIES = {"none", "future", "all"}


def _legacy_to_kinds(policy: str, ebook: bool, audiobook: bool) -> tuple[str, str]:
    """(audiobook_policy, ebook_policy) for a legacy {policy, ebook, audiobook} body."""
    return (policy if audiobook else "none", policy if ebook else "none")


def _kinds_to_legacy(audiobook_policy: str, ebook_policy: str) -> tuple[str, bool, bool]:
    """(policy, ebook, audiobook) kept in sync on the old columns for old clients."""
    kinds = {audiobook_policy, ebook_policy}
    policy = "all" if "all" in kinds else "future" if "future" in kinds else "none"
    return policy, ebook_policy != "none", audiobook_policy != "none"


def stored_kind_policies(series_id: str) -> tuple[str, str]:
    """Current (audiobook_policy, ebook_policy); rows not yet backfilled fall back to the legacy columns."""
    row = db.one("SELECT * FROM series_monitors WHERE series_id=?", (series_id,))
    if not row:
        return "none", "none"
    legacy = _legacy_to_kinds(row["policy"], bool(row["ebook"]), bool(row["audiobook"]))
    return row.get("audiobook_policy") or legacy[0], row.get("ebook_policy") or legacy[1]


def set_series_monitor(
    series_id: str,
    *,
    audiobook_policy: str | None = None,
    ebook_policy: str | None = None,
    policy: str | None = None,
    ebook: bool | None = None,
    audiobook: bool | None = None,
) -> None:
    """Record independent per-kind follow policies and apply them to current members.

    'all' sets acquisition intent for that kind on every member; 'future' only records
    the policy (apply_future_policy handles works added later); 'none' clears intent on
    members that are not owned. Owned assets are never demoted (STATUS_ON_INTENT).

    Per-kind arguments win; a kind left out keeps its stored policy. With neither given,
    the legacy {policy, ebook, audiobook} arguments are mapped onto the two kinds."""
    if audiobook_policy is None and ebook_policy is None:
        if policy not in {"all", "future", "none"}:
            raise ValueError("Invalid series monitor policy")
        audiobook_policy, ebook_policy = _legacy_to_kinds(policy, bool(ebook), True if audiobook is None else audiobook)
        _audiobook_explicit = _ebook_explicit = True
    elif audiobook_policy is None or ebook_policy is None:
        stored = stored_kind_policies(series_id)
        _audiobook_explicit = audiobook_policy is not None
        _ebook_explicit = ebook_policy is not None
        audiobook_policy = audiobook_policy or stored[0]
        ebook_policy = ebook_policy or stored[1]
    else:
        _audiobook_explicit = _ebook_explicit = True
    if audiobook_policy not in KIND_POLICIES or ebook_policy not in KIND_POLICIES:
        raise ValueError("Invalid series monitor policy")
    if not db.one("SELECT id FROM series WHERE id=?", (series_id,)):
        raise ValueError("Series not found")
    policy, ebook, audiobook = _kinds_to_legacy(audiobook_policy, ebook_policy)
    kinds = (("audiobook", audiobook_policy, _audiobook_explicit), ("ebook", ebook_policy, _ebook_explicit))
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO series_monitors(series_id,policy,ebook,audiobook,audiobook_policy,ebook_policy,started_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(series_id) DO UPDATE SET policy=excluded.policy,ebook=excluded.ebook,
               audiobook=excluded.audiobook,audiobook_policy=excluded.audiobook_policy,ebook_policy=excluded.ebook_policy""",
            (series_id, policy, int(ebook), int(audiobook), audiobook_policy, ebook_policy, db.now()),
        )
        member_ids = [r["work_id"] for r in conn.execute("SELECT work_id FROM series_memberships WHERE series_id=?", (series_id,)).fetchall()]
        for work_id in member_ids:
            cleared = False
            for kind, kind_policy, explicit in kinds:
                if kind_policy == "all":
                    conn.execute(
                        "UPDATE media_assets SET monitored=1,has_acquisition_intent=1," + STATUS_ON_INTENT + ",updated_at=? WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL",  # nosec B608
                        (1, db.now(), work_id, kind),
                    )
                elif kind_policy == "none" and explicit:
                    cleared |= conn.execute(
                        "UPDATE media_assets SET monitored=0,has_acquisition_intent=0," + STATUS_ON_INTENT + ",updated_at=? "  # nosec B608
                        "WHERE work_id=? AND kind=? AND status!='owned' AND (monitored=1 OR has_acquisition_intent=1)",
                        (0, db.now(), work_id, kind),
                    ).rowcount > 0
            if "all" in (audiobook_policy, ebook_policy):
                conn.execute("UPDATE works SET monitored=1,updated_at=? WHERE id=?", (db.now(), work_id))
            elif audiobook_policy == ebook_policy == "none":
                conn.execute("UPDATE works SET monitored=0,updated_at=? WHERE id=?", (db.now(), work_id))
            elif cleared:
                # Only a member whose intent this call actually removed can stop being monitored.
                conn.execute(
                    "UPDATE works SET monitored=EXISTS(SELECT 1 FROM media_assets WHERE work_id=works.id AND monitored=1),updated_at=? WHERE id=?",
                    (db.now(), work_id),
                )


def apply_future_policy(conn: Any, work_id: str, series_id: str) -> None:
    """Hook for a work newly added to a series: kinds whose policy is 'future' or 'all'
    get acquisition intent (the series analogue of monitoring._apply_asset_monitoring).
    Runs inside the caller's transaction."""
    row = conn.execute("SELECT * FROM series_monitors WHERE series_id=?", (series_id,)).fetchone()
    if not row:
        return
    legacy = _legacy_to_kinds(row["policy"], bool(row["ebook"]), bool(row["audiobook"]))
    wanted = [
        kind for kind, value in (("audiobook", row["audiobook_policy"] or legacy[0]), ("ebook", row["ebook_policy"] or legacy[1]))
        if value in ("future", "all")
    ]
    if not wanted:
        return
    conn.execute("UPDATE works SET monitored=1,updated_at=? WHERE id=?", (db.now(), work_id))
    for kind in wanted:
        conn.execute(
            "UPDATE media_assets SET monitored=1,has_acquisition_intent=1," + STATUS_ON_INTENT + ",updated_at=? WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL",  # nosec B608
            (1, db.now(), work_id, kind),
        )


def discover_more(series_id: str) -> dict[str, Any]:
    """Best-effort: ask the metadata provider for anything else with this series' name.

    Not a confident auto-add - a series name search is loose (title/author fuzziness
    both ways), so this only ever surfaces candidates already excluding what the
    catalog holds, for the operator to accept explicitly via the normal Add flow.
    """
    s = db.one("SELECT * FROM series WHERE id=?", (series_id,))
    if not s:
        raise ValueError("Series not found")
    known_titles = {r["title"].casefold().strip() for r in db.all(
        "SELECT w.title FROM series_memberships sm JOIN works w ON w.id=sm.work_id WHERE sm.series_id=?", (series_id,)
    )}
    try:
        results = metadata_service.search(s["name"], limit=20)
    except Exception as exc:
        return {"series": s["name"], "candidates": [], "error": str(exc)}
    candidates = [
        r for r in results
        if r.get("title", "").casefold().strip() not in known_titles
        and any((sr.get("name") or "").casefold().strip() == s["name"].casefold().strip() for sr in (r.get("series") or []))
    ]
    return {"series": s["name"], "candidates": candidates}


def dedupe_series() -> dict[str, Any]:
    """One-time (idempotent) cleanup of series rows imported before bookdoe did
    its own name-based dedup (see seriesnorm.py and metadata._link_series):
    drop rows that are actually publisher-imprint/library-classification strings,
    then merge rows whose names are the same series under seriesnorm.norm_key.

    Safe to re-run: a library with no such rows left does nothing on either pass.
    """
    dropped = merged = 0
    with db.tx() as conn:
        for r in conn.execute("SELECT id, name FROM series").fetchall():
            if seriesnorm.is_junk(r["name"]):
                conn.execute("DELETE FROM series_memberships WHERE series_id=?", (r["id"],))
                conn.execute("DELETE FROM series WHERE id=?", (r["id"],))
                dropped += 1
        # A name stored with its position still attached ("39 Clues: Unstoppable --
        # book two", from before bookdoe extracted positions on ingestion): move
        # the position onto the links and key the row by the real name, so it merges
        # with a sibling row that already has the bare form below.
        for r in conn.execute("SELECT id, name FROM series").fetchall():
            base, pos = seriesnorm.split_position(r["name"])
            if pos and base != r["name"]:
                conn.execute(
                    "UPDATE series_memberships SET position=? WHERE series_id=? AND position IS NULL",
                    (pos, r["id"]),
                )
                conn.execute(
                    "UPDATE series SET name=?,sort_name=?,norm_key=? WHERE id=?",
                    (base, metadata_service._sort_title(base), seriesnorm.norm_key(base), r["id"]),
                )
        for r in conn.execute("SELECT id, name FROM series WHERE norm_key IS NULL").fetchall():
            conn.execute("UPDATE series SET norm_key=? WHERE id=?", (seriesnorm.norm_key(r["name"]), r["id"]))

        rows = conn.execute(
            """SELECT s.id, s.name, s.norm_key, COUNT(sm.work_id) n FROM series s
               LEFT JOIN series_memberships sm ON sm.series_id=s.id GROUP BY s.id ORDER BY s.id"""
        ).fetchall()
        groups: list[list[Any]] = []
        for r in rows:
            for g in groups:
                if seriesnorm.similar(r["norm_key"], g[0]["norm_key"]):
                    g.append(r)
                    break
            else:
                groups.append([r])
        for g in groups:
            if len(g) < 2:
                continue
            best = seriesnorm.pick_canonical([r["name"] for r in g for _ in range(max(r["n"], 1))])
            keep = next(r for r in g if r["name"] == best)
            for r in g:
                if r["id"] == keep["id"]:
                    continue
                conn.execute(
                    """INSERT INTO series_memberships(work_id,series_id,position)
                       SELECT work_id, ?, position FROM series_memberships WHERE series_id=?
                       ON CONFLICT(work_id,series_id) DO UPDATE
                       SET position=COALESCE(series_memberships.position, excluded.position)""",
                    (keep["id"], r["id"]),
                )
                conn.execute("DELETE FROM series_memberships WHERE series_id=?", (r["id"],))
                conn.execute("DELETE FROM series WHERE id=?", (r["id"],))
                merged += 1
            display = keep["name"] if seriesnorm.is_bare(keep["name"]) else seriesnorm.bare_form(keep["name"])
            conn.execute(
                "UPDATE series SET name=?,sort_name=?,norm_key=? WHERE id=?",
                (display, metadata_service._sort_title(display), seriesnorm.norm_key(display), keep["id"]),
            )
    return {"dropped": dropped, "merged": merged}


def confirm_assertion(assertion_id: str) -> dict[str, Any]:
    """Mark an assertion as manually_confirmed; ensure the membership exists.

    Idempotent: confirming an already-confirmed assertion is a no-op.
    """
    t = db.now()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id, work_id, series_id, status, proposed_position FROM series_assertions WHERE id=?",
            (assertion_id,),
        ).fetchone()
        if not row:
            raise ValueError("Assertion not found")
        if row["status"] == "manually_confirmed":
            return {"id": row["id"], "status": "manually_confirmed", "changed": False}

        conn.execute(
            "UPDATE series_assertions SET status='manually_confirmed', updated_at=? WHERE id=?",
            (t, assertion_id),
        )
        # Ensure the membership exists (position from assertion if none).
        if row["series_id"]:
            conn.execute(
                "INSERT OR REPLACE INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
                (row["work_id"], row["series_id"], row["proposed_position"]),
            )
        work_id = row["work_id"]
        series_id = row["series_id"]
    db.add_history(
        "series.confirmed",
        f"Series assertion {assertion_id} manually confirmed",
        work_id=work_id,
        data={"assertion_id": assertion_id, "series_id": series_id},
    )
    return {"id": row["id"], "status": "manually_confirmed", "changed": True}


def reject_assertion(assertion_id: str) -> dict[str, Any]:
    """Mark an assertion as rejected; remove membership only if no other support.

    If the membership is supported by another accepted/confirmed assertion,
    the membership row stays (the work belongs to the series for other reasons).
    """
    t = db.now()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id, work_id, series_id, status FROM series_assertions WHERE id=?",
            (assertion_id,),
        ).fetchone()
        if not row:
            raise ValueError("Assertion not found")
        if row["status"] == "rejected":
            return {"id": row["id"], "status": "rejected", "changed": False}

        conn.execute(
            "UPDATE series_assertions SET status='rejected', updated_at=? WHERE id=?",
            (t, assertion_id),
        )
        # Remove membership only if this was the only support.
        work_id = row["work_id"]
        series_id = row["series_id"]
        if row["series_id"]:
            other = conn.execute(
                """SELECT COUNT(*) c FROM series_assertions
                   WHERE work_id=? AND series_id=? AND status IN ('auto_accepted','accepted','manually_confirmed')
                   AND id!=?""",
                (row["work_id"], row["series_id"], assertion_id),
            ).fetchone()["c"]
            if other == 0:
                conn.execute(
                    "DELETE FROM series_memberships WHERE work_id=? AND series_id=?",
                    (row["work_id"], row["series_id"]),
                )
    db.add_history(
        "series.rejected",
        f"Series assertion {assertion_id} manually rejected",
        work_id=work_id,
        data={"assertion_id": assertion_id, "series_id": series_id},
    )
    return {"id": row["id"], "status": "rejected", "changed": True}


def suppress_series_for_work(work_id: str, series_id: str) -> dict[str, Any]:
    """Suppress a series for a specific work.

    Creates a manually_suppressed assertion, removes the membership, and ensures
    future automatic _link_series calls for this (work, series) pair do not
    recreate the membership.
    """
    t = db.now()
    with db.tx() as conn:
        # Check for existing suppression.
        existing = conn.execute(
            """SELECT id FROM series_assertions
               WHERE work_id=? AND series_id=? AND status='manually_suppressed'""",
            (work_id, series_id),
        ).fetchone()
        if existing:
            return {"work_id": work_id, "series_id": series_id, "status": "manually_suppressed", "changed": False}

        # Remove membership.
        conn.execute(
            "DELETE FROM series_memberships WHERE work_id=? AND series_id=?",
            (work_id, series_id),
        )
        # Create suppressed assertion.
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.uid(), work_id, series_id, "manual", "", "", None, "manual_suppression", 1.0,
             "manually_suppressed", t, t),
        )
    db.add_history(
        "series.suppressed",
        f"Series {series_id} suppressed for work {work_id}",
        work_id=work_id,
        data={"series_id": series_id},
    )
    return {"work_id": work_id, "series_id": series_id, "status": "manually_suppressed", "changed": True}


def list_assertions_for_work(work_id: str) -> list[dict[str, Any]]:
    """List all assertions for a work, with status and evidence."""
    return db.all(
        """SELECT id, work_id, series_id, source, raw_value, normalized_name,
                   proposed_position, evidence_type, confidence, status, created_at, updated_at
           FROM series_assertions WHERE work_id=? ORDER BY created_at DESC""",
        (work_id,),
    )
