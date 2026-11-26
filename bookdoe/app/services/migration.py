from __future__ import annotations

import hashlib
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from .. import db
from . import metadata, scanner

log = logging.getLogger(__name__)


def _free_space_bytes(path: str) -> int:
    """Return free bytes on the filesystem containing *path*."""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def check_free_space(
    root_path: str | None,
    total_planned_bytes: int,
    safety_margin: float = 0.10,
) -> None:
    """Raise ValueError unless the destination share has planned size plus margin free.

    The safety_margin is a fraction (0.10 for 10 %). Required space is
    ``ceil(total_planned_bytes * (1 + safety_margin))``; free == required passes.
    """
    if not root_path:
        raise ValueError(
            "No managed destination root is configured. "
            "Add a managed root folder before planning or running a migration."
        )
    required = math.ceil(total_planned_bytes * (1 + safety_margin))
    try:
        free = _free_space_bytes(root_path)
    except OSError as exc:
        raise ValueError(f"Cannot read free space of destination share {root_path}: {exc}")
    if free < required:
        raise ValueError(
            f"Insufficient free space on destination share {root_path}: "
            f"need {required} bytes ({total_planned_bytes} planned plus {safety_margin * 100:.0f}% safety margin), "
            f"but only {free} bytes are free"
        )


def _q(name: str) -> str:
    # identifiers come only from sqlite_master/PRAGMA, not user-provided SQL text
    return '"' + name.replace('"', '""') + '"'


def _tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: [r[1] for r in conn.execute(f"PRAGMA table_info({_q(n)})")] for n in names}


def inspect_readarr(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise ValueError(f"Readarr database not found: {path}")
    conn = db.connect(p, readonly=True)
    try:
        tables = _tables(conn)
        counts = {}
        for t in ("Authors", "Books", "Editions", "BookFiles", "RootFolders", "QualityProfiles", "History", "Tags"):
            if t in tables:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0]  # nosec B608
        return {
            "path": str(p),
            "size_bytes": p.stat().st_size,
            "tables": tables,
            "counts": counts,
            "compatible": "Books" in tables and "Authors" in tables,
        }
    finally:
        conn.close()


def _col(cols: list[str], *candidates: str) -> str | None:
    lower = {x.casefold(): x for x in cols}
    for c in candidates:
        if c.casefold() in lower:
            return lower[c.casefold()]
    return None


def _row_get(row: sqlite3.Row | dict, *names: str, default=None):
    keys = {k.casefold(): k for k in row.keys()}
    for n in names:
        k = keys.get(n.casefold())
        if k is not None:
            return row[k]
    return default


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").casefold())


def _author_map(conn: sqlite3.Connection, tables: dict[str, list[str]]) -> dict[Any, str]:
    """Map the key carried on Books to an author name.

    Real Readarr keeps no name on Authors at all: Books.AuthorMetadataId points at
    AuthorMetadata.Id, and only AuthorMetadata carries Name. Reading Authors alone
    yields no name for any row, which silently degrades every author to "Unknown" --
    and an unknown author makes title-only matching look like a confident match.
    Forks that do carry a name on Authors are still supported as a fallback.
    """
    out: dict[Any, str] = {}
    if "AuthorMetadata" in tables:
        for r in conn.execute("SELECT * FROM AuthorMetadata"):
            rid = _row_get(r, "Id", "id")
            name = _row_get(r, "Name", "SortName", "name")
            if rid is not None and name:
                out[rid] = name
    if "Authors" in tables:
        for r in conn.execute("SELECT * FROM Authors"):
            rid = _row_get(r, "Id", "id")
            name = _row_get(r, "Name", "AuthorName", "name")
            if rid is not None and name and rid not in out:
                out[rid] = name
    return out


def _edition_rows(conn: sqlite3.Connection, tables: dict[str, list[str]]) -> dict[Any, list[sqlite3.Row]]:
    if "Editions" not in tables:
        return {}
    out: dict[Any, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM Editions"):
        bid = _row_get(r, "BookId", "book_id")
        if bid is not None:
            out.setdefault(bid, []).append(r)
    return out


def _book_file_rows(conn: sqlite3.Connection, tables: dict[str, list[str]]) -> dict[Any, list[sqlite3.Row]]:
    """Group Readarr BookFiles rows by the Book they belong to.

    Real Readarr links files to *editions*, not books: BookFiles.EditionId ->
    Editions.Id -> Editions.BookId. Some schema variants (and older forks) carry a
    direct BookId column instead, so both are supported. Files whose edition has
    since been deleted cannot be attributed to a book and are reported separately
    rather than silently dropped.
    """
    if "BookFiles" not in tables:
        return {}
    edition_to_book: dict[Any, Any] = {}
    if "Editions" in tables:
        for e in conn.execute("SELECT * FROM Editions"):
            eid, bid = _row_get(e, "Id"), _row_get(e, "BookId", "book_id")
            if eid is not None and bid is not None:
                edition_to_book[eid] = bid
    out: dict[Any, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM BookFiles"):
        bid = _row_get(r, "BookId", "book_id")
        if bid is None:
            eid = _row_get(r, "EditionId", "edition_id")
            if eid is not None:
                bid = edition_to_book.get(eid)
        if bid is not None:
            out.setdefault(bid, []).append(r)
    return out


def _orphan_book_files(conn: sqlite3.Connection, tables: dict[str, list[str]]) -> int:
    """Count BookFiles that cannot be attributed to any surviving Book."""
    if "BookFiles" not in tables:
        return 0
    attributed = sum(len(v) for v in _book_file_rows(conn, tables).values())
    total = conn.execute("SELECT COUNT(*) FROM BookFiles").fetchone()[0]
    return max(0, total - attributed)


def _root_rows(conn: sqlite3.Connection, tables: dict[str, list[str]]) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM RootFolders")) if "RootFolders" in tables else []


def analyze_readarr(path: str, media_kind: str, source_name: str | None = None) -> dict[str, Any]:
    if media_kind not in {"ebook", "audiobook"}:
        raise ValueError("media_kind must be ebook or audiobook")
    info = inspect_readarr(path)
    if not info["compatible"]:
        raise ValueError("Database does not look like Readarr: Authors/Books tables are required")
    run_id = db.uid()
    analysis = {
        "source": info,
        "media_kind": media_kind,
        "books": [],
        "root_folders": [],
        "warnings": [],
    }
    conn = db.connect(path, readonly=True)
    try:
        tables = info["tables"]
        authors = _author_map(conn, tables)
        editions = _edition_rows(conn, tables)
        files = _book_file_rows(conn, tables)
        orphans = _orphan_book_files(conn, tables)
        if orphans:
            analysis["warnings"].append(
                f"{orphans} BookFiles row(s) reference an edition that no longer exists and cannot be attributed to a book"
            )
        analysis["orphan_files"] = orphans
        for rr in _root_rows(conn, tables):
            analysis["root_folders"].append({"id": _row_get(rr, "Id"), "path": _row_get(rr, "Path"), "name": _row_get(rr, "Name")})
        for b in conn.execute("SELECT * FROM Books"):
            bid = _row_get(b, "Id")
            author_id = _row_get(b, "AuthorMetadataId", "AuthorId", "author_id")
            title = _row_get(b, "Title", "title", default="Unknown")
            monitored = bool(_row_get(b, "Monitored", "monitored", default=1))
            author = authors.get(author_id) or _row_get(b, "AuthorName", default="")
            book_editions = editions.get(bid, [])
            identifiers: dict[str, str] = {}
            for e in book_editions:
                for field, ns in (("Isbn13", "isbn13"), ("ISBN13", "isbn13"), ("Isbn", "isbn13"), ("Isbn10", "isbn10"), ("ASIN", "asin"), ("Asin", "asin")):
                    v = _row_get(e, field)
                    if v:
                        cleaned = re.sub(r"[^0-9Xx]", "", str(v)) if ns.startswith("isbn") else str(v).strip()
                        if cleaned:
                            identifiers["isbn10" if len(cleaned) == 10 and ns.startswith("isbn") else ns] = cleaned
            file_rows = []
            for f in files.get(bid, []):
                file_rows.append({
                    "id": _row_get(f, "Id"),
                    "path": _row_get(f, "Path", "RelativePath", "FilePath"),
                    "size": _row_get(f, "Size", "Size64", default=0),
                    "quality": _row_get(f, "Quality", default=None),
                    "part": _row_get(f, "Part", default=None),
                    "edition_id": _row_get(f, "EditionId", "edition_id", default=None),
                })
            analysis["books"].append({
                "source_id": bid,
                "title": title,
                "author": author,
                "monitored": monitored,
                "identifiers": identifiers,
                "files": file_rows,
                "foreign_id": _row_get(b, "ForeignBookId", "GoodreadsBookId", "ForeignId"),
            })
    finally:
        conn.close()
    with db.tx() as c:
        c.execute(
            """INSERT INTO migration_runs(id,source_kind,source_name,source_path,media_kind,status,dry_run,analysis_json,created_at,updated_at)
               VALUES (?,?,?,?,?,'analyzed',1,?,?,?)""",
            (run_id, "readarr", source_name or Path(path).stem, path, media_kind, db.json_dump(analysis), db.now(), db.now()),
        )
        for b in analysis["books"]:
            c.execute(
                "INSERT INTO migration_mappings(run_id,source_entity,source_id,confidence,evidence_json,action) VALUES (?,?,?,?,?,'review')",
                (run_id, "book", str(b["source_id"]), 0, db.json_dump({"title": b["title"], "author": b["author"], "identifiers": b["identifiers"]})),
            )
    return {"run_id": run_id, "summary": {"books": len(analysis["books"]), "roots": len(analysis["root_folders"]), "files": sum(len(b["files"]) for b in analysis["books"])}, "analysis": analysis}


def _destination_root(media_kind: str) -> str | None:
    """Return the managed root that receives writes for *media_kind* (adopted roots are never targets)."""
    row = db.one(
        "SELECT path FROM root_folders WHERE enabled=1 AND management_mode='managed' AND media_kind=? ORDER BY path LIMIT 1",
        (media_kind,),
    )
    return row["path"] if row else None


def _planned_bytes(analysis: dict[str, Any]) -> int:
    return sum(fr.get("size", 0) for book in analysis.get("books", []) for fr in book.get("files", []))


def plan_migration(run_id: str, *, safety_margin: float | None = None) -> dict[str, Any]:
    """Compute the migration plan and run a free-space preflight check.

    Returns a dict with ``total_planned_bytes``, ``required_bytes``,
    ``safety_margin``, and ``space_ok``.  Raises ``ValueError`` when the
    destination share does not have enough free space.
    """
    from ..config import settings

    if safety_margin is None:
        safety_margin = settings.migration_safety_margin

    run = db.one("SELECT * FROM migration_runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("Migration run not found")
    analysis = db.json_load(run["analysis_json"], {})

    total_planned_bytes = _planned_bytes(analysis)
    required_bytes = math.ceil(total_planned_bytes * (1 + safety_margin))
    check_free_space(_destination_root(run["media_kind"]), total_planned_bytes, safety_margin)
    space_ok = True

    return {
        "run_id": run_id,
        "total_planned_bytes": total_planned_bytes,
        "required_bytes": required_bytes,
        "safety_margin": safety_margin,
        "space_ok": space_ok,
    }


def reconcile_run(run_id: str, *, use_network: bool = True) -> dict[str, Any]:
    run = db.one("SELECT * FROM migration_runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("Migration run not found")
    analysis = db.json_load(run["analysis_json"], {})
    stats = {"resolved": 0, "probable": 0, "unresolved": 0, "errors": 0}
    for book in analysis.get("books", []):
        target_id, confidence, action, evidence = _reconcile_book(book, use_network=use_network)
        with db.tx() as conn:
            conn.execute(
                """UPDATE migration_mappings SET target_entity=?,target_id=?,confidence=?,evidence_json=?,action=?
                   WHERE run_id=? AND source_entity='book' AND source_id=?""",
                ("work" if target_id else None, target_id, confidence, db.json_dump(evidence), action, run_id, str(book["source_id"])),
            )
        if action == "import":
            stats["resolved"] += 1
        elif action == "probable":
            stats["probable"] += 1
        elif action == "error":
            stats["errors"] += 1
        else:
            stats["unresolved"] += 1
    with db.tx() as conn:
        conn.execute("UPDATE migration_runs SET status='reconciled',result_json=?,updated_at=? WHERE id=?", (db.json_dump(stats), db.now(), run_id))
    return {"run_id": run_id, **stats}


def _reconcile_book(book: dict[str, Any], *, use_network: bool) -> tuple[str | None, float, str, dict[str, Any]]:
    # Exact local identifier first.
    for ns, value in (book.get("identifiers") or {}).items():
        row = db.one("SELECT entity_type,entity_id FROM identifiers WHERE namespace=? AND value=?", (ns, value))
        if row:
            if row["entity_type"] == "work":
                return row["entity_id"], 1.0, "import", {"method": f"local {ns}"}
            if row["entity_type"] == "edition":
                e = db.one("SELECT work_id FROM editions WHERE id=?", (row["entity_id"],))
                if e:
                    return e["work_id"], 1.0, "import", {"method": f"local edition {ns}"}
    # Existing local normalized title+author enables cross-instance merge even if network is unavailable.
    rows = db.all(
        """SELECT DISTINCT w.id,w.title,c.name author FROM works w
           LEFT JOIN work_contributors wc ON wc.work_id=w.id AND wc.role='author'
           LEFT JOIN contributors c ON c.id=wc.contributor_id WHERE lower(w.title)=lower(?)""",
        (book.get("title") or "",),
    )
    author_known = bool(_norm(book.get("author"))) and book.get("author") != "Unknown"
    exact = [r for r in rows if author_known and _norm(r.get("author")) == _norm(book.get("author"))]
    if len(exact) == 1:
        return exact[0]["id"], 0.96, "import", {"method": "local title+author"}
    if use_network:
        try:
            res = metadata.resolve(identifiers=book.get("identifiers"), title=book.get("title"), authors=[book.get("author")] if book.get("author") else [])
            if res.get("match") and float(res.get("confidence", 0)) >= 0.95:
                wid = metadata.upsert_work(res["match"], monitored=book.get("monitored", True))
                return wid, float(res.get("confidence", 1)), "import", {"method": res.get("method"), "metadata_id": res["match"].get("id")}
            if res.get("candidates"):
                return None, float(res.get("confidence", 0)), "probable", {"method": "metadata candidates", "candidates": [x.get("id") for x in res["candidates"][:5]]}
        except Exception as exc:
            return None, 0, "error", {"method": "metadata error", "error": str(exc)}
    return None, 0, "review", {"method": "unresolved"}


def execute_run(
    run_id: str,
    *,
    import_unresolved_stubs: bool = True,
    safety_margin: float | None = None,
) -> dict[str, Any]:
    from ..config import settings

    if safety_margin is None:
        safety_margin = settings.migration_safety_margin

    run = db.one("SELECT * FROM migration_runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("Migration run not found")
    if run["status"] not in {"reconciled", "analyzed", "previewed"}:
        raise ValueError(f"Migration is not ready to execute (status={run['status']})")
    analysis = db.json_load(run["analysis_json"], {})
    mappings = {m["source_id"]: m for m in db.all("SELECT * FROM migration_mappings WHERE run_id=? AND source_entity='book'", (run_id,))}
    # Free-space preflight: reject before any changes if the destination share is too full.
    check_free_space(_destination_root(run["media_kind"]), _planned_bytes(analysis), safety_margin)
    # Adopt roots first, but never modify them. A source root is only worth importing
    # if this container can actually see it and no configured root already covers it:
    # Readarr's paths are its own container's view, so importing them blindly creates
    # dead roots (a path that does not exist here) or a second root over files an
    # existing root already owns, which would adopt the same media twice.
    for r in analysis.get("root_folders", []):
        path = (r.get("path") or "").rstrip("/")
        if not path:
            continue
        if db.one("SELECT id FROM root_folders WHERE rtrim(path,'/')=?", (path,)):
            continue
        if not Path(path).is_dir():
            log.info("skipping migration root %s: not visible in this container", path)
            continue
        if _covered_by_existing_root(path):
            log.info("skipping migration root %s: already covered by a configured root", path)
            continue
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?, 'adopt', ?,?)",
                (db.uid(), r.get("name") or Path(path).name or run["media_kind"].title(), path, run["media_kind"], db.now(), db.now()),
            )
    stats = {"works": 0, "files": 0, "stubs": 0, "review": 0, "missing_files": 0}
    for book in analysis.get("books", []):
        mapping = mappings.get(str(book["source_id"])) or {}
        work_id = mapping.get("target_id")
        if not work_id and import_unresolved_stubs:
            work_id = _find_or_create_stub(book)
            stats["stubs"] += 1
            _create_review(book, work_id, run_id)
            stats["review"] += 1
        if not work_id:
            continue
        stats["works"] += 1
        # Preserve source monitoring state at the asset level.
        asset_id = _asset_for_migration(work_id, run["media_kind"], bool(book.get("monitored", True)))
        for fr in book.get("files", []):
            resolved = _resolve_readarr_file_path(fr.get("path"), analysis.get("root_folders", []), book)
            if not resolved or not Path(resolved).exists():
                stats["missing_files"] += 1
                db.add_history("migration.file_missing", f"Readarr file was not found: {resolved or fr.get('path')}", work_id=work_id, asset_id=asset_id, data={"source": fr})
                continue
            p = Path(resolved)
            # Find the root that contains this file so path evidence extraction
            # can distinguish author folders from the root itself.
            root_path = None
            for r in analysis.get("root_folders", []):
                rpath = r.get("path", "")
                if rpath and str(p).startswith(rpath.rstrip("/") + "/"):
                    root_path = Path(rpath)
                    break
            try:
                detected = scanner.inspect_file(p, root=root_path)
            except Exception:
                detected = {"size_bytes": p.stat().st_size, "mtime": p.stat().st_mtime, "fingerprint": None}
            pc = scanner.probe_columns(detected)
            with db.tx() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO media_files(id,asset_id,path,size_bytes,mtime,fingerprint,container,codec,sample_rate,channels,chapter_count,has_embedded_cover,embedded_json,adopted,managed,missing,discovered_at,verified_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,0,?,?)""",
                    (db.uid(), asset_id, str(p), int(detected.get("size_bytes") or p.stat().st_size), detected.get("mtime"), detected.get("fingerprint"), p.suffix.lower().lstrip("."),
                     pc["codec"], pc["sample_rate"], pc["channels"], pc["chapter_count"], pc["has_embedded_cover"], db.json_dump(detected), db.now(), db.now()),
                )
                conn.execute("UPDATE media_assets SET status='owned',format=COALESCE(format,?),updated_at=? WHERE id=?", (p.suffix.lower().lstrip("."), db.now(), asset_id))
            stats["files"] += 1
    with db.tx() as conn:
        conn.execute("UPDATE migration_runs SET status='complete',dry_run=0,result_json=?,updated_at=? WHERE id=?", (db.json_dump(stats), db.now(), run_id))
    db.add_history("migration.complete", f"Completed {run['source_name']} migration", data={"run_id": run_id, **stats})
    return {"run_id": run_id, **stats}


def _find_or_create_stub(book: dict[str, Any]) -> str:
    title = book.get("title") or "Unknown"
    author = book.get("author") or "Unknown"
    # Cross-instance unresolved matching remains conservative: exact normalized title+author only.
    rows = db.all(
        """SELECT w.id,w.title,c.name author FROM works w LEFT JOIN work_contributors wc ON wc.work_id=w.id AND wc.role='author'
           LEFT JOIN contributors c ON c.id=wc.contributor_id WHERE w.metadata_status='unresolved'"""
    )
    # An unknown author is not evidence of sameness. Two different books can share a
    # title, so merging on title alone would silently fuse unrelated works.
    if _norm(author) and author != "Unknown":
        for row in rows:
            if _norm(row["title"]) == _norm(title) and _norm(row.get("author")) == _norm(author):
                return row["id"]
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,?, 'unresolved', ?,?)",
            (wid, title, title, int(bool(book.get("monitored", True))), t, t),
        )
        # One contributor row per *author*, not per book. Inserting a fresh row here
        # made the authors page one entry per imported title and made author-level
        # monitoring meaningless, because each row owned exactly one work.
        cid = db.contributor_id_for_name(conn, author)
        if cid:
            conn.execute("INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?, 'author',0)", (wid, cid))
        for ns, value in (book.get("identifiers") or {}).items():
            conn.execute(
                "INSERT OR IGNORE INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,?,?,?,0,0.5)",
                (wid, ns, value, "readarr-migration"),
            )
    return wid


def _asset_for_migration(work_id: str, kind: str, monitored: bool) -> str:
    """Return the single MediaAsset a Work owns for this media kind.

    A Work owns at most one asset per kind; differing file formats (m4b + mp3,
    epub + mobi) are multiple MediaFiles *of that one asset*, not separate assets.
    Preferring a format-less asset but falling back to any existing one of the same
    kind is what makes cross-instance convergence safe: when a second Readarr source
    resolves onto a Work already carrying, say, an m4b audiobook asset, we must reuse
    it. Creating a second asset would violate the
    UNIQUE(work_id, kind, format) constraint as soon as its format was set.
    """
    row = db.one(
        "SELECT id FROM media_assets WHERE work_id=? AND kind=? ORDER BY (format IS NOT NULL), added_at LIMIT 1",
        (work_id, kind),
    )
    if row:
        with db.tx() as conn:
            # Wanted state imported from Readarr is pre-existing user intent, so it
            # must survive the migration rather than be silently dropped.
            conn.execute("UPDATE media_assets SET monitored=?,has_acquisition_intent=?,updated_at=? WHERE id=?", (int(monitored), int(monitored), db.now(), row["id"]))
        return row["id"]
    aid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,profile_id,added_at,updated_at) VALUES (?,?,?,'missing',?,?,?,?,?)",
            (aid, work_id, kind, int(monitored), int(monitored), "audio-default" if kind == "audiobook" else "ebook-default", db.now(), db.now()),
        )
    return aid


def _create_review(book: dict[str, Any], work_id: str, run_id: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,priority,created_at) VALUES (?, 'metadata_match','work',?,?,?,?,?)",
            (db.uid(), work_id, f"Resolve metadata: {book.get('title')}", db.json_dump({"migration_run": run_id, "source": book}), 80, db.now()),
        )


def _bookdoe_root_paths() -> list[str]:
    return [r["path"] for r in db.all("SELECT path FROM root_folders WHERE enabled=1 ORDER BY path")]


def _dev_ino(path: str) -> tuple[int, int] | None:
    try:
        st = Path(path).stat()
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _covered_by_existing_root(path: str) -> bool:
    """True when a configured root already covers this directory.

    Path-prefix comparison is not sufficient: the same host directory is commonly
    bind-mounted at two container paths (/audiobooks and /data/audiobooks/library),
    which share no prefix yet are the same directory. Adding both as roots would
    adopt every file twice. Identity is therefore established by (st_dev, st_ino).
    """
    target = path.rstrip("/")
    tid = _dev_ino(target)
    for existing in _bookdoe_root_paths():
        ex = existing.rstrip("/")
        if target == ex or target.startswith(ex + "/") or ex.startswith(target + "/"):
            return True
        if tid is not None and _dev_ino(ex) == tid:
            return True
    return False


def _under_configured_root(path: str) -> bool:
    return any(path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/") for r in _bookdoe_root_paths())


def _remap_absolute(raw: str, source_roots: list[dict[str, Any]]) -> str | None:
    """Translate an absolute path recorded by Readarr into this container's view.

    Readarr stores paths as *its own* container saw them (e.g. /calibre_data/ebooks/...).
    bookdoe mounts the same media elsewhere (e.g. /books-legacy/...), so an absolute
    source path is only usable after the source root prefix is swapped for the matching
    bookdoe root. Verified against the filesystem; never guessed.
    """
    for sr in source_roots:
        sroot = (sr.get("path") or "").rstrip("/")
        if not sroot or not raw.startswith(sroot + "/"):
            continue
        rel = raw[len(sroot) + 1:]
        for aroot in _bookdoe_root_paths():
            candidate = Path(aroot) / rel
            if candidate.exists():
                return str(candidate)
    return None


def _resolve_readarr_file_path(raw: str | None, roots: list[dict[str, Any]], book: dict[str, Any]) -> str | None:
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        # Prefer a location inside a configured root so the file is manageable as
        # library content. The same media is often visible at several container
        # paths; only one of them is a root bookdoe actually knows about.
        if p.exists() and _under_configured_root(str(p)):
            return str(p)
        remapped = _remap_absolute(raw, roots)
        if remapped:
            return remapped
        if p.exists():
            return str(p)
        return str(p)
    # Readarr BookFiles often stores a relative file path. Root + book Path is
    # schema/version dependent, so test deterministic candidates only.
    for r in roots:
        root = r.get("path")
        if not root:
            continue
        candidate = Path(root) / raw
        if candidate.exists():
            return str(candidate)
        title_dir = Path(root) / str(book.get("title") or "") / raw
        if title_dir.exists():
            return str(title_dir)
    return str(p)
