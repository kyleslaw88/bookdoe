"""Player API, version 1 (`/api/player/v1`).

The contract the Player shell, the PWA and future reading devices use. Everything is
scoped to the authenticated account (session, API key or device token) and to the
content that account may see; nothing here organises the library. Conventions:
compact JSON, `limit`/`offset` paging with `total`, `private` cache headers, ETags on
list and detail answers (see `_etag`), and plain `{"detail": ...}` errors. The
machine-readable contract is FastAPI's `/openapi.json`; `docs/PLAYER_API.md` is the
human one. Routes are added per phase; phase 2 covers personal state.
"""
from __future__ import annotations

import hashlib
import re
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import auth, db
from .services import reader, signin, tagging

router = APIRouter(prefix="/api/player/v1", tags=["player"])

PREF_KEYS = {"reader", "audio", "ui"}  # top-level sections a client may store
PREF_MAX_BYTES = 8192


def _etag(payload: Any) -> str:
    return '"' + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20] + '"'


def _conditional(request: Request, response: Response, payload: Any):
    """Attach an ETag; answer 304 when the client already holds this exact body."""
    tag = _etag(payload)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "private, no-cache"
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "private, no-cache"})
    return payload


def _visible_or_404(work_id: str, user: dict[str, Any]) -> None:
    if not db.one("SELECT 1 FROM works WHERE id=?", (work_id,)) or not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")


# ---------- preferences ----------
class PreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reader: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    ui: dict[str, Any] | None = None


def get_preferences(user_id: str) -> dict[str, Any]:
    row = db.one("SELECT prefs_json,updated_at FROM user_preferences WHERE user_id=?", (user_id,))
    prefs = db.json_load(row["prefs_json"], {}) if row else {}
    return {"reader": prefs.get("reader", {}), "audio": prefs.get("audio", {}), "ui": prefs.get("ui", {}),
            "updated_at": row["updated_at"] if row else None}


@router.get("/preferences")
def preferences(request: Request, response: Response, user=Depends(auth.require_user)):
    return _conditional(request, response, get_preferences(user["id"]))


@router.put("/preferences")
def put_preferences(req: PreferencesRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Merge the sections sent; sections omitted are kept. Per account, so two people
    on one tablet never share a font size."""
    current = get_preferences(user["id"])
    merged = {k: current[k] for k in PREF_KEYS}
    for k in PREF_KEYS:
        v = getattr(req, k)
        if v is not None:
            merged[k] = v
    raw = db.json_dump(merged)
    if len(raw.encode()) > PREF_MAX_BYTES:
        raise HTTPException(413, "Preferences are too large")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO user_preferences(user_id,prefs_json,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET prefs_json=excluded.prefs_json,updated_at=excluded.updated_at",
            (user["id"], raw, db.now()),
        )
    return get_preferences(user["id"])


# ---------- bookmarks ----------
class BookmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    location: str | None = Field(default=None, max_length=2000)
    position: float | None = Field(default=None, ge=0)
    percent: float | None = Field(default=None, ge=0, le=1)
    title: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    # 'highlight': a selected passage; `location` is its EPUB CFI range, `title` the selected text.
    kind: Literal["bookmark", "highlight"] = "bookmark"
    color: str | None = Field(default=None, max_length=20, pattern=r"^[A-Za-z0-9#_-]+$")


class BookmarkPatch(BaseModel):
    """Only the fields sent change; an explicit null clears a note or colour."""
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, max_length=20, pattern=r"^[A-Za-z0-9#_-]+$")


def _bookmark_public(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "asset_id", "work_id", "location", "position", "percent", "title", "note", "created_at")}
    out.update(kind=r.get("kind") or "bookmark", color=r.get("color"), updated_at=r.get("updated_at") or r["created_at"])
    return out


@router.get("/bookmarks")
def list_bookmarks(request: Request, response: Response, asset_id: str | None = None, work_id: str | None = None,
                   kind: Literal["bookmark", "highlight"] | None = None, limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0), user=Depends(auth.require_user)):
    visible, vparams = tagging.kids_visibility(user)
    where, params = ["b.user_id=?", visible], [user["id"], *vparams]
    if asset_id:
        where.append("b.asset_id=?"); params.append(asset_id)
    if work_id:
        where.append("b.work_id=?"); params.append(work_id)
    if kind:
        where.append("b.kind=?"); params.append(kind)
    clause = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM bookmarks b JOIN works w ON w.id=b.work_id WHERE {clause}", tuple(params))["n"]  # nosec B608
    rows = db.all(f"SELECT b.* FROM bookmarks b JOIN works w ON w.id=b.work_id WHERE {clause} ORDER BY b.created_at LIMIT ? OFFSET ?",  # nosec B608
                  tuple(params + [limit, offset]))
    return _conditional(request, response, {"items": [_bookmark_public(r) for r in rows], "total": total, "limit": limit, "offset": offset})


@router.post("/bookmarks", status_code=201)
def create_bookmark(req: BookmarkRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    asset = db.one("SELECT id,work_id FROM media_assets WHERE id=?", (req.asset_id,))
    if not asset or not tagging.is_visible_to(asset["work_id"], user):
        raise HTTPException(404, "Asset not found")
    if req.location is None and req.position is None:
        raise HTTPException(400, "A bookmark needs a location (ebook) or a position (audio)")
    if req.kind == "highlight" and not req.location:
        raise HTTPException(400, "A highlight needs a location (the selected passage)")
    bid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO bookmarks(id,user_id,asset_id,work_id,location,position,percent,title,note,created_at,kind,color,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, user["id"], req.asset_id, asset["work_id"], req.location, req.position, req.percent, req.title, req.note, t, req.kind, req.color, t),
        )
    return _bookmark_public(db.one("SELECT * FROM bookmarks WHERE id=?", (bid,)))


@router.patch("/bookmarks/{bookmark_id}")
def update_bookmark(bookmark_id: str, req: BookmarkPatch, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Edit a note, title or colour of one's own bookmark or highlight."""
    sent = [f for f in ("title", "note", "color") if f in req.model_fields_set]
    if not sent:
        raise HTTPException(400, "Nothing to change")
    sets = ", ".join(f"{f}=?" for f in sent)
    with db.tx() as conn:
        n = conn.execute(f"UPDATE bookmarks SET {sets}, updated_at=? WHERE id=? AND user_id=?",  # nosec B608
                         (*[getattr(req, f) for f in sent], db.now(), bookmark_id, user["id"])).rowcount
    if not n:
        raise HTTPException(404, "Bookmark not found")
    return _bookmark_public(db.one("SELECT * FROM bookmarks WHERE id=?", (bookmark_id,)))


@router.delete("/bookmarks/{bookmark_id}")
def delete_bookmark(bookmark_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    # Scoped to the owner in the statement itself: another account's id is simply "not found".
    with db.tx() as conn:
        n = conn.execute("DELETE FROM bookmarks WHERE id=? AND user_id=?", (bookmark_id, user["id"])).rowcount
    if not n:
        raise HTTPException(404, "Bookmark not found")
    return {"ok": True}


# ---------- favorites ----------
@router.get("/favorites")
def list_favorites(request: Request, response: Response, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                   user=Depends(auth.require_user)):
    from .main import _batch_library_summary  # the shared work summary (lazy: main imports this module)

    visible, vparams = tagging.kids_visibility(user)
    total = db.one(f"SELECT COUNT(*) n FROM favorites f JOIN works w ON w.id=f.work_id WHERE f.user_id=? AND {visible}", (user["id"], *vparams))["n"]  # nosec B608
    works = db.all(f"SELECT w.* FROM favorites f JOIN works w ON w.id=f.work_id WHERE f.user_id=? AND {visible} ORDER BY f.created_at DESC LIMIT ? OFFSET ?",  # nosec B608
                   (user["id"], *vparams, limit, offset))
    return _conditional(request, response, {"items": _batch_library_summary(works, user), "total": total, "limit": limit, "offset": offset})


@router.put("/favorites/{work_id}")
def add_favorite(work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    _visible_or_404(work_id, user)
    with db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO favorites(user_id,work_id,created_at) VALUES (?,?,?)", (user["id"], work_id, db.now()))
    return {"work_id": work_id, "favorite": True}


@router.delete("/favorites/{work_id}")
def remove_favorite(work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    with db.tx() as conn:
        conn.execute("DELETE FROM favorites WHERE user_id=? AND work_id=?", (user["id"], work_id))
    return {"work_id": work_id, "favorite": False}


# ---------- home: continue, recent ----------
def _continue_feed(user, kind, limit):
    """Books and in-progress podcast episodes for `kind`, interleaved by last played time."""
    from .services import podcast_queue

    books = [] if kind == "podcast" else reader.continue_items(user, kind=kind, limit=limit)
    episodes = []
    if kind in (None, "audiobook", "podcast"):
        with db.session() as conn:
            episodes = podcast_queue.continue_items(conn, user["id"], limit)
    return sorted([*books, *episodes], key=lambda i: i.get("updated_at") or 0, reverse=True)[:limit]


@router.get("/continue")
def continue_listing(request: Request, response: Response, kind: Literal["ebook", "audiobook", "podcast"] | None = None,
                     limit: int = Query(12, ge=1, le=50), user=Depends(auth.require_user)):
    """In-progress items for this account only. The reading and listening rows are
    separate because the Home screen shows them as two shelves. In-progress podcast episodes are
    interleaved by last played time into the listening shelf (``kind=audiobook``), into ``kind=podcast``
    (episodes only) and into the unfiltered feed. ``kind=ebook`` never contains episodes."""
    items = _continue_feed(user, kind, limit)
    return _conditional(request, response, {"items": items})


@router.get("/recent")
def recent(request: Request, response: Response, limit: int = Query(12, ge=1, le=60), user=Depends(auth.require_user)):
    """Recently added (visible, owned) and recently opened by this account."""
    from .main import _batch_library_summary

    visible, vparams = tagging.kids_visibility(user)
    added = db.all(
        f"""SELECT w.* FROM works w WHERE {visible} AND EXISTS (SELECT 1 FROM media_assets a JOIN media_files f ON f.asset_id=a.id
            WHERE a.work_id=w.id AND a.status='owned' AND f.missing=0) ORDER BY w.added_at DESC LIMIT ?""",  # nosec B608
        (*vparams, limit),
    )
    opened = db.all(
        f"""SELECT w.*, MAX(p.updated_at) opened_at FROM media_progress p JOIN works w ON w.id=p.work_id
            WHERE p.user_id=? AND {visible} GROUP BY w.id ORDER BY opened_at DESC LIMIT ?""",  # nosec B608
        (user["id"], *vparams, limit),
    )
    for w in opened:
        w.pop("opened_at", None)
    return _conditional(request, response, {"recently_added": _batch_library_summary(added, user), "recently_opened": _batch_library_summary(opened, user)})


@router.get("/home")
def home(request: Request, response: Response, user=Depends(auth.require_user)):
    """One call for the Home screen: who, continue reading, continue listening,
    recently added, recently opened, favorites count."""
    from .main import _batch_library_summary, _me_public

    visible, vparams = tagging.kids_visibility(user)
    added = db.all(
        f"""SELECT w.* FROM works w WHERE {visible} AND EXISTS (SELECT 1 FROM media_assets a JOIN media_files f ON f.asset_id=a.id
            WHERE a.work_id=w.id AND a.status='owned' AND f.missing=0) ORDER BY w.added_at DESC LIMIT 12""",  # nosec B608
        vparams,
    )
    payload = {
        "user": _me_public(user),
        "continue_reading": reader.continue_items(user, kind="ebook", limit=6),
        "continue_listening": _continue_feed(user, "audiobook", 6),
        "recently_added": _batch_library_summary(added, user),
        "favorites": db.one(f"SELECT COUNT(*) n FROM favorites f JOIN works w ON w.id=f.work_id WHERE f.user_id=? AND {visible}", (user["id"], *vparams))["n"],  # nosec B608
    }
    return _conditional(request, response, payload)


# ---------- discovery and requests ----------
from .services import family, metadata  # noqa: E402


def _family_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except family.Forbidden as exc:
        raise HTTPException(403, str(exc))
    except family.NotFound as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class RequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_id: str | None = None
    metadata_id: str | None = None
    title: str | None = Field(default=None, max_length=300)
    author: str | None = Field(default=None, max_length=300)
    kinds: list[Literal["ebook", "audiobook"]] = Field(default_factory=lambda: ["ebook"])


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=500)


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=300)


@router.get("/requests/search")
def request_search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(12, ge=1, le=30), user=Depends(auth.require_user)):
    """What this account can request for the words typed: books already on its shelf
    (nothing to request), books in the catalogue it cannot see yet (a kid asks for
    access; an adult asks for a missing format), and provider results that are not in
    the library at all. Provider errors degrade to an empty external list, never a 5xx."""
    from .main import _batch_library_summary

    like = db.like_pattern(q.strip())
    rows = db.all(
        """SELECT w.* FROM works w WHERE (w.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id
           WHERE wc.work_id=w.id AND c.name LIKE ? ESCAPE '\\')) ORDER BY COALESCE(w.sort_title,w.title) LIMIT ?""",
        (like, like, limit),
    )
    visible, vparams = tagging.kids_visibility(user)
    visible_ids = {r["id"] for r in db.all(f"SELECT w.id FROM works w WHERE {visible} AND w.id IN ({','.join('?' * len(rows))})", (*vparams, *(r["id"] for r in rows)))} if rows else set()  # nosec B608
    summaries = _batch_library_summary(rows, user)
    in_library = []
    for w in summaries:
        owned = sorted({a["kind"] for a in w["assets"] if a["status"] == "owned"})
        # A kid sees only title and author of a book off its shelf: enough to ask, not a
        # description, not files, not what else the household owns about it.
        entry = {"id": w["id"], "title": w["title"], "authors": [a["name"] for a in w["authors"]], "owned_kinds": owned,
                 "visible": w["id"] in visible_ids, "has_cover": w["has_cover"] if w["id"] in visible_ids else False}
        in_library.append(entry)
    external: list[dict[str, Any]] = []
    external_error = None
    if auth.account_type(user) != "kid" or family.kid_external_search():
        try:
            known = {r["metadata_id"] for r in rows if r.get("metadata_id")}
            for item in metadata.search(q.strip(), limit):
                if item.get("id") in known:
                    continue
                external.append({"metadata_id": item.get("id"), "title": item.get("title"), "authors": item.get("authors") or [a.get("name") for a in item.get("contributors", []) if isinstance(a, dict)],
                                 "year": item.get("first_publish_date") or item.get("year"), "cover_url": item.get("cover_url")})
        except Exception as exc:  # the provider being down must not break Discover
            external_error = "Provider search is unavailable right now"
            log_search_failure(exc)
    return {"query": q, "in_library": in_library, "external": external[:limit], "external_error": external_error}


def log_search_failure(exc: Exception) -> None:
    import logging
    logging.getLogger(__name__).info("request search: provider unavailable: %s", exc)


@router.get("/requests")
def my_requests(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), user=Depends(auth.require_user)):
    return family.list_own(user, limit=limit, offset=offset)


@router.post("/requests", status_code=201)
def create_request(req: RequestCreate, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    return _family_errors(family.submit, user, work_id=req.work_id, metadata_id=req.metadata_id, title=req.title, author=req.author, kinds=list(req.kinds))


@router.get("/requests/{request_id}")
def request_detail(request_id: str, user=Depends(auth.require_user)):
    return _family_errors(family.get, request_id, user)


@router.delete("/requests/{request_id}")
def cancel_request(request_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    return _family_errors(family.cancel, user, request_id)


# ---------- approvals (admins and designated approvers) ----------
def require_approver(request: Request) -> dict[str, Any]:
    user = auth.require_user(request)
    if user.get("_auth_method") == "device":
        # Deciding requests and granting books to a kid is an administrative act: a real
        # sign-in session only, never a reader's token nor a trusted browser's cookie.
        raise HTTPException(403, "Approvals need a normal signed-in session, not a trusted device")
    if auth.account_type(user) == "admin":
        return user
    if auth.account_type(user) == "adult" and family.approver_kid_ids(user):
        return user
    raise HTTPException(403, "You do not approve requests for anyone")


@router.get("/approvals")
def approvals(status: Literal["pending", "approved", "denied", "cancelled"] | None = "pending", limit: int = Query(100, ge=1, le=500),
              offset: int = Query(0, ge=0), user=Depends(require_approver)):
    return family.list_for_approver(user, status=status, limit=limit, offset=offset)


@router.post("/approvals/{request_id}/approve")
def approve(request_id: str, req: DecisionRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(require_approver)):
    return _family_errors(family.decide, user, request_id, "approved", note=req.note)


@router.post("/approvals/{request_id}/deny")
def deny(request_id: str, req: DecisionRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(require_approver)):
    return _family_errors(family.decide, user, request_id, "denied", note=req.note)


@router.get("/approvals/kids")
def approver_kids(user=Depends(require_approver)):
    return {"items": family.kids_for(user)}


@router.get("/approvals/kids/{kid_id}/grants")
def kid_grants(kid_id: str, user=Depends(require_approver)):
    return {"items": _family_errors(family.grants_for, user, kid_id)}


@router.put("/approvals/kids/{kid_id}/grants/{work_id}")
def kid_grant(kid_id: str, work_id: str, request: Request, req: GrantRequest | None = None, _=Depends(auth.require_csrf), user=Depends(require_approver)):
    return _family_errors(family.grant_work, user, kid_id, work_id, note=req.note if req else None)


@router.delete("/approvals/kids/{kid_id}/grants/{work_id}")
def kid_revoke(kid_id: str, work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(require_approver)):
    if not _family_errors(family.revoke_work, user, kid_id, work_id):
        raise HTTPException(404, "No such grant")
    return {"kid_id": kid_id, "work_id": work_id, "granted": False}


@router.get("/approvals/kids/{kid_id}/search")
def kid_grant_search(kid_id: str, q: str = Query(..., min_length=1, max_length=200), limit: int = Query(12, ge=1, le=30), user=Depends(require_approver)):
    """Library works an approver can grant to this kid, with whether each is already visible."""
    from .main import _batch_library_summary

    kid = _family_errors(family.require_act_for, user, kid_id)
    like = db.like_pattern(q.strip())
    rows = db.all(
        """SELECT w.* FROM works w WHERE EXISTS (SELECT 1 FROM media_assets a WHERE a.work_id=w.id AND a.status='owned')
           AND (w.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=w.id AND c.name LIKE ? ESCAPE '\\'))
           ORDER BY COALESCE(w.sort_title,w.title) LIMIT ?""", (like, like, limit))
    items = _batch_library_summary(rows, None)
    for w in items:
        w["visible_to_kid"] = tagging.is_visible_to(w["id"], kid)
        w["granted"] = db.one("SELECT 1 FROM content_grants WHERE user_id=? AND work_id=?", (kid_id, w["id"])) is not None
        for drop in ("assets", "tags", "metadata_status", "metadata_source", "monitored"):
            w.pop(drop, None)
    return {"items": items}


# ---------- identity, capabilities ----------
from . import __version__  # noqa: E402
from .config import settings  # noqa: E402
from .services import buildinfo, covers, derived as derived_svc, discover_chat, reader as reader_svc  # noqa: E402

API_VERSION = buildinfo.API_VERSION
PAGE_MAX = 200


@router.get("/me")
def me(request: Request, user=Depends(auth.require_user)):
    """Who the credential belongs to, its account type and what it may reach."""
    from .main import _me_public

    info = _me_public(user)
    kind = auth.account_type(user)
    info["scopes"] = ["player"] + (["approvals"] if info["approver_for"] or kind == "admin" else [])
    info["content_scope"] = "approved_only" if kind == "kid" else "all"
    info["management"] = False if user.get("_auth_method") == "device" else info["management"]
    info["csrf"] = auth.csrf_for_user(user)   # session or trusted-browser cookie; None for Bearer credentials
    return info


@router.get("/capabilities")
def capabilities():
    """Static facts a client needs before it signs in. Unauthenticated on purpose: a
    device shows the pairing instructions from this."""
    from . import setup as setup_svc

    return {
        "server": settings.app_name, "version": __version__, "api_version": API_VERSION,
        # Short form only: sha and build time (a client compares it with its own sha).
        "build": buildinfo.short(),
        # setup_required tells a native app to send the person to /setup first.
        "setup_required": setup_svc.setup_required(),
        "auth": {"device_pairing": True, "password_login": _password_login_allowed(), "auth_mode": settings.auth_mode,
                 "verification_uri": f"{settings.public_url}/app#pair", "token_prefix": auth.DEVICE_TOKEN_PREFIX,
                 "poll_interval": auth.DEVICE_CODE_INTERVAL, "code_lifetime": int(auth.DEVICE_CODE_TTL)},
        "limits": {"page_max": PAGE_MAX, "progress_batch_max": 100, "cover_sizes": [96, 192, 384, 768]},
        "ebook_formats": sorted(reader_svc.READABLE_EBOOK_FORMATS), "audio_extensions": sorted(reader_svc.PLAYABLE_AUDIO_EXTS),
        "features": ["range_requests", "etag", "incremental_sync", "bookmarks", "highlights", "favorites", "requests", "password_login", "converted_assets"],
        # False when the LLM is off or unreachable: the Player then hides the chat and keeps plain search.
        "discover_chat": discover_chat.available(),
    }


# ---------- password login for native apps ----------
def _password_login_allowed() -> bool:
    # Same rule as /api/v1/auth/login: local accounts exist in local and oidc modes
    # (kids accounts have no IdP identity); proxy and none have no password to check.
    return settings.auth_mode in ("local", "oidc")


class PasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)
    device_name: str | None = Field(default=None, max_length=80)
    platform: str | None = Field(default=None, max_length=60)
    client_version: str | None = Field(default=None, max_length=40)


@router.post("/auth/login")
def password_login(req: PasswordLoginRequest, request: Request):
    """Sign a native app in with a local username and password and hand it a device
    token (the same credential pairing issues), so a phone with a keyboard need not
    walk through the device-code flow. Throttled like the browser login; refused
    outside local and oidc modes, where pairing remains the way in. The token is
    shown once: keep it in the platform keychain."""
    if not _password_login_allowed():
        raise HTTPException(400, f"Password login is disabled (auth mode: {settings.auth_mode}); pair the device instead")
    auth.throttle_check(request, req.username)
    user = auth.login_local(req.username, req.password)
    if not user:
        auth.throttle_failure(request, req.username)
        raise HTTPException(401, "Invalid username or password")
    auth.throttle_success(request, req.username)
    if auth.is_pending(user):
        raise HTTPException(403, auth.PENDING_DETAIL)
    name = (req.device_name or "").strip() or f"{req.platform or 'app'} device"
    out = auth.create_device(user["id"], name=name, platform=req.platform, client_version=req.client_version)
    from .main import _me_public
    out["user"] = _me_public(user)
    return Response(content=json.dumps(out), media_type="application/json", headers={"Cache-Control": "no-store"})


# ---------- device pairing and management ----------
# Pairing requests are the only unauthenticated write in the API; a small per-IP budget
# keeps a stranger from filling the table. In memory, per process, like login throttling.
_PAIR_BUDGET = 10
_PAIR_WINDOW = 600.0
_pair_hits: dict[str, list[float]] = {}


def _pair_throttle(request: Request) -> None:
    now = auth._clock()
    ip = auth.client_ip(request)
    hits = [t for t in _pair_hits.get(ip, []) if now - t < _PAIR_WINDOW]
    if len(hits) >= _PAIR_BUDGET:
        raise HTTPException(429, "Too many pairing requests; try again later", headers={"Retry-After": "600"})
    hits.append(now)
    _pair_hits[ip] = hits
    if len(_pair_hits) > 5000:
        for k in [k for k, v in _pair_hits.items() if not v or now - v[-1] > _PAIR_WINDOW]:
            _pair_hits.pop(k, None)


class DeviceCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=80)
    platform: str | None = Field(default=None, max_length=60)
    client_version: str | None = Field(default=None, max_length=40)


class DeviceTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_code: str = Field(min_length=8, max_length=200)


class DeviceApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_code: str = Field(min_length=8, max_length=12)
    name: str | None = Field(default=None, max_length=80)


class DeviceRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


@router.post("/device/code", status_code=201)
def device_code(req: DeviceCodeRequest, request: Request):
    """Pairing step 1 (device): get a device_code to poll with and a user_code to show.
    Shaped like RFC 8628 so OAuth device-grant client code ports over, but issued by
    bookdoe for an bookdoe token (no IdP secret ever reaches the device)."""
    _pair_throttle(request)
    out = auth.start_device_code(name=req.name, platform=req.platform, client_version=req.client_version)
    out["verification_uri"] = f"{settings.public_url}/app#pair"
    out["verification_uri_complete"] = f"{settings.public_url}/app#pair/{out['user_code']}"
    return out


@router.post("/device/token")
def device_token(req: DeviceTokenRequest, request: Request):
    """Pairing step 3 (device, polling): the token once a person approved the code.
    Errors follow RFC 8628: 400 with `error` authorization_pending | slow_down |
    expired_token | access_denied. The token is shown once; store it securely."""
    state, payload = auth.redeem_device_code(req.device_code)
    if state != "ok":
        return Response(content=json.dumps({"error": state, "detail": {
            "authorization_pending": "Not approved yet; keep polling at the given interval",
            "slow_down": "Polling too fast; wait for the interval",
            "expired_token": "This code expired or was already used; ask for a new one",
            "access_denied": "The person denied this device (or the account is not active)",
        }[state]}), status_code=400 if state != "access_denied" else 403, media_type="application/json", headers={"Cache-Control": "no-store"})
    me_row = db.one("SELECT * FROM users WHERE id=?", (payload["user_id"],))
    from .main import _me_public
    payload["user"] = _me_public(me_row)
    return payload


@router.post("/device/approve")
def device_approve(req: DeviceApproveRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Pairing step 2 (person, signed in to the Player): approve the code on the device.
    The new device inherits exactly this account's rights and nothing more."""
    return auth.approve_device_code(user, req.user_code, name=req.name)


@router.post("/device/deny")
def device_deny(req: DeviceApproveRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    if not auth.deny_device_code(user, req.user_code):
        raise HTTPException(404, "That code is not waiting to be paired")
    return {"user_code": auth.normalize_user_code(req.user_code), "denied": True}


@router.get("/devices")
def my_devices(request: Request, user=Depends(auth.require_user)):
    """This account's paired devices (token metadata only)."""
    return {"items": auth.list_devices(user["id"]),
            "current_device_id": user.get("_device_id") or auth.device_id_for_cookie(request, user["id"]),
            "alerts": [{"id": r["id"], "name": r["name"], "detected_at": r["reuse_detected_at"]} for r in db.all(
                "SELECT id,name,reuse_detected_at FROM devices WHERE user_id=? AND reuse_detected_at>? ORDER BY reuse_detected_at DESC",
                (user["id"], db.now() - 30 * 86400))],
            "trust": {"allowed": signin.trust_allowed(auth.account_type(user)) and settings.auth_mode in ("local", "oidc"),
                      "max": signin.trusted_devices_max()}}


@router.patch("/devices/{device_id}")
def rename_device(device_id: str, req: DeviceRenameRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    try:
        ok = auth.rename_device(device_id, user["id"], req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "Device not found")
    return {"id": device_id, "name": req.name.strip()}


@router.delete("/devices/{device_id}")
def revoke_my_device(device_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Revoke one of your own devices (a lost reader). Administrators revoke anyone's
    through /api/v1/devices. A device may revoke itself (sign out)."""
    if not auth.revoke_device(device_id, user_id=user["id"], actor=user["id"]):
        raise HTTPException(404, "Device not found or already revoked")
    return {"ok": True}


@router.post("/devices/rotate")
def rotate_my_token(request: Request, user=Depends(auth.require_user)):
    """Device only: a fresh token for this device; the old one stops working at once.
    A trusted browser's token is never returned (it lives in an HttpOnly cookie and
    rotates by itself), so it cannot use this."""
    if auth.is_cookie_device(user):
        raise HTTPException(403, "A trusted browser's token rotates automatically and is never shown")
    return auth.rotate_device(user)


# ---------- trusted browsers ("Trust this device") ----------
_TRUST_BUDGET = 5
_TRUST_WINDOW = 600.0
_trust_hits: dict[str, list[float]] = {}


def _trust_throttle(request: Request, user: dict[str, Any]) -> None:
    """5 trust requests per 10 minutes per account and per address (in memory, like pairing)."""
    now = auth._clock()
    for key in ("u:" + user["id"], "ip:" + auth.client_ip(request)):
        hits = [t for t in _trust_hits.get(key, []) if now - t < _TRUST_WINDOW]
        if len(hits) >= _TRUST_BUDGET:
            raise HTTPException(429, "Too many attempts to trust a device; try again later", headers={"Retry-After": str(int(_TRUST_WINDOW - (now - hits[0])) + 1)})
        hits.append(now)
        _trust_hits[key] = hits
    if len(_trust_hits) > 5000:
        for k in [k for k, v in _trust_hits.items() if not v or now - v[-1] > _TRUST_WINDOW]:
            _trust_hits.pop(k, None)


class TrustDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=80)


@router.post("/devices/trust", status_code=201)
def trust_this_device(req: TrustDeviceRequest, request: Request, response: Response, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Keep this browser signed in: registers it as a trusted device and sets the
    HttpOnly `bookdoe_device` cookie. No token is returned in the body. Needs a normal
    signed-in session with CSRF; a device credential (Bearer or cookie) and an API key
    cannot mint more trust. A trusted browser reaches the Player only, administrators
    included: Management always needs the normal sign-in."""
    if user.get("_auth_method") != "session":
        raise HTTPException(403, "Trust a device from a normal signed-in session, not from another credential")
    if settings.auth_mode not in ("local", "oidc"):
        raise HTTPException(400, f"Trusted devices are not available in {settings.auth_mode} mode")
    kind = auth.account_type(user)
    if not signin.trust_allowed(kind):
        raise HTTPException(403, "Trusting a device is not enabled for this kind of account")
    _trust_throttle(request, user)
    # Trusting again from a browser that is already trusted replaces its registration.
    # (Whoever owned the old registration: it is this browser, and an orphan would count against them.)
    replaced = auth.device_for_cookie(request)
    if replaced:
        auth.revoke_device(replaced["id"], user_id=None, actor=user["id"])
    summary = auth.ua_summary(request.headers.get("user-agent"))
    name = (req.name or "").strip() or summary
    try:
        made = auth.create_device(user["id"], name=name, platform="browser", kind="browser", ua_summary=summary,
                                  ip_prefix=auth.ip_prefix(auth.client_ip(request)), max_browser=signin.trusted_devices_max())
    except auth.DeviceLimitError as exc:
        raise HTTPException(409, f"You already have {exc.active} trusted devices (the limit is {signin.trusted_devices_max()}). Revoke one in Settings first.")
    auth.set_device_cookie(response, made["token"])
    return {"ok": True, "device": {"id": made["device_id"], "name": made["name"], "ua_summary": summary, "current": True}}


# ---------- library (compact) ----------
def _client_formats(request: Request, formats: str | None) -> frozenset[str]:
    """The formats a client declares it can play/read, from ``?formats=m4b,epub`` or the
    ``X-bookdoe-Formats`` header. Empty (the default) keeps the pre-conversion behaviour:
    converted copies are not listed at all. Only the preferred formats are ever offered."""
    raw = formats if formats is not None else request.headers.get("x-bookdoe-formats", "")
    wanted = {f.strip().lower() for f in raw.split(",") if f.strip()}
    return frozenset(wanted & set(derived_svc.PREFERRED_FORMAT.values()))


def _compact(w: dict[str, Any], user: dict[str, Any], accept: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The work summary a constrained client needs: no files, no paths, no acquisition
    state, no monitoring - availability as owned kinds only.

    Converted copies (derived assets) are invisible unless the client declared support for
    their format (``accept``). Then they are listed with ``derived_from`` and the response
    names the best asset per kind in ``preferred`` (a supported preferred-format copy, else
    the first original); originals always stay in the list."""
    listed = [a for a in w["assets"] if a["status"] == "owned" and (not a.get("derived_from_asset_id") or (a.get("format") or "").lower() in accept)]
    owned = sorted({a["kind"] for a in listed if any(not f["missing"] for f in a.get("files", []))})
    assets = []
    for a in listed:
        entry = {"id": a["id"], "kind": a["kind"], "format": a.get("format"),
                 "readable": a["kind"] == "ebook" and any(not f["missing"] and f.get("format") in reader_svc.READABLE_EBOOK_FORMATS for f in a.get("files", []))
                 or a["kind"] == "audiobook" and any(not f["missing"] and ("." + (f.get("format") or "")) in reader_svc.PLAYABLE_AUDIO_EXTS for f in a.get("files", [])),
                 "duration_seconds": a.get("duration_seconds") or a.get("total_duration_seconds"),
                 # Additive: every format present (best first), the one a reader opens first, and a
                 # count/size for a "12 files · 8h 12m" line. Never a path or a per-file list.
                 "formats": a.get("formats") or [], "primary_format": a.get("primary_format"),
                 "file_count": a.get("file_count") or 0, "size_bytes": a.get("total_size_bytes") or 0}
        if a.get("derived_from_asset_id"):
            entry["derived_from"] = a["derived_from_asset_id"]
        assets.append(entry)
    out = {
        "id": w["id"], "title": w["title"], "subtitle": w.get("subtitle"), "sort_title": w.get("sort_title"),
        "authors": [{"id": a["id"], "name": a["name"]} for a in w["authors"]],
        "narrators": [{"id": n["id"], "name": n["name"]} for n in w.get("narrators", [])],
        "series": w["series"], "year": (w.get("first_publish_date") or "")[:4] or None, "language": w.get("primary_language"),
        "has_cover": w["has_cover"], "cover_url": f"/api/player/v1/library/{w['id']}/cover" if w["has_cover"] else None,
        "formats": owned, "assets": assets, "rating": w.get("rating"), "read_status": w.get("read_status"),
    }
    if accept:
        best: dict[str, str] = {}
        for kind in owned:
            usable = [a for a in assets if a["kind"] == kind and a["readable"]]
            pick = next((a for a in usable if a.get("derived_from")), None) or (usable[0] if usable else None)
            if pick:
                best[kind] = pick["id"]
        out["preferred"] = best
    return out


@router.get("/library")
def library(request: Request, response: Response, q: str | None = None, kind: Literal["ebook", "audiobook"] | None = None,
            author_id: str | None = None, series_id: str | None = None, updated_since: float | None = Query(None, ge=0),
            formats: str | None = Query(None, max_length=60),
            sort: Literal["title", "added", "updated"] = "title",
            limit: int = Query(50, ge=1, le=PAGE_MAX), offset: int = Query(0, ge=0), user=Depends(auth.require_user)):
    """Books this account may read or listen to (owned, with at least one present file).
    `updated_since` (server seconds) returns only works changed since a previous sync so a
    device never re-downloads the whole catalogue; pair it with `/sync`."""
    from .main import _batch_library_summary

    visible, vparams = tagging.kids_visibility(user)
    where = [visible, "EXISTS (SELECT 1 FROM media_assets a JOIN media_files f ON f.asset_id=a.id WHERE a.work_id=w.id AND a.status='owned' AND f.missing=0)"]
    params: list[Any] = list(vparams)
    if q:
        where.append("(w.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=w.id AND c.name LIKE ? ESCAPE '\\'))")
        params += [db.like_pattern(q), db.like_pattern(q)]
    if kind:
        where.append("EXISTS (SELECT 1 FROM media_assets ax JOIN media_files fx ON fx.asset_id=ax.id WHERE ax.work_id=w.id AND ax.kind=? AND ax.status='owned' AND fx.missing=0)")
        params.append(kind)
    if author_id:
        where.append("EXISTS (SELECT 1 FROM work_contributors wc WHERE wc.work_id=w.id AND wc.contributor_id=?)"); params.append(author_id)
    if series_id:
        where.append("EXISTS (SELECT 1 FROM series_memberships sm WHERE sm.work_id=w.id AND sm.series_id=?)"); params.append(series_id)
    if updated_since is not None:
        where.append("w.updated_at>?"); params.append(updated_since)
    order = {"title": "COALESCE(w.sort_title,w.title)", "added": "w.added_at DESC", "updated": "w.updated_at DESC"}[sort]
    clause = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM works w WHERE {clause}", tuple(params))["n"]  # nosec B608
    works = db.all(f"SELECT w.* FROM works w WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?", tuple(params + [limit, offset]))  # nosec B608
    accept = _client_formats(request, formats)
    items = [_compact(w, user, accept) for w in _batch_library_summary(works, user)]
    for item, w in zip(items, works):
        item["updated_at"] = w["updated_at"]
    payload = {"items": items, "total": total, "limit": limit, "offset": offset, "server_time": db.now()}
    tag = _etag({k: v for k, v in payload.items() if k != "server_time"})
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "private, no-cache"
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return payload


@router.get("/library/{work_id}")
def library_work(work_id: str, request: Request, response: Response, formats: str | None = Query(None, max_length=60), user=Depends(auth.require_user)):
    from .main import _batch_library_summary

    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w or not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    item = _compact(_batch_library_summary([w], user)[0], user, _client_formats(request, formats))
    item["description"] = w.get("description")
    item["updated_at"] = w["updated_at"]
    item["progress"] = reader_svc.progress_for_work(work_id, user["id"])
    item["favorite"] = db.one("SELECT 1 FROM favorites WHERE user_id=? AND work_id=?", (user["id"], work_id)) is not None
    return _conditional(request, response, item)


@router.get("/library/{work_id}/cover")
def library_cover(work_id: str, size: int | None = Query(None, ge=32, le=1024), user=Depends(auth.require_user)):
    """Cover art, optionally scaled (longest side `size`, via ffmpeg when present; the
    original is served when scaling is unavailable). Private, cacheable for a day."""
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "No cover available")
    hit = covers.cached(work_id)
    if not hit:
        raise HTTPException(404, "No cover available")
    path = hit["path"]
    if size:
        path = covers.thumbnail(work_id, hit["path"], size) or hit["path"]
    from fastapi.responses import FileResponse
    return FileResponse(path, media_type=hit.get("content_type") or "image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/authors")
def authors(request: Request, response: Response, q: str | None = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), user=Depends(auth.require_user)):
    visible, vparams = tagging.kids_visibility(user)
    where, params = ["1=1"], list(vparams)  # the predicate sits in the JOIN, before the WHERE's `?`
    if q:
        where.append("c.name LIKE ? ESCAPE '\\'"); params.append(db.like_pattern(q))
    base = f"""SELECT c.id,c.name,COUNT(DISTINCT w.id) book_count FROM contributors c
               JOIN work_contributors wc ON wc.contributor_id=c.id AND wc.role='author'
               JOIN works w ON w.id=wc.work_id AND {visible}
               AND EXISTS (SELECT 1 FROM media_assets a JOIN media_files f ON f.asset_id=a.id WHERE a.work_id=w.id AND a.status='owned' AND f.missing=0)
               WHERE {' AND '.join(where)} GROUP BY c.id"""  # nosec B608
    total = db.one(f"SELECT COUNT(*) n FROM ({base})", tuple(params))["n"]  # nosec B608
    rows = db.all(f"{base} ORDER BY COALESCE(c.sort_name,c.name) COLLATE NOCASE LIMIT ? OFFSET ?", tuple(params + [limit, offset]))
    return _conditional(request, response, {"items": rows, "total": total, "limit": limit, "offset": offset})


@router.get("/series")
def series(request: Request, response: Response, q: str | None = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), user=Depends(auth.require_user)):
    from .services import series as series_service

    page = series_service.list_series_page(user, q=q, limit=limit, offset=offset)
    items = [{"id": s["id"], "name": s["name"], "book_count": s["work_count"], "available_count": s["owned_count"]} for s in page["items"] if s["owned_count"]]
    return _conditional(request, response, {"items": items, "total": page["total"], "limit": limit, "offset": offset})


# ---------- delivery manifests ----------
def _manifest(work_id: str, asset_id: str, user: dict[str, Any], kind: str) -> dict[str, Any]:
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    try:
        m = reader_svc.manifest(work_id, asset_id, user)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 409, str(exc))
    if m["kind"] != kind:
        raise HTTPException(404, f"Asset is not an {kind}")
    m["cover_url"] = f"/api/player/v1/library/{work_id}/cover" if covers.has_cached(work_id) else None
    m["authors"] = ", ".join(a["name"] for a in db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (work_id,)))
    return m


@router.get("/ebook/{work_id}/{asset_id}")
def ebook_manifest(work_id: str, asset_id: str, request: Request, response: Response, user=Depends(auth.require_user)):
    """The ebook file to download (`files[0].url`, Range-capable, same credential) with
    size, format and this account's position. Only EPUB and PDF are listed."""
    m = _manifest(work_id, asset_id, user, "ebook")
    m["download_url"] = m["files"][0]["url"]
    return _conditional(request, response, m)


@router.get("/audio/{work_id}/{asset_id}")
def audio_manifest(work_id: str, asset_id: str, request: Request, response: Response, user=Depends(auth.require_user)):
    """Tracks in order (each a Range-capable stream URL), chapters when embedded, total
    duration and this account's position. Downloading for offline use is permitted for
    every track listed (`download_eligible`)."""
    m = _manifest(work_id, asset_id, user, "audiobook")
    m["duration_seconds"] = sum(int(f.get("duration_seconds") or 0) for f in m["files"]) or None
    m["download_eligible"] = True
    return _conditional(request, response, m)


# ---------- progress and sync ----------
class ProgressItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    file_id: str | None = None
    position: float | None = Field(default=None, ge=0)
    location: str | None = Field(default=None, max_length=2000)
    percent: float | None = Field(default=None, ge=0, le=1)
    duration: float | None = Field(default=None, ge=0)
    device_id: str | None = Field(default=None, max_length=120)
    client_updated_at: float | None = Field(default=None, ge=0)


class ProgressBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ProgressItem] = Field(max_length=100)


def _progress_rows(user: dict[str, Any], since: float | None) -> list[dict[str, Any]]:
    visible, vparams = tagging.kids_visibility(user)
    where, params = [visible, "p.user_id=?"], [*vparams, user["id"]]
    if since is not None:
        where.append("p.updated_at>?"); params.append(since)
    return db.all(
        f"SELECT p.asset_id,p.work_id,p.kind,{','.join('p.' + c for c in reader_svc.PROGRESS_COLUMNS.split(','))} FROM media_progress p JOIN works w ON w.id=p.work_id WHERE {' AND '.join(where)} ORDER BY p.updated_at",  # nosec B608
        tuple(params))


@router.get("/progress")
def all_progress(request: Request, response: Response, since: float | None = Query(None, ge=0), user=Depends(auth.require_user)):
    """Every position this account holds (or those changed since `since`)."""
    return _conditional(request, response, {"items": _progress_rows(user, since), "server_time": db.now()})


@router.put("/progress")
def put_progress_batch(req: ProgressBatch, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Apply a batch of position updates (offline reconciliation). Each item is judged
    on its own by the conflict rule; the answer says which were applied and returns the
    stored state for the rest, so the device can adopt it. Unknown or invisible assets
    are reported (``error: not_found``), not fatal: the device keeps those positions and
    asks again later. Each item counts against the same per-book write budget as the
    single-item route; items past it come back ``error: rate_limited`` with ``retry_after``."""
    out = []
    for item in req.items:
        wait = reader_svc.progress_write_retry_after(user["id"], item.asset_id)
        if wait:
            out.append({"asset_id": item.asset_id, "applied": False, "error": "rate_limited", "retry_after": wait}); continue
        asset = db.one("SELECT id,work_id FROM media_assets WHERE id=?", (item.asset_id,))
        if not asset or not tagging.is_visible_to(asset["work_id"], user):
            out.append({"asset_id": item.asset_id, "applied": False, "error": "not_found"}); continue
        try:
            res = reader_svc.set_progress(asset["work_id"], item.asset_id, user["id"], file_id=item.file_id, position=item.position, location=item.location,
                                          percent=item.percent, device_id=item.device_id or user.get("_device_id"), client_updated_at=item.client_updated_at, duration=item.duration)
            res["work_id"] = asset["work_id"]
            out.append(res)
        except ValueError as exc:
            out.append({"asset_id": item.asset_id, "applied": False, "error": str(exc)})
    return {"items": out, "server_time": db.now()}


@router.get("/progress/{asset_id}")
def one_progress(asset_id: str, user=Depends(auth.require_user)):
    asset = db.one("SELECT id,work_id FROM media_assets WHERE id=?", (asset_id,))
    if not asset or not tagging.is_visible_to(asset["work_id"], user):
        raise HTTPException(404, "Asset not found")
    return {"asset_id": asset_id, "work_id": asset["work_id"], "progress": reader_svc.get_progress(asset_id, user["id"])}


@router.delete("/progress/{asset_id}")
def clear_one_progress(asset_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Forget this account's position in the asset ("mark as unread"): it leaves
    /continue and the next open starts at the beginning. Only this account's row goes;
    the same rules as clearing it from the web (an automatic 'reading' state is removed
    with it, a hand-set status is kept). Idempotent: clearing nothing answers the same."""
    asset = db.one("SELECT id,work_id FROM media_assets WHERE id=?", (asset_id,))
    if not asset or not tagging.is_visible_to(asset["work_id"], user):
        raise HTTPException(404, "Asset not found")
    reader_svc.clear_progress(asset_id, user["id"])
    return {"asset_id": asset_id, "work_id": asset["work_id"], "progress": None}


@router.put("/progress/{asset_id}")
def put_one_progress(asset_id: str, item: ProgressItem, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    if item.asset_id != asset_id:
        raise HTTPException(400, "asset_id in the body must match the path")
    # Same answers as the legacy per-work route: 429 past the budget, one cheap 404 for
    # a book that is missing or off this account's shelf.
    res = put_progress_batch(ProgressBatch(items=[item]), request, None, user)["items"][0]
    if res.get("error") == "rate_limited":
        raise HTTPException(429, "Saving too often; try again shortly", headers={"Retry-After": str(res["retry_after"])})
    if res.get("error") == "not_found":
        raise HTTPException(404, "Not found", headers={"Cache-Control": "no-store"})
    return res


@router.get("/sync")
def sync(since: float | None = Query(None, ge=0), user=Depends(auth.require_user)):
    """Incremental sync in one call: server time to store as the next `since`, positions
    changed since, favorites and bookmarks (small, sent whole), and the ids of visible
    works added or changed since so the device fetches only those from /library."""
    visible, vparams = tagging.kids_visibility(user)
    changed = db.all(f"SELECT w.id,w.updated_at FROM works w WHERE {visible} AND (? IS NULL OR w.updated_at>?) "  # nosec B608
                     "AND EXISTS (SELECT 1 FROM media_assets a JOIN media_files f ON f.asset_id=a.id WHERE a.work_id=w.id AND a.status='owned' AND f.missing=0) "
                     "ORDER BY w.updated_at LIMIT 1000", (*vparams, since, since))
    return {
        "server_time": db.now(), "since": since,
        "progress": _progress_rows(user, since),
        "favorites": [r["work_id"] for r in db.all(f"SELECT f.work_id FROM favorites f JOIN works w ON w.id=f.work_id WHERE f.user_id=? AND {visible}", (user["id"], *vparams))],  # nosec B608
        "bookmarks": [_bookmark_public(r) for r in db.all(f"SELECT b.* FROM bookmarks b JOIN works w ON w.id=b.work_id WHERE b.user_id=? AND {visible} AND (? IS NULL OR COALESCE(b.updated_at,b.created_at)>?)", (user["id"], *vparams, since, since))],  # nosec B608
        "library_changed": [r["id"] for r in changed], "library_changed_truncated": len(changed) >= 1000,
    }


# ---------- Send to Kindle ----------
# Delivery of a book already on the person's shelf to their own Kindle. Session or API
# key only: a device credential is a reading client and has no business sending mail,
# so the device lane is refused here explicitly (it never reaches /api/v1 anyway).
from .services import devices as devices_svc  # noqa: E402

KINDLE_SEND_BUDGET = 10          # sends per user ...
KINDLE_SEND_WINDOW = 600.0       # ... per 10 minutes
_kindle_sends: dict[str, list[float]] = {}
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def kindle_rate_limit(user: dict[str, Any]) -> None:
    now = auth._clock()
    hits = [t for t in _kindle_sends.get(user["id"], []) if now - t < KINDLE_SEND_WINDOW]
    if len(hits) >= KINDLE_SEND_BUDGET:
        raise HTTPException(429, f"Too many Kindle sends; try again in a few minutes (limit {KINDLE_SEND_BUDGET} per {int(KINDLE_SEND_WINDOW // 60)} minutes)",
                            headers={"Retry-After": str(int(KINDLE_SEND_WINDOW - (now - hits[0])) + 1)})
    hits.append(now)
    _kindle_sends[user["id"]] = hits


def set_own_kindle_email(user: dict[str, Any], value: str | None) -> str | None:
    """Validate and store the signed-in person's own Kindle address; audited."""
    cleaned = (value or "").strip() or None
    if cleaned and (len(cleaned) > 254 or not _EMAIL_RE.match(cleaned)):
        raise HTTPException(400, "That does not look like an email address")
    with db.tx() as conn:
        conn.execute("UPDATE users SET kindle_email=?,updated_at=? WHERE id=?", (cleaned, db.now(), user["id"]))
    db.add_history("account.kindle_email", f"'{user['username']}' {'set' if cleaned else 'cleared'} their Kindle email", data={"user_id": user["id"]})
    return cleaned


def _no_device(user: dict[str, Any]) -> None:
    if user.get("_auth_method") == "device" and not auth.is_cookie_device(user):
        raise HTTPException(403, "Sending to a Kindle is not available to a device credential")


class KindleEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kindle_email: str | None = Field(default=None, max_length=254)


class KindleSendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str | None = None   # omitted: the best sendable file is chosen


@router.get("/kindle")
def kindle_status(user=Depends(auth.require_user)):
    """Whether Send-to-Kindle is set up on this server, the sender address the Kindle
    must approve (nothing else about the mail settings), this account's Kindle
    address and the accepted file formats."""
    _no_device(user)
    cfg = devices_svc.get_smtp_config()
    fresh = db.one("SELECT kindle_email FROM users WHERE id=?", (user["id"],)) or {}
    return {"configured": bool(cfg["configured"]), "sender_address": cfg["from_email"] if cfg["configured"] else None,
            "kindle_email": fresh.get("kindle_email"), "formats": sorted(x.lstrip(".") for x in devices_svc.KINDLE_SUPPORTED_EXTS),
            "limit": {"sends": KINDLE_SEND_BUDGET, "window_seconds": int(KINDLE_SEND_WINDOW)}}


@router.put("/kindle")
def kindle_email(req: KindleEmailRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    if user.get("_auth_method") == "device":
        # Re-pointing where books are mailed is an account change: a real session only.
        raise HTTPException(403, "Changing the Kindle address needs a normal signed-in session, not a trusted device")
    _no_device(user)
    return {"kindle_email": set_own_kindle_email(user, req.kindle_email)}


@router.get("/kindle/{work_id}")
def kindle_options(work_id: str, user=Depends(auth.require_user)):
    """The files of a visible work that can be sent, best first (EPUB preferred)."""
    _no_device(user)
    _visible_or_404(work_id, user)
    return {"work_id": work_id, "items": devices_svc.sendable_assets(work_id)}


@router.post("/kindle/send/{work_id}")
def kindle_send(work_id: str, req: KindleSendBody, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Send one file of a visible work to this account's Kindle address. Picks the best
    sendable file unless `asset_id` names one. 400 with a plain message when nothing is
    sendable, no address is on file or Send-to-Kindle is not configured; 429 past the
    per-account limit; 502 when the mail server refused."""
    _no_device(user)
    _visible_or_404(work_id, user)
    if not devices_svc.get_smtp_config()["configured"]:
        raise HTTPException(400, "Send-to-Kindle is not set up on this server yet")
    to_email = (db.one("SELECT kindle_email FROM users WHERE id=?", (user["id"],)) or {}).get("kindle_email")
    if not to_email:
        raise HTTPException(400, "Add your Kindle email in Settings first")
    options = devices_svc.sendable_assets(work_id)
    if not options:
        raise HTTPException(400, "This book has no file a Kindle accepts")
    chosen = next((o for o in options if o["asset_id"] == req.asset_id), None) if req.asset_id else options[0]
    if not chosen:
        raise HTTPException(400, "That file cannot be sent to a Kindle")
    kindle_rate_limit(user)
    try:
        out = devices_svc.send_to_kindle(work_id, chosen["asset_id"], to_email, by_user_id=user["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except devices_svc.MailError as exc:
        # 424 (failed dependency) with the plain reason: a 5xx would be replaced by
        # Cloudflare's own error page and the person would learn nothing.
        raise HTTPException(424, exc.message)
    return {**out, "asset_id": chosen["asset_id"], "format": chosen["format"]}
