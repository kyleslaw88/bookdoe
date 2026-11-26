"""What a Work owns versus what it still wants (Library State filter, card chips, placeholder cleanup).

Ownership is judged per KIND (ebook / audiobook), never per asset row. A kind is *owned* when any asset of it is
owned (a converted copy counts, it is a real file of that medium); it is *wanted* when a monitored, original
(not converted) asset of that kind exists. A Work's state is then:

  complete      owns something, and every wanted kind is owned
  partial       owns something, and at least one wanted kind is not owned  ("Partly owned")
  nothing       owns nothing but wants something                          ("Nothing owned")

Judging per kind is what stops a redundant empty ebook placeholder sitting beside the OWNED ebook from making the
book look "missing": the placeholder never counts against a kind that is owned.

Legacy ``status`` query values keep working (bookmarked URLs): ``owned`` is "owns something" (the Library default),
``missing`` means the same as ``partial``.

Placeholder cleanup removes only provably redundant rows: a non-converted, format-less, not-owned asset with no files
that sits beside an OWNED asset of the same kind and that nothing else references except history and search state.
It is idempotent, audited with one history event per Work that carries the deleted rows, and the wish it expressed
(monitored / acquisition intent) is OR-ed into the owned asset, so nothing is lost and monitoring recreates the slot.
"""
from __future__ import annotations

import contextlib
import sqlite3
from typing import Any, Iterable, Iterator

from .. import db

KINDS = ("ebook", "audiobook")
STATES = ("complete", "partial", "nothing")
LEGACY = {"missing": "partial"}
KIND_LABEL = {"ebook": "ebook", "audiobook": "audiobook"}

OWNS_ANY = "EXISTS (SELECT 1 FROM media_assets ox WHERE ox.work_id=w.id AND ox.status='owned')"
# A wanted kind: some original monitored asset of it is not owned, and no asset of that kind is owned.
WANTS_MISSING = ("EXISTS (SELECT 1 FROM media_assets mx WHERE mx.work_id=w.id AND mx.derived_from_asset_id IS NULL AND mx.monitored=1 "
                 "AND mx.status<>'owned' AND w.monitored=1 AND NOT EXISTS (SELECT 1 FROM media_assets ox2 WHERE ox2.work_id=w.id AND ox2.kind=mx.kind AND ox2.status='owned'))")
WANTS_ANY = "EXISTS (SELECT 1 FROM media_assets wx WHERE wx.work_id=w.id AND wx.derived_from_asset_id IS NULL AND wx.monitored=1 AND wx.status<>'owned' AND w.monitored=1)"

STATE_LABELS = {"": "Any state", "complete": "Complete", "partial": "Partly owned", "nothing": "Nothing owned", "owned": "Owned something", "missing": "Partly owned"}


def normalize_state(value: str | None) -> str | None:
    """The state a ``status`` query value stands for ('' / None when it is not a state filter)."""
    v = (value or "").strip().lower()
    return LEGACY.get(v, v) if v in STATES or v in LEGACY else None


def state_clause(state: str) -> str:
    """SQL (over works alias ``w``) for one state; the caller adds the owned-something default where it applies."""
    if state == "complete":
        return f"({OWNS_ANY} AND NOT {WANTS_MISSING})"
    if state == "partial":
        return f"({OWNS_ANY} AND {WANTS_MISSING})"
    if state == "nothing":
        return f"(NOT {OWNS_ANY} AND {WANTS_ANY})"
    raise ValueError(f"unknown state {state}")


def summarize(assets: Iterable[dict[str, Any]], work_monitored: bool = True) -> dict[str, Any]:
    """The same judgement in Python for one Work's asset rows: state, owned kinds, missing (wanted, not owned) kinds
    and the wanted asset behind each (the book page lists them as "Wanted: Audiobook" with Search / Unmonitor)."""
    rows = list(assets)
    # An unmonitored Work (works.monitored=0) wants nothing: its monitored placeholders do not make it "Partly owned".
    if not work_monitored:
        rows = [a for a in rows if a.get("status") == "owned"]
    owned = {a["kind"] for a in rows if a.get("status") == "owned"}
    wanted: dict[str, dict[str, Any]] = {}
    for a in rows:
        if a.get("derived_from_asset_id") or not a.get("monitored") or a.get("status") == "owned" or a["kind"] in owned:
            continue
        wanted.setdefault(a["kind"], {"asset_id": a["id"], "kind": a["kind"], "status": a.get("status")})
    if owned:
        state = "partial" if wanted else "complete"
    else:
        state = "nothing" if wanted else "none"
    return {"state": state, "owned_kinds": sorted(owned), "missing_formats": sorted(wanted), "wanted": [wanted[k] for k in sorted(wanted)]}


def state_counts(where: str, params: Iterable[Any]) -> dict[str, int]:
    """Works per state under the other filters (``where`` is the library WHERE clause without a state filter and
    without the owned-something default). 'nothing' only counts books that are wanted."""
    p = tuple(params)
    row = db.one(
        f"SELECT COUNT(CASE WHEN {OWNS_ANY} THEN 1 END) n_owned,"  # nosec B608
        f" COUNT(CASE WHEN {OWNS_ANY} AND NOT {WANTS_MISSING} THEN 1 END) n_complete,"
        f" COUNT(CASE WHEN {OWNS_ANY} AND {WANTS_MISSING} THEN 1 END) n_partial,"
        f" COUNT(CASE WHEN NOT {OWNS_ANY} AND {WANTS_ANY} THEN 1 END) n_nothing"
        f" FROM works w WHERE {where}", p)
    return {k: int(row["n_" + k] or 0) for k in ("owned", "complete", "partial", "nothing")}


# ---------------------------------------------------------------------------
# Placeholder assets
# ---------------------------------------------------------------------------

# Rows that may reference a placeholder without making it worth keeping: the history trail (asset_id is SET NULL
# on delete) and the automatic search's cooldown bookkeeping.
HARMLESS_REFERENCES = {("history", "asset_id"), ("wanted_search_state", "asset_id")}
EVENT_CLEANUP = "assets.placeholder_cleanup"


def has_owned_of_kind(conn: sqlite3.Connection, work_id: str, kind: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM media_assets WHERE work_id=? AND kind=? AND status='owned' LIMIT 1", (work_id, kind)).fetchone())


def may_create_placeholder(conn: sqlite3.Connection, work_id: str, kind: str) -> bool:
    """Creation guard: a Work gets an empty slot for a kind only when it has NO asset of that kind (an owned one, or an
    existing slot to reuse, makes a second row redundant)."""
    return not conn.execute("SELECT 1 FROM media_assets WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL LIMIT 1", (work_id, kind)).fetchone()


def _blocking_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every (table, column) that points at media_assets, except the harmless ones. Derived from the schema so a table
    added later blocks cleanup (safe) instead of being ignored."""
    from . import workmerge
    return [(d["table"], d["column"]) for d in workmerge.dependent_tables(conn)
            if d["parent"] == "media_assets" and (d["table"], d["column"]) not in HARMLESS_REFERENCES]


def is_redundant_placeholder(conn: sqlite3.Connection, asset: sqlite3.Row | dict[str, Any], blocking: list[tuple[str, str]] | None = None) -> bool:
    """True when ``asset`` is an empty, format-less, not-owned original beside an owned asset of its kind with no data of its own."""
    a = dict(asset)
    if a["status"] == "owned" or a["format"] is not None or a["derived_from_asset_id"]:
        return False
    if not has_owned_of_kind(conn, a["work_id"], a["kind"]):
        return False
    for table, col in blocking if blocking is not None else _blocking_columns(conn):
        if conn.execute(f'SELECT 1 FROM "{table}" WHERE "{col}"=? LIMIT 1', (a["id"],)).fetchone():  # nosec B608
            return False
    return True


def find_redundant_placeholders(conn: sqlite3.Connection, work_id: str | None = None) -> list[dict[str, Any]]:
    """Redundant placeholder rows (optionally of one Work), each with the owned asset it sits beside."""
    blocking = _blocking_columns(conn)
    sql = ("SELECT p.*,w.title FROM media_assets p JOIN works w ON w.id=p.work_id WHERE p.status<>'owned' AND p.format IS NULL "
           "AND p.derived_from_asset_id IS NULL AND EXISTS (SELECT 1 FROM media_assets o WHERE o.work_id=p.work_id AND o.kind=p.kind AND o.status='owned')")
    params: tuple = ()
    if work_id:
        sql += " AND p.work_id=?"
        params = (work_id,)
    out = []
    for p in conn.execute(sql + " ORDER BY w.title,p.kind,p.id", params).fetchall():
        if is_redundant_placeholder(conn, p, blocking):
            out.append(dict(p))
    return out


@contextlib.contextmanager
def _conn(write: bool) -> Iterator[sqlite3.Connection]:
    """Apply: the ordinary write transaction (BEGIN IMMEDIATE under the global write lock). Dry run: a read-only connection,
    so looking never takes the write lock and never blocks (or waits for) a merge, scan or import."""
    if write:
        with db.tx() as conn:
            yield conn
        return
    conn = db.connect(readonly=True)
    try:
        yield conn
    finally:
        conn.close()


def cleanup_placeholders(apply: bool = False, work_id: str | None = None, user: Any = None, limit: int = 5000) -> dict[str, Any]:
    """Dry run (``apply=False``) or removal of redundant placeholder assets. Idempotent: a second run finds nothing.
    The wish a placeholder carried (monitored, acquisition intent) is OR-ed into the owned asset first, and every
    removed row is written to the history of its Work, so the cleanup is audited and the slot can be recreated."""
    by = user.get("id") if isinstance(user, dict) else user
    with _conn(apply) as conn:
        found = find_redundant_placeholders(conn, work_id)
        kept_back = found[limit:]
        found = found[:limit]
        items = [{"asset_id": p["id"], "work_id": p["work_id"], "title": p["title"], "kind": p["kind"], "monitored": bool(p["monitored"])} for p in found]
        removed = 0
        if apply:
            t = db.now()
            per_work: dict[str, list[dict[str, Any]]] = {}
            for p in found:
                owned = conn.execute("SELECT id,monitored,has_acquisition_intent FROM media_assets WHERE work_id=? AND kind=? AND status='owned' ORDER BY (format IS NULL),added_at LIMIT 1",
                                     (p["work_id"], p["kind"])).fetchone()
                if p["monitored"] or p["has_acquisition_intent"]:
                    conn.execute("UPDATE media_assets SET monitored=?,has_acquisition_intent=?,updated_at=? WHERE id=?",
                                 (int(bool(owned["monitored"] or p["monitored"])), int(bool(owned["has_acquisition_intent"] or p["has_acquisition_intent"])), t, owned["id"]))
                conn.execute("DELETE FROM media_assets WHERE id=?", (p["id"],))
                per_work.setdefault(p["work_id"], []).append({k: p[k] for k in p if k != "title"})
                removed += 1
            for wid, rows in per_work.items():
                conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                             (db.uid(), wid, None, EVENT_CLEANUP, f"Removed {len(rows)} redundant empty placeholder asset(s)",
                              db.json_dump({"by": by, "assets": rows}), t))
    return {"dry_run": not apply, "found": len(items), "removed": removed, "remaining": len(kept_back), "items": items[:200], "works": len({i["work_id"] for i in items})}
