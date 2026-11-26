from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from .. import db
from ..integrations import downloaders
from . import importer
from . import acquisition

log = logging.getLogger(__name__)


def _compatible_clients(protocol: str) -> list[dict[str, Any]]:
    kinds = {"usenet": {"sabnzbd", "nzbget"}, "torrent": {"qbittorrent", "transmission"}}.get(protocol, set())
    return [c for c in db.all("SELECT * FROM download_clients WHERE enabled=1 ORDER BY priority DESC,name") if c["kind"] in kinds]


def grab_release(release_id: str, client_id: str | None = None, grabbed_by: str | None = None) -> dict[str, Any]:
    acquisition.ensure_enabled()
    rel = db.one("SELECT * FROM releases WHERE id=?", (release_id,))
    if not rel:
        raise ValueError("Release not found")
    if rel["rejected"]:
        raise ValueError("Release is rejected by current policy; use a manual override endpoint if you intend to force it")
    candidates = _compatible_clients(rel["protocol"])
    if client_id:
        candidates = [x for x in candidates if x["id"] == client_id]
    if not candidates:
        raise ValueError(f"No enabled {rel['protocol']} download client is configured")
    client = candidates[0]
    # Track first, then add: a crash between the client add and our bookkeeping must not leave a
    # client job nobody knows about. The row starts as 'grabbed' (no client id yet; the poller can
    # still match it by name) and counts as in flight for the wanted search at once.
    asset = db.one("SELECT * FROM media_assets WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL ORDER BY format IS NULL DESC,added_at LIMIT 1", (rel["work_id"], rel["asset_kind"]))
    download_id = db.uid()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO downloads(id,work_id,asset_id,release_id,client_id,client_download_id,state,protocol,source_title,progress,added_at,updated_at,size_bytes,grabbed_by)
               VALUES (?,?,?,?,?,NULL,'grabbed',?,?,0,?,?,?,?)""",
            (download_id, rel["work_id"], asset["id"] if asset else None, release_id, client["id"], rel["protocol"], rel["raw_title"], db.now(), db.now(), rel.get("size_bytes"), grabbed_by),
        )
    try:
        result = downloaders.add(client, rel)
        if not result.get("ok"):
            raise ValueError("Download client did not accept the release")
    except BaseException:
        with db.tx() as conn:
            conn.execute("DELETE FROM downloads WHERE id=? AND state='grabbed' AND client_download_id IS NULL", (download_id,))
        raise
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET client_download_id=?,state='downloading',updated_at=? WHERE id=?", (result.get("id"), db.now(), download_id))
        # Lifecycle: a wanted slot with a download in flight is no longer "wanted", so the
        # Wanted list and the automatic search stop picking it. An owned slot (a redundant
        # extra download) keeps its status.
        if asset:
            conn.execute("UPDATE media_assets SET status='downloading',updated_at=? WHERE id=? AND status IN ('missing','none')", (db.now(), asset["id"]))
    db.add_history("release.grabbed", f"Grabbed {rel['raw_title']} via {client['name']}", work_id=rel["work_id"], asset_id=asset["id"] if asset else None, data={"release_id": release_id, "client_id": client["id"]})
    return {"download_id": download_id, "client": client["name"], "client_result": result}


def force_grab_release(release_id: str, client_id: str | None = None, grabbed_by: str | None = None) -> dict[str, Any]:
    acquisition.ensure_enabled()
    rel = db.one("SELECT * FROM releases WHERE id=?", (release_id,))
    if not rel:
        raise ValueError("Release not found")
    with db.tx() as conn:
        conn.execute("UPDATE releases SET rejected=0 WHERE id=?", (release_id,))
    try:
        result = grab_release(release_id, client_id, grabbed_by)
        db.add_history("release.force_grabbed", f"Force-grabbed {rel['raw_title']}", work_id=rel["work_id"], data={"release_id": release_id})
        return result
    finally:
        with db.tx() as conn:
            conn.execute("UPDATE releases SET rejected=? WHERE id=?", (rel["rejected"], release_id))


def poll_all() -> dict[str, Any]:
    acquisition.ensure_enabled()
    try:
        recover_stale_imports()
    except Exception:
        log.exception("recovering stale imports failed")
    clients = db.all("SELECT * FROM download_clients WHERE enabled=1 ORDER BY priority DESC")
    updated, completed, imported, errors = 0, 0, 0, []
    for client in clients:
        try:
            items = downloaders.poll(client)
            by_id = {str(x.get("id")): x for x in items if x.get("id")}
            active = db.all("SELECT * FROM downloads WHERE client_id=? AND state IN ('grabbed','downloading','completed','import_pending')", (client["id"],))
            for d in active:
                hit = by_id.get(str(d.get("client_download_id")))
                # qBit URL adds may not initially give us a hash. Correlate conservatively by exact source title/name.
                rebind = False
                # Older qBittorrent rows stored the indexer guid as the id; treat an id the
                # client does not know as "no id" for that kind so the name match can fix it.
                lost_id = bool(d.get("client_download_id")) and client["kind"] == "qbittorrent"
                if not hit and (not d.get("client_download_id") or lost_id):
                    exact = [x for x in items if (x.get("name") or "").casefold() == (d.get("source_title") or "").casefold()]
                    hit = exact[0] if len(exact) == 1 else None
                    rebind = bool(hit and lost_id)
                if not hit:
                    continue
                if rebind and hit.get("id"):
                    with db.tx() as conn:
                        conn.execute("UPDATE downloads SET client_download_id=? WHERE id=?", (str(hit["id"]), d["id"]))
                state = hit.get("state") or "downloading"
                path = hit.get("path")
                client_id = hit.get("id")
                with db.tx() as conn:
                    # The state guard keeps a poll that raced an import (manual or from the
                    # scheduler) from overwriting 'importing'/'imported' with the client's view.
                    changed = conn.execute(
                        "UPDATE downloads SET client_download_id=COALESCE(client_download_id,?),state=?,progress=?,download_path=COALESCE(?,download_path),updated_at=?,completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,?) ELSE completed_at END,size_bytes=COALESCE(?,size_bytes),downloaded_bytes=COALESCE(?,downloaded_bytes),speed_bps=?,eta_seconds=? WHERE id=? AND state NOT IN ('importing','imported','cancelled')",
                        (str(client_id) if client_id else None, state, float(hit.get("progress") or 0), path, db.now(), state, db.now(),
                         hit.get("size_bytes"), hit.get("downloaded_bytes"), hit.get("speed_bps") if state != "completed" else None, hit.get("eta_seconds") if state != "completed" else None, d["id"]),
                    ).rowcount
                if not changed:
                    continue
                updated += 1
                if state == "failed" and d["state"] != "failed":
                    fail_download(d, "Download failed in the client")
                    continue
                if state == "completed" and d["state"] != "completed":
                    completed += 1
                    db.add_history("download.completed", f"Download completed: {d.get('source_title') or d['id']}", work_id=d.get("work_id"), asset_id=d.get("asset_id"), data={"download_id": d["id"], "path": path})
                    if importer.automatic_import_enabled():
                        imported += _auto_import(d["id"], d.get("source_title") or d["id"])
        except Exception as exc:
            errors.append({"client": client["name"], "error": str(exc)})
            log.warning("download poll failed for %s: %s", client["name"], exc)
    return {"updated": updated, "completed": completed, "imported": imported, "errors": errors}


def _auto_import(download_id: str, label: str) -> int:
    """Import a freshly completed download, without letting a failure break the poll.

    A failure is deliberately not raised: the download stays in the queue as
    import_failed with its error, exactly where a manual import would have left it,
    and the rest of the poll carries on. The common causes - no managed root for that
    media kind, a release whose size does not match what the indexer advertised - are
    ones an operator needs to see and decide about, not ones that should stop the
    other downloads being tracked.
    """
    try:
        import_completed(download_id)
        return 1
    except ImportBusy:
        return 0  # someone else is importing it; not a failure
    except Exception as exc:
        log.warning("automatic import failed for %s: %s", label, exc)
        db.add_history("download.import_failed", f"Automatic import failed for {label}: {exc}", data={"download_id": download_id})
        return 0


_importing_lock = threading.Lock()
_importing_now: set[str] = set()   # downloads this process is importing right now
STALE_IMPORT_SECONDS = 300


def recover_stale_imports(max_age: float = STALE_IMPORT_SECONDS) -> int:
    """Put 'importing' rows that no import in this process owns back to 'import_pending'
    and retry them. A finalising write that lost the database lock (or a restart) leaves
    a row 'importing' for good; the import is idempotent, so running it again is safe."""
    cutoff = db.now() - max_age
    with _importing_lock:
        live = set(_importing_now)
    ids = [r["id"] for r in db.all("SELECT id FROM downloads WHERE state='importing' AND updated_at<?", (cutoff,)) if r["id"] not in live]
    recovered = []
    for did in ids:
        with db.tx() as conn:
            if conn.execute("UPDATE downloads SET state='import_pending',updated_at=? WHERE id=? AND state='importing'", (db.now(), did)).rowcount:
                recovered.append(did)
    if recovered:
        log.warning("recovered %d stale importing download(s)", len(recovered))
    if recovered and importer.automatic_import_enabled():
        for did in recovered:
            _auto_import(did, did)
    return len(recovered)


class ImportBusy(ValueError):
    """Another import already claimed this download."""


def import_completed(download_id: str, mode: str | None = None) -> dict[str, Any]:
    acquisition.ensure_enabled()
    d = db.one("SELECT * FROM downloads WHERE id=?", (download_id,))
    if not d:
        raise ValueError("Download not found")
    if d["state"] == "importing":
        raise ImportBusy("Download is already being imported")
    if d["state"] not in {"completed", "import_pending", "import_failed"}:
        raise ValueError(f"Download is not ready for import (state={d['state']})")
    # Atomic claim: the scheduler's poll and a manual import can both arrive here. Only
    # one wins; the loser must neither import nor record a failure for the winner's work.
    with db.tx() as conn:
        claimed = conn.execute(
            "UPDATE downloads SET state='importing',updated_at=? WHERE id=? AND state IN ('completed','import_pending','import_failed')",
            (db.now(), download_id),
        ).rowcount
    if not claimed:
        raise ImportBusy("Download is already being imported or is no longer ready for import")
    with _importing_lock:
        _importing_now.add(download_id)
    try:
        return importer.import_download(download_id, mode=mode)
    except Exception as exc:
        db.tx_retry(lambda conn: conn.execute("UPDATE downloads SET state='import_failed',error=?,updated_at=? WHERE id=?", (str(exc), db.now(), download_id)))
        db.add_history("download.import_failed", f"Import failed: {exc}", work_id=d.get("work_id"), asset_id=d.get("asset_id"), data={"download_id": download_id})
        blocklist_release(d, f"Import failed: {exc}")
        raise
    finally:
        with _importing_lock:
            _importing_now.discard(download_id)


BLOCKLIST_DAYS = 30


def blocklist_release(d: dict[str, Any], reason: str) -> bool:
    """Blocklist the release behind a download, then free its asset for another try.

    Order matters: the blocklist row is written before the asset goes back to 'missing',
    so the wanted search that may run immediately afterwards cannot re-grab the release
    that just failed. Returns False when there is no release identity to block.
    """
    rel = db.one("SELECT indexer_guid,raw_title FROM releases WHERE id=?", (d["release_id"],)) if d.get("release_id") else None
    guid = rel["indexer_guid"] if rel else None
    title = (rel["raw_title"] if rel else None) or d.get("source_title")
    blocked = False
    if guid or title:
        t = db.now()
        with db.tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM blocklist WHERE COALESCE(work_id,'')=COALESCE(?,'') AND COALESCE(indexer_guid,'')=COALESCE(?,'') AND COALESCE(release_title,'')=COALESCE(?,'') AND (expires_at IS NULL OR expires_at>?)",
                (d.get("work_id"), guid, title, t),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO blocklist(id,work_id,indexer_guid,release_title,reason,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                    (db.uid(), d.get("work_id"), guid, title, reason, t + BLOCKLIST_DAYS * 86400, t),
                )
        blocked = True
        db.add_history("release.blocklisted", f"Blocklisted for {BLOCKLIST_DAYS} days: {title or guid} ({reason})", work_id=d.get("work_id"), asset_id=d.get("asset_id"), data={"download_id": d["id"], "indexer_guid": guid})
    if d.get("asset_id"):
        with db.tx() as conn:
            conn.execute(
                "UPDATE media_assets SET status=CASE WHEN has_acquisition_intent=1 THEN 'missing' ELSE 'none' END,updated_at=? WHERE id=? AND status='downloading'",
                (db.now(), d["asset_id"]),
            )
    return blocked


def fail_download(d: dict[str, Any], reason: str) -> None:
    """A client reported the job itself failed (as opposed to our import failing)."""
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET error=?,updated_at=? WHERE id=?", (reason, db.now(), d["id"]))
    db.add_history("download.failed", f"{reason}: {d.get('source_title') or d['id']}", work_id=d.get("work_id"), asset_id=d.get("asset_id"), data={"download_id": d["id"]})
    blocklist_release(d, reason)


class DownloadNotFound(ValueError):
    """No download row has this id (the route maps it to 404)."""


_ACTIVE_STATES = {"grabbed", "downloading"}
_STUCK_STATES = {"completed", "import_pending", "import_failed", "importing"}


def cancel_download(download_id: str, remove_from_client: bool = True) -> dict[str, Any]:
    """Cancel an active download or dismiss a failed/stuck one; never touches library files.

    Only the download row and the client's own job/data are affected. A client that is
    unreachable or no longer knows the job does not block the cancel: the error is kept
    in the history entry instead, so a dead client cannot pin an item in the queue.
    """
    acquisition.ensure_enabled()
    d = db.one("SELECT * FROM downloads WHERE id=?", (download_id,))
    if not d:
        raise DownloadNotFound("Download not found")
    state = d["state"]
    if state == "imported":
        raise ValueError("Download is already imported; library files are never removed from here")
    if state not in _ACTIVE_STATES | _STUCK_STATES:
        raise ValueError(f"Download cannot be cancelled (state={state})")
    # An active job would keep downloading into the client regardless of the flag.
    removed, client_error = False, None
    if state in _ACTIVE_STATES or remove_from_client:
        client = db.one("SELECT * FROM download_clients WHERE id=?", (d["client_id"],)) if d.get("client_id") else None
        if not client:
            client_error = "Download client no longer exists"
        else:
            try:
                downloaders.remove(client, d.get("client_download_id"), d.get("source_title"))
                removed = True
            except Exception as exc:
                client_error = str(exc) or exc.__class__.__name__
                log.warning("could not remove %s from %s: %s", d["id"], client["name"], client_error)
    with db.tx() as conn:
        # Conditional on the state read above: a poll or import that moved the row since
        # then wins, and the caller is told rather than having its progress overwritten.
        changed = conn.execute("UPDATE downloads SET state='cancelled',updated_at=? WHERE id=? AND state=?", (db.now(), download_id, state)).rowcount
        if not changed:
            raise ValueError("Download changed state while it was being cancelled; try again")
        # Nothing may be left looking in-flight: an unowned slot goes back to wanted
        # if someone still wants it, so it can be searched and grabbed again.
        if d.get("asset_id"):
            conn.execute(
                "UPDATE media_assets SET status=CASE WHEN has_acquisition_intent=1 THEN 'missing' ELSE 'none' END,updated_at=? WHERE id=? AND status!='owned'",
                (db.now(), d["asset_id"]),
            )
    label = d.get("source_title") or download_id
    msg = f"Cancelled download: {label}"
    if client_error:
        msg += f" (client error: {client_error})"
    db.add_history("download.cancelled", msg, work_id=d.get("work_id"), asset_id=d.get("asset_id"), data={"download_id": download_id, "previous_state": state, "removed_from_client": removed, "client_error": client_error})
    return {"id": download_id, "state": "cancelled", "removed_from_client": removed, "client_error": client_error}


# ---------------------------------------------------------------------------
# Queue page payload
# ---------------------------------------------------------------------------

STATE_LABELS = {
    "grabbed": "Sent to client", "downloading": "Downloading", "completed": "Downloaded, awaiting import",
    "import_pending": "Waiting to import", "importing": "Importing", "imported": "Imported",
    "import_failed": "Import failed", "failed": "Failed", "cancelled": "Cancelled",
}
# Filter groups the UI offers; every state belongs to exactly one.
STATE_GROUPS = {
    "queued": {"grabbed"}, "active": {"downloading", "importing"},
    "completed": {"completed", "import_pending", "imported"}, "failed": {"failed", "import_failed", "cancelled"},
}
_HISTORY_EVENTS_PER_ITEM = 12


def state_group(state: str) -> str:
    return next((g for g, members in STATE_GROUPS.items() if state in members), "active")


def _json_obj(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def queue_items(scope: str = "open", history_limit: int = 25) -> dict[str, Any]:
    """Queue rows for the Management Queue page, joined server side.

    ``open`` = everything still in flight or waiting on an operator (the original queue
    contents), ``history`` = the last ``history_limit`` imported/failed/cancelled rows
    (never deleted, just listed), ``all`` = both. Missing joins (deleted work, release,
    indexer, client) give nulls, never an error. Admin-only: callers must hold
    require_full_access; paths and indexer names are part of the payload.
    """
    history_limit = max(1, min(int(history_limit), 200))
    closed = ("imported", "failed", "cancelled")
    open_sql = "d.state NOT IN (?,?,?)"  # same meaning as the old queue: anything not finished
    base = """SELECT d.*, w.title AS title, w.sort_title AS work_sort_title,
        a.kind AS asset_kind, a.format AS asset_format, a.root_folder_id AS asset_root_folder_id,
        r.indexer_id AS rel_indexer_id, r.raw_title AS release_title, r.size_bytes AS release_size_bytes, r.score AS release_score,
        r.parsed_json AS release_parsed_json, r.seeders AS release_seeders, r.asset_kind AS release_asset_kind,
        r.search_id AS release_search_id, i.name AS indexer_name,
        rs.automatic AS search_automatic,
        c.name AS client_name, c.kind AS client_kind
        FROM downloads d
        LEFT JOIN works w ON w.id=d.work_id
        LEFT JOIN media_assets a ON a.id=d.asset_id
        LEFT JOIN releases r ON r.id=d.release_id
        LEFT JOIN indexers i ON i.id=r.indexer_id
        LEFT JOIN release_searches rs ON rs.id=r.search_id
        LEFT JOIN download_clients c ON c.id=d.client_id"""
    rows: list[dict[str, Any]] = []
    if scope in ("open", "all"):
        rows += db.all(f"{base} WHERE {open_sql} ORDER BY d.updated_at DESC", closed)
    if scope in ("history", "all"):
        rows += db.all(f"{base} WHERE d.state IN (?,?,?) ORDER BY d.updated_at DESC LIMIT ?", (*closed, history_limit))
    ids = [r["id"] for r in rows]
    work_ids = sorted({r["work_id"] for r in rows if r.get("work_id")})
    authors: dict[str, list[str]] = {}
    series: dict[str, dict[str, Any]] = {}
    events: dict[str, list[dict[str, Any]]] = {}
    files: dict[str, str] = {}
    roots = {r["id"]: r["path"] for r in db.all("SELECT id,path FROM root_folders")}
    if work_ids:
        ph = ",".join("?" * len(work_ids))
        for r in db.all(f"SELECT wc.work_id,c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.role='author' AND wc.work_id IN ({ph}) ORDER BY wc.ord", work_ids):  # nosec B608
            authors.setdefault(r["work_id"], []).append(r["name"])
        for r in db.all(f"SELECT sm.work_id,s.name,sm.position FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id IN ({ph}) ORDER BY s.name", work_ids):  # nosec B608
            series.setdefault(r["work_id"], {"name": r["name"], "position": r["position"]})
    if ids:
        ph = ",".join("?" * len(ids))
        for r in db.all(f"SELECT event_type,message,created_at,json_extract(data_json,'$.download_id') AS download_id FROM history WHERE json_extract(data_json,'$.download_id') IN ({ph}) ORDER BY created_at", ids):  # nosec B608
            events.setdefault(r["download_id"], []).append({"event_type": r["event_type"], "message": r["message"], "created_at": r["created_at"]})
    asset_ids = sorted({r["asset_id"] for r in rows if r.get("asset_id") and r["state"] == "imported"})
    if asset_ids:
        ph = ",".join("?" * len(asset_ids))
        for r in db.all(f"SELECT asset_id,path FROM media_files WHERE asset_id IN ({ph}) ORDER BY part_number,path", asset_ids):  # nosec B608
            files.setdefault(r["asset_id"], os.path.dirname(r["path"]))
    t = db.now()
    items = []
    for r in rows:
        state = r["state"]
        terminal = state in closed
        end_ts = (r.get("imported_at") or r.get("completed_at") or r["updated_at"]) if terminal else t
        parsed = _json_obj(r.get("release_parsed_json"))
        size = r.get("size_bytes") or r.get("release_size_bytes")
        progress = float(r.get("progress") or 0)
        if state == "imported":
            progress = 1.0
        downloaded = r.get("downloaded_bytes")
        if downloaded is None and size:
            downloaded = int(size * progress)
        # Unknown (None) when the release or its search row is gone; never guessed.
        automatic = None if r.get("search_automatic") is None else bool(r["search_automatic"])
        kind = r.get("asset_kind") or r.get("release_asset_kind")
        root_path = roots.get(r.get("asset_root_folder_id")) if r.get("asset_root_folder_id") else None
        destination = files.get(r.get("asset_id")) or root_path
        evs = events.get(r["id"], [])
        item = dict(r)
        for k in ("work_sort_title", "asset_root_folder_id", "rel_indexer_id", "release_size_bytes", "release_parsed_json", "release_search_id", "search_automatic", "release_asset_kind", "release_seeders"):
            item.pop(k, None)
        item.update({
            "media_kind": kind,
            "format": r.get("asset_format") or parsed.get("format"),
            "work_title": r.get("title"),
            "authors": authors.get(r.get("work_id"), []),
            "series": series.get(r.get("work_id")),
            "release_title": r.get("release_title") or r.get("source_title"),
            "indexer": r.get("indexer_name"),
            "client": r.get("client_name"),
            "client_kind": r.get("client_kind"),
            "size_bytes": size,
            "downloaded_bytes": downloaded,
            "progress_percent": round(progress * 100, 1),
            "speed_bps": r.get("speed_bps") if state == "downloading" else None,
            "eta_seconds": r.get("eta_seconds") if state == "downloading" else None,
            "state_label": STATE_LABELS.get(state, state.replace("_", " ").capitalize()),
            "state_group": state_group(state),
            "started_at": r["added_at"],
            "finished_at": (r.get("imported_at") or r.get("completed_at") or r["updated_at"]) if terminal else r.get("completed_at"),
            "duration_seconds": max(0, int(end_ts - r["added_at"])),
            "automatic": automatic,
            "grab_source": None if automatic is None else ("automatic" if automatic else "manual"),
            "grabbed_by": r.get("grabbed_by"),
            "release_score": r.get("release_score"),
            "failure_reason": r.get("error"),
            "destination": destination,
            "history": evs[-_HISTORY_EVENTS_PER_ITEM:],
        })
        items.append(item)
    counts = {g: 0 for g in STATE_GROUPS}
    for it in items:
        counts[it["state_group"]] += 1
    return {"items": items, "counts": counts}
