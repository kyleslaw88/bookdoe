"""Family controls: who may approve for whom, what a kid may see beyond the kids shelf,
and the request workflow that feeds the existing acquisition engine.

Authorization lives here, not in the routes. Every function that acts on a kid takes
the acting user and refuses unless `can_act_for(actor, kid)` holds: an administrator
may act for anyone, an adult only for kids an administrator assigned to them
(`approver_assignments`). An approver never gets a kid's reading state through this
module; it only knows about requests and grants.

Requests never create a second acquisition engine. Approving one records intent on the
work's media assets exactly like the Monitor toggle does (`STATUS_ON_INTENT`), after
which the wanted search, grab, download and import pipeline takes over unchanged. The
friendly status a person sees is derived from that pipeline's state every time it is
read, never stored.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import auth, db
from . import metadata, ownership, request_search

log = logging.getLogger(__name__)

KINDS = ("ebook", "audiobook")
ADULT_POLICY_KEY = "requests.adult_policy"          # 'auto' (default) | 'approve'
KID_EXTERNAL_SEARCH_KEY = "requests.kid_external_search"  # bool, default True
SEARCH_ON_APPROVAL_KEY = "requests.search_on_approval"    # bool, default True
# Kept identical to main.STATUS_ON_INTENT and series.STATUS_ON_INTENT.
STATUS_ON_INTENT = "status=CASE WHEN status='owned' THEN status WHEN ?=1 THEN 'missing' ELSE 'none' END"

FRIENDLY = {
    "pending": ("Awaiting Approval", "warn"),
    "denied": ("Unavailable", "bad"),
    "cancelled": ("Cancelled", ""),
}


class Forbidden(PermissionError):
    """The actor may not act for this account (object-level denial)."""


class NotFound(LookupError):
    pass


# ---------- policy ----------
def adult_policy() -> str:
    return "approve" if db.get_setting(ADULT_POLICY_KEY, "auto") == "approve" else "auto"


def set_adult_policy(value: str) -> str:
    if value not in ("auto", "approve"):
        raise ValueError("adult_policy must be 'auto' or 'approve'")
    db.set_setting(ADULT_POLICY_KEY, value)
    return value


def kid_external_search() -> bool:
    return bool(db.get_setting(KID_EXTERNAL_SEARCH_KEY, True))


def search_on_approval() -> bool:
    """Search once for a book as soon as its request is approved (works with the scheduled
    wanted-search job off)."""
    return bool(db.get_setting(SEARCH_ON_APPROVAL_KEY, True))


# ---------- approver model ----------
def _user(user_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise NotFound("User not found")
    return row


def approver_kid_ids(user: dict[str, Any]) -> list[str]:
    """Kids this account approves for. Admins: every kid account."""
    if auth.account_type(user) == "admin":
        return [r["id"] for r in db.all("SELECT id FROM users WHERE access_scope='kids' ORDER BY username")]
    if auth.account_type(user) != "adult":
        return []
    return [r["kid_user_id"] for r in db.all(
        "SELECT a.kid_user_id FROM approver_assignments a JOIN users u ON u.id=a.kid_user_id "
        "WHERE a.approver_user_id=? AND u.access_scope='kids' ORDER BY u.username", (user["id"],))]


def can_act_for(actor: dict[str, Any], kid_id: str) -> bool:
    """Object-level rule: admin for anyone; adult only for an assigned kid that is still
    a kid (an assignment to an account later made adult or admin is inert)."""
    if auth.account_type(actor) == "admin":
        return True
    if auth.account_type(actor) != "adult":
        return False
    return db.one(
        "SELECT 1 FROM approver_assignments a JOIN users u ON u.id=a.kid_user_id "
        "WHERE a.approver_user_id=? AND a.kid_user_id=? AND u.access_scope='kids'", (actor["id"], kid_id),
    ) is not None


def require_act_for(actor: dict[str, Any], kid_id: str) -> dict[str, Any]:
    kid = db.one("SELECT * FROM users WHERE id=?", (kid_id,))
    # Deny before "not found" so an approver cannot probe which ids exist.
    if not kid or not can_act_for(actor, kid_id):
        raise Forbidden("You do not approve for this account")
    return kid


def assign_approver(admin: dict[str, Any], approver_id: str, kid_id: str) -> dict[str, Any]:
    """Admin only. The approver must be an adult, the kid a kid."""
    if auth.account_type(admin) != "admin":
        raise Forbidden("Only an administrator can assign approvers")
    approver, kid = _user(approver_id), _user(kid_id)
    if auth.account_type(approver) != "adult":
        raise ValueError("Only an adult account can be made an approver (administrators already approve everything)")
    if auth.account_type(kid) != "kid":
        raise ValueError("Approvers are assigned to kid accounts")
    with db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO approver_assignments(approver_user_id,kid_user_id,created_by,created_at) VALUES (?,?,?,?)",
                     (approver_id, kid_id, admin["id"], db.now()))
    db.add_history("approver.assigned", f"'{approver['username']}' now approves for '{kid['username']}' (by {admin['username']})",
                   data={"approver_id": approver_id, "kid_id": kid_id, "by": admin["id"]})
    return {"approver_id": approver_id, "kid_id": kid_id}


def remove_approver(admin: dict[str, Any], approver_id: str, kid_id: str) -> bool:
    if auth.account_type(admin) != "admin":
        raise Forbidden("Only an administrator can remove approvers")
    with db.tx() as conn:
        n = conn.execute("DELETE FROM approver_assignments WHERE approver_user_id=? AND kid_user_id=?", (approver_id, kid_id)).rowcount
    if n:
        a, k = db.one("SELECT username FROM users WHERE id=?", (approver_id,)), db.one("SELECT username FROM users WHERE id=?", (kid_id,))
        db.add_history("approver.removed", f"'{(a or {}).get('username', approver_id)}' no longer approves for '{(k or {}).get('username', kid_id)}' (by {admin['username']})",
                       data={"approver_id": approver_id, "kid_id": kid_id, "by": admin["id"]})
    return bool(n)


def approvers_of(kid_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT u.id,u.username,u.display_name,a.created_at FROM approver_assignments a JOIN users u ON u.id=a.approver_user_id "
        "WHERE a.kid_user_id=? ORDER BY u.username", (kid_id,))


def kids_for(user: dict[str, Any]) -> list[dict[str, Any]]:
    """The kid accounts this actor may act for, with pending-request counts."""
    ids = approver_kid_ids(user)
    out = []
    for kid_id in ids:
        k = db.one("SELECT id,username,display_name FROM users WHERE id=?", (kid_id,))
        if not k:
            continue
        k["pending_requests"] = db.one("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status='pending'", (kid_id,))["n"]
        k["grants"] = db.one("SELECT COUNT(*) n FROM content_grants WHERE user_id=?", (kid_id,))["n"]
        out.append(k)
    return out


# ---------- grants ----------
def grant_work(actor: dict[str, Any], kid_id: str, work_id: str, *, note: str | None = None) -> dict[str, Any]:
    kid = require_act_for(actor, kid_id)
    work = db.one("SELECT id,title FROM works WHERE id=?", (work_id,))
    if not work:
        raise NotFound("Work not found")
    with db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO content_grants(user_id,work_id,granted_by,note,created_at) VALUES (?,?,?,?,?)",
                     (kid_id, work_id, actor["id"], note, db.now()))
    db.add_history("grant.added", f"'{kid['username']}' may now read {work['title']} (by {actor['username']})",
                   work_id=work_id, data={"kid_id": kid_id, "by": actor["id"]})
    return {"kid_id": kid_id, "work_id": work_id, "granted": True}


def revoke_work(actor: dict[str, Any], kid_id: str, work_id: str) -> bool:
    kid = require_act_for(actor, kid_id)
    with db.tx() as conn:
        n = conn.execute("DELETE FROM content_grants WHERE user_id=? AND work_id=?", (kid_id, work_id)).rowcount
    if n:
        db.add_history("grant.removed", f"'{kid['username']}' lost access to a book (by {actor['username']})",
                       work_id=work_id, data={"kid_id": kid_id, "by": actor["id"]})
    return bool(n)


def grants_for(actor: dict[str, Any], kid_id: str) -> list[dict[str, Any]]:
    require_act_for(actor, kid_id)
    return db.all(
        "SELECT g.work_id,w.title,g.note,g.created_at,u.username granted_by FROM content_grants g JOIN works w ON w.id=g.work_id "
        "LEFT JOIN users u ON u.id=g.granted_by WHERE g.user_id=? ORDER BY w.sort_title,w.title", (kid_id,))


def grant_tag(admin: dict[str, Any], kid_id: str, tag_id: str) -> dict[str, Any]:
    """Collections (tags) widen a kid's shelf by many books at once, so this stays with
    administrators even though book-level grants are open to approvers."""
    if auth.account_type(admin) != "admin":
        raise Forbidden("Only an administrator can grant a whole collection")
    kid = _user(kid_id)
    tag = db.one("SELECT id,name FROM tags WHERE id=?", (tag_id,))
    if not tag:
        raise NotFound("Tag not found")
    with db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO tag_grants(user_id,tag_id,granted_by,created_at) VALUES (?,?,?,?)", (kid_id, tag_id, admin["id"], db.now()))
    db.add_history("grant.tag_added", f"'{kid['username']}' may now read everything tagged '{tag['name']}' (by {admin['username']})",
                   data={"kid_id": kid_id, "tag_id": tag_id, "by": admin["id"]})
    return {"kid_id": kid_id, "tag_id": tag_id, "granted": True}


def revoke_tag(admin: dict[str, Any], kid_id: str, tag_id: str) -> bool:
    if auth.account_type(admin) != "admin":
        raise Forbidden("Only an administrator can revoke a collection grant")
    with db.tx() as conn:
        n = conn.execute("DELETE FROM tag_grants WHERE user_id=? AND tag_id=?", (kid_id, tag_id)).rowcount
    if n:
        db.add_history("grant.tag_removed", "Collection grant removed", data={"kid_id": kid_id, "tag_id": tag_id, "by": admin["id"]})
    return bool(n)


def tag_grants_for(kid_id: str) -> list[dict[str, Any]]:
    return db.all("SELECT g.tag_id,t.name,g.created_at FROM tag_grants g JOIN tags t ON t.id=g.tag_id WHERE g.user_id=? ORDER BY t.name", (kid_id,))


# ---------- requests ----------
def _kinds(kinds: list[str] | None) -> list[str]:
    out = [k for k in (kinds or ["ebook"]) if k in KINDS]
    if not out:
        raise ValueError("kinds must include 'ebook' and/or 'audiobook'")
    return sorted(set(out))


def _record_intent(conn, work_id: str, kinds: list[str]) -> None:
    """The same statement the Monitor toggle runs (main.patch_asset), per requested kind,
    plus the work-level monitor flag the wanted search requires. An owned asset keeps
    its status; a slot that does not exist yet is created already wanted."""
    t = db.now()
    conn.execute("UPDATE works SET monitored=1,updated_at=? WHERE id=?", (t, work_id))
    for kind in kinds:
        row = conn.execute("SELECT id FROM media_assets WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL ORDER BY (format IS NOT NULL), added_at LIMIT 1", (work_id, kind)).fetchone()
        if row:
            conn.execute(f"UPDATE media_assets SET monitored=1,has_acquisition_intent=1,{STATUS_ON_INTENT},updated_at=? WHERE id=?", (1, t, row["id"]))  # nosec B608
            # Like the Monitor toggle (wanted_search.reset_cooldown(asset_id=...)): a fresh approval
            # must not be blocked by an old miss. Same statement, inside this transaction.
            conn.execute("DELETE FROM wanted_search_state WHERE asset_id=?", (row["id"],))
        elif ownership.may_create_placeholder(conn, work_id, kind):
            conn.execute(
                "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,?,'missing',1,1,?,?)",
                (db.uid(), work_id, kind, t, t),
            )


def _materialize_work(req: dict[str, Any]) -> str | None:
    """The library work a request points at, creating it from the provider record when
    the request came from an external search. None when the provider is unreachable:
    the request stays approved and is retried on the next read (see enrich)."""
    if req.get("work_id"):
        return req["work_id"]
    if not req.get("metadata_id"):
        return None
    existing = db.one("SELECT id FROM works WHERE metadata_id=?", (req["metadata_id"],))
    if existing:
        return existing["id"]
    try:
        meta = metadata.get_work(req["metadata_id"])
        wid = metadata.upsert_work(meta, monitored=True)
        db.add_history("library.added", f"Added {meta.get('title', 'work')} to library (request)", work_id=wid)
        return wid
    except Exception as exc:
        log.warning("request %s: could not create work from %s: %s", req["id"], req["metadata_id"], exc)
        return None


def _apply_approval(req_id: str) -> dict[str, Any]:
    """Make an approved request real: work exists, intent recorded, kid granted."""
    req = db.one("SELECT * FROM requests WHERE id=?", (req_id,))
    wid = _materialize_work(req)
    if not wid:
        return req
    kinds = db.json_load(req["kinds_json"], ["ebook"])
    with db.tx() as conn:
        conn.execute("UPDATE requests SET work_id=?,updated_at=? WHERE id=?", (wid, db.now(), req_id))
        _record_intent(conn, wid, kinds)
        if db.one("SELECT access_scope FROM users WHERE id=?", (req["user_id"],))["access_scope"] == "kids":
            conn.execute("INSERT OR IGNORE INTO content_grants(user_id,work_id,granted_by,note,created_at) VALUES (?,?,?,?,?)",
                         (req["user_id"], wid, req.get("decided_by"), "approved request", db.now()))
    request_search.trigger(req_id)   # only ever after approval; off-thread; never raises
    return db.one("SELECT * FROM requests WHERE id=?", (req_id,))


def submit(user: dict[str, Any], *, work_id: str | None = None, metadata_id: str | None = None, title: str | None = None,
           author: str | None = None, kinds: list[str] | None = None) -> dict[str, Any]:
    """A request from the signed-in account for itself. Kids wait for approval; adults
    follow the household policy (auto by default); administrators are approved at once."""
    kind = auth.account_type(user)
    if kind not in ("kid", "adult", "admin"):
        raise Forbidden("This account cannot request books")
    kinds = _kinds(kinds)
    if work_id:
        w = db.one("SELECT id,title FROM works WHERE id=?", (work_id,))
        if not w:
            raise NotFound("Work not found")
        title = w["title"]
        author = author or ", ".join(a["name"] for a in db.all(
            "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (work_id,)))
    elif metadata_id:
        if not (title or "").strip():
            raise ValueError("A request for a provider result needs its title")
        if kind == "kid" and not kid_external_search():
            raise Forbidden("Requesting books outside the library is turned off for kid accounts")
    else:
        raise ValueError("A request names a library work or a provider result")
    dup = db.one(
        "SELECT id FROM requests WHERE user_id=? AND status IN ('pending','approved') AND ((work_id IS NOT NULL AND work_id=?) OR (metadata_id IS NOT NULL AND metadata_id=?))",
        (user["id"], work_id, metadata_id))
    if dup:
        raise ValueError("You already have an open request for this book")
    needs_approval = kind == "kid" or (kind == "adult" and adult_policy() == "approve")
    rid, t = db.uid(), db.now()
    status = "pending" if needs_approval else "approved"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO requests(id,user_id,work_id,metadata_id,title,author,kinds_json,status,decided_by,decided_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, user["id"], work_id, metadata_id, (title or "").strip()[:300], (author or "").strip()[:300] or None, db.json_dump(kinds), status,
             None if needs_approval else user["id"], None if needs_approval else t, t, t),
        )
    db.add_history("request.submitted", f"'{user['username']}' requested {title} ({'/'.join(kinds)}; {status})",
                   work_id=work_id, data={"request_id": rid, "user_id": user["id"], "status": status})
    if status == "approved":
        _apply_approval(rid)
    return get(rid, user)


def decide(actor: dict[str, Any], req_id: str, decision: str, *, note: str | None = None) -> dict[str, Any]:
    """Approve or deny a pending request. The actor must be able to act for the
    requester (admin for anyone, approver for an assigned kid). Denial touches no
    asset: no intent, no download, nothing for the wanted search to find."""
    if decision not in ("approved", "denied"):
        raise ValueError("decision must be 'approved' or 'denied'")
    req = db.one("SELECT * FROM requests WHERE id=?", (req_id,))
    if not req or not can_act_for(actor, req["user_id"]):
        raise Forbidden("You do not approve requests for this account")
    if req["status"] != "pending":
        raise ValueError("This request has already been decided")
    t = db.now()
    with db.tx() as conn:
        conn.execute("UPDATE requests SET status=?,decided_by=?,decided_at=?,decision_note=?,updated_at=? WHERE id=?",
                     (decision, actor["id"], t, (note or "").strip()[:500] or None, t, req_id))
    requester = db.one("SELECT username FROM users WHERE id=?", (req["user_id"],))
    db.add_history(f"request.{decision}", f"{actor['username']} {decision} '{requester['username'] if requester else '?'}' request for {req['title']}",
                   work_id=req.get("work_id"), data={"request_id": req_id, "by": actor["id"], "note": note})
    if decision == "approved":
        _apply_approval(req_id)
    return enrich(db.one("SELECT * FROM requests WHERE id=?", (req_id,)))


def cancel(user: dict[str, Any], req_id: str) -> dict[str, Any]:
    """The requester withdraws their own pending request."""
    req = db.one("SELECT * FROM requests WHERE id=? AND user_id=?", (req_id, user["id"]))
    if not req:
        raise NotFound("Request not found")
    if req["status"] != "pending":
        raise ValueError("Only a pending request can be cancelled")
    with db.tx() as conn:
        conn.execute("UPDATE requests SET status='cancelled',updated_at=? WHERE id=?", (db.now(), req_id))
    return enrich(db.one("SELECT * FROM requests WHERE id=?", (req_id,)))


def friendly_status(req: dict[str, Any]) -> tuple[str, str]:
    """(label, tone) derived from the request and, once approved, from the acquisition
    pipeline: owned -> Available; an active download -> Being Prepared; intent recorded
    and waiting for a search -> Requested; approved but the work could not be created
    yet -> Approved."""
    if req["status"] in FRIENDLY:
        return FRIENDLY[req["status"]]
    wid = req.get("work_id")
    if not wid:
        return ("Approved", "info")
    kinds = db.json_load(req["kinds_json"], ["ebook"])
    assets = {a["kind"]: a for a in db.all("SELECT id,kind,status FROM media_assets WHERE work_id=? AND derived_from_asset_id IS NULL", (wid,))}
    if kinds and all(assets.get(k, {}).get("status") == "owned" for k in kinds):
        return ("Available", "good")
    if db.one(
        "SELECT 1 FROM downloads d JOIN media_assets a ON a.id=d.asset_id WHERE a.work_id=? AND d.state IN ('grabbed','downloading','completed','import_pending','importing')",
        (wid,)):
        return ("Being Prepared", "info")
    if any(assets.get(k, {}).get("status") in ("missing", "downloading") for k in kinds):
        return ("Requested", "info")
    return ("Approved", "info")


def enrich(req: dict[str, Any]) -> dict[str, Any]:
    if req["status"] == "approved" and not req.get("work_id") and req.get("metadata_id"):
        req = _apply_approval(req["id"])  # the provider may be back
    label, tone = friendly_status(req)
    kinds = db.json_load(req["kinds_json"], ["ebook"])
    out = {k: req.get(k) for k in ("id", "user_id", "work_id", "metadata_id", "title", "author", "status", "decided_at", "decision_note", "created_at", "updated_at")}
    out.update({"kinds": kinds, "friendly_status": label, "tone": tone, "search": request_search.status_for(req),
                "decided_by": (db.one("SELECT username FROM users WHERE id=?", (req["decided_by"],)) or {}).get("username") if req.get("decided_by") else None})
    return out


def get(req_id: str, user: dict[str, Any]) -> dict[str, Any]:
    req = db.one("SELECT * FROM requests WHERE id=?", (req_id,))
    if not req or (req["user_id"] != user["id"] and not can_act_for(user, req["user_id"])):
        raise NotFound("Request not found")
    return enrich(req)


def list_own(user: dict[str, Any], *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    total = db.one("SELECT COUNT(*) n FROM requests WHERE user_id=?", (user["id"],))["n"]
    rows = db.all("SELECT * FROM requests WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (user["id"], limit, offset))
    return {"items": [enrich(r) for r in rows], "total": total, "limit": limit, "offset": offset}


def list_for_approver(actor: dict[str, Any], *, status: str | None = "pending", limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Requests this actor may decide: admins see every account's; an approver sees
    only requests from kids assigned to them. Other adults' requests and other
    families' kids never appear, whatever the filter."""
    if auth.account_type(actor) == "admin":
        where, params = ["1=1"], []
    else:
        ids = approver_kid_ids(actor)
        if not ids:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        where, params = [f"r.user_id IN ({','.join('?' * len(ids))})"], list(ids)
    if status:
        where.append("r.status=?"); params.append(status)
    clause = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM requests r WHERE {clause}", tuple(params))["n"]  # nosec B608
    rows = db.all(f"SELECT r.*,u.username requester,u.display_name requester_name,u.access_scope requester_scope FROM requests r JOIN users u ON u.id=r.user_id WHERE {clause} ORDER BY r.created_at DESC LIMIT ? OFFSET ?",  # nosec B608
                  tuple(params + [limit, offset]))
    items = []
    for r in rows:
        item = enrich(r)
        item.update({"requester": r["requester"], "requester_name": r["requester_name"], "requester_type": "kid" if r["requester_scope"] == "kids" else "adult"})
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
