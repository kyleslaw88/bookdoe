"""Library-wide duplicate finder (admin): candidate groups for the Duplicates page, ranked and remembered.

The per-book and per-series finders (workdupes) answer "what is like THIS book?". This module answers "which books in the
whole library are one book twice?", stores the answer in ``duplicate_groups`` and keeps it fresh incrementally.

Matching reuses ``workdupes.load_facts`` / ``workdupes.compatible`` (same normalized title once series decoration is removed,
same author set, compatible language, no different series position) and ``catalogdupes.title_key`` for the blocking index, so
there is exactly one definition of "plausibly the same book". Nothing here merges anything.

Safety stance (same as ``confirm_dissimilar``): Works whose normalized titles differ are only ever grouped when they share an
identifier or hit a refresh conflict, are labelled ``titles_differ`` and are never offered to the bulk merge. Groups are
cliques: every member is compatible with every other one, otherwise the compatible PAIRS are listed separately. There is no
transitive grouping (A~B and B~C never makes A,B,C).

Confidence:
  high    refresh conflict (the provider refused to link one Work because the other holds the record), a shared identifier
          with agreeing titles, or one provider-backed Work + file-only Work(s) with the same author set
  medium  same title and author set otherwise; or an identifier shared by differing titles
  low     same title, no author known on either side

A group is a *scan result*, not a verdict: the canonical suggestion is the best metadata (provider record, description,
cover, series position, identifiers), never "the one with files".

Incremental: a run only re-examines title blocks (and identifiers/conflicts) that contain a Work changed since the last
complete scan. Interrupted runs resume from a stored cursor. A dismissed group stays dismissed until a member's identity
(title, provider record, authors, owned assets) changes.
"""
from __future__ import annotations

import hashlib
import itertools
import time
from typing import Any, Callable, Iterable

from .. import db
from . import catalogdupes, workdupes

STATE_ID = "main"
MAX_BLOCK = 300          # a title shared by more Works than this is a placeholder title, not a book
MAX_GROUP = 8
CHUNK_BLOCKS = 150
BUDGET_SECONDS = 900.0
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
KINDS = ("refresh_conflict", "file_vs_provider", "identifier", "title_author")
KIND_LABEL = {"refresh_conflict": "Refresh conflict", "file_vs_provider": "File vs provider record",
              "identifier": "Shared identifier", "title_author": "Same title and author"}
HARVEST_CURSOR_KEY = "duplicates.harvest_cursor"


# ---------------------------------------------------------------------------
# Refresh conflicts (recorded where metadata.link_work refuses)
# ---------------------------------------------------------------------------

def record_refresh_conflict(work_id: str, conflict_work_id: str, metadata_id: str | None = None) -> None:
    """Remember that refreshing ``work_id`` hit "another Work already holds this provider record" and who holds it.
    First-class evidence: these pairs are always high confidence candidates. Best effort, never raises into the refresh."""
    if not work_id or not conflict_work_id or work_id == conflict_work_id:
        return
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO work_refresh_conflicts(work_id,conflict_work_id,metadata_id,detected_at,attempts,resolved_at) VALUES (?,?,?,?,1,NULL) "
                "ON CONFLICT(work_id) DO UPDATE SET conflict_work_id=excluded.conflict_work_id,metadata_id=excluded.metadata_id,"
                "detected_at=excluded.detected_at,attempts=work_refresh_conflicts.attempts+1,resolved_at=NULL",
                (work_id, conflict_work_id, metadata_id, db.now()))
    except Exception:   # the refresh must report its own error, not ours
        pass


def clear_refresh_conflict(work_id: str) -> None:
    """The Work linked after all (or was merged): its conflict is history."""
    try:
        with db.tx() as conn:
            conn.execute("UPDATE work_refresh_conflicts SET resolved_at=? WHERE work_id=? AND resolved_at IS NULL", (db.now(), work_id))
    except Exception:
        pass


def open_conflicts() -> list[dict[str, Any]]:
    """Unresolved conflicts whose two Works both still exist."""
    return db.all("SELECT c.* FROM work_refresh_conflicts c WHERE c.resolved_at IS NULL "
                  "AND EXISTS (SELECT 1 FROM works a WHERE a.id=c.work_id) AND EXISTS (SELECT 1 FROM works b WHERE b.id=c.conflict_work_id)")


# ---------------------------------------------------------------------------
# Metadata quality and the recommended canonical
# ---------------------------------------------------------------------------

def load_extras(work_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Per-Work fields the quality ranking and the page need beyond workdupes' facts."""
    ids = list(dict.fromkeys(work_ids))
    out: dict[str, dict[str, Any]] = {}
    for chunk in workdupes._chunks(ids):
        m = ",".join("?" * len(chunk))
        for r in db.all(f"SELECT id,description,cover_url,metadata_source,metadata_id,first_publish_date,updated_at,subtitle FROM works WHERE id IN ({m})", tuple(chunk)):  # nosec B608
            out[r["id"]] = {"description": (r["description"] or "").strip(), "cover_url": r["cover_url"], "metadata_source": r["metadata_source"],
                            "metadata_id": r["metadata_id"], "first_publish_date": r["first_publish_date"], "updated_at": r["updated_at"] or 0,
                            "subtitle": r["subtitle"], "cover_cached": False, "asset_updated_at": 0}
        for r in db.all(f"SELECT work_id FROM cover_cache WHERE status='cached' AND work_id IN ({m})", tuple(chunk)):  # nosec B608
            if r["work_id"] in out:
                out[r["work_id"]]["cover_cached"] = True
        for r in db.all(f"SELECT work_id,MAX(updated_at) u FROM media_assets WHERE work_id IN ({m}) GROUP BY work_id", tuple(chunk)):  # nosec B608
            if r["work_id"] in out:
                out[r["work_id"]]["asset_updated_at"] = r["u"] or 0
    return out


def quality(f: dict[str, Any], x: dict[str, Any]) -> dict[str, Any]:
    """How good this Work's metadata is: a score, what it has and what it lacks. Files do not count: they are what a
    merge carries over, not what makes the record worth keeping."""
    has: list[str] = []
    lacks: list[str] = []
    score = 0

    def add(ok: bool, pts: int, label: str) -> None:
        nonlocal score
        if ok:
            score += pts
            has.append(label)
        else:
            lacks.append(label)
    add(f["provider_linked"], 40, "provider record")
    add(len(x["description"]) >= 40, 20, "description")
    add(bool(x["cover_url"] or x["cover_cached"]), 15, "cover")
    positioned = any(s["position"] for s in f["series"].values())
    add(bool(f["series"]), 7, "series")
    if f["series"]:
        add(positioned, 3, "series position")
    n = len(f["identifiers"])
    add(n > 0, 6, "identifiers")
    if n > 1:
        score += 2
    add(bool(x["first_publish_date"]), 2, "publication date")
    add(f["clean_title"], 2, "clean title")
    source = "provider" if f["provider_linked"] else ("file" if (x["metadata_source"] in (None, "local")) else x["metadata_source"])
    return {"score": score, "has": has, "lacks": lacks, "source": source, "source_label": "Provider record" if f["provider_linked"] else ("From file" if source == "file" else "Edited by hand")}


def recommend(work_ids: list[str], facts: dict[str, dict[str, Any]], extras: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The Work to keep (best metadata) with the reason, and each member's quality."""
    qs = {i: quality(facts[i], extras[i]) for i in work_ids if i in facts and i in extras}
    order = sorted(qs, key=lambda i: (-qs[i]["score"], not facts[i]["provider_linked"], facts[i]["added_at"], i))
    best = order[0]
    others = order[1:]
    why = []
    b = qs[best]
    if facts[best]["provider_linked"] and not all(facts[o]["provider_linked"] for o in others):
        why.append("it is linked to the provider record")
    gained = sorted(set(b["has"]) - set().union(*[set(qs[o]["has"]) for o in others])) if others else []
    gained = [g for g in gained if g != "provider record"]
    if gained:
        why.append("it has " + ", ".join(gained))
    if not why:
        why.append("the metadata is equally complete, so the older record is kept")
    top_other = max((qs[o]["score"] for o in others), default=0)
    reason = f"Best metadata ({b['score']} vs {top_other}): " + " and ".join(why) + ". Files of the other record(s) move onto it; none are deleted."
    return {"canonical_work_id": best, "reason": reason, "quality": qs}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _pair_info(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, list[str]]:
    ok, sig = workdupes.compatible(a, b)
    return ok, sig


def _file_only(f: dict[str, Any], x: dict[str, Any]) -> bool:
    return not f["provider_linked"] and x.get("metadata_source") in (None, "local")


def classify(ids: list[str], facts: dict[str, dict[str, Any]], extras: dict[str, dict[str, Any]], seed_reasons: Iterable[str] = (),
             conflict: bool = False) -> dict[str, Any]:
    """kind, confidence, score and reason codes for a candidate set (every member already pairwise compatible, or flagged
    by a conflict / shared identifier)."""
    members = [facts[i] for i in ids]
    reasons: list[str] = list(seed_reasons)
    shared_all = set.intersection(*[m["identifiers"] for m in members]) if members else set()
    pair_conf: list[tuple[bool, str]] = []
    titles_agree = all(a["key"] == b["key"] and a["full_key"] == b["full_key"] for a, b in itertools.combinations(members, 2))
    provider = [m for m in members if m["provider_linked"]]
    files = [m for m in members if _file_only(m, extras[m["id"]])]
    sigs: set[str] = set()
    for a, b in itertools.combinations(members, 2):
        _, sg = _pair_info(a, b)
        sigs |= set(sg)
    for code in ("title_equal", "title_equal_after_series_suffix", "same_authors", "same_position", "shared_identifier"):
        if code in sigs and code not in reasons:
            reasons.append(code)
    authors_known = [bool(m["authors"]) for m in members]
    same_authors = "same_authors" in sigs and all(authors_known)
    pairwise_shared = any(a["identifiers"] & b["identifiers"] for a, b in itertools.combinations(members, 2))
    if pairwise_shared and "shared_identifier" not in reasons:
        reasons.append("shared_identifier")
    one_provider = len(provider) == 1 and len(files) == len(members) - 1
    if one_provider:
        reasons.append("file_only_vs_provider")
    if conflict:
        kind, conf = "refresh_conflict", "high"
    elif not titles_agree:
        kind, conf = "identifier", "medium"
        reasons.append("titles_differ")
    elif pairwise_shared:
        kind, conf = "identifier", "high"
    elif one_provider:
        kind = "file_vs_provider"
        conf = "high" if same_authors else "medium"
    else:
        kind = "title_author"
        conf = "medium" if same_authors else ("low" if not any(authors_known) else "medium")
    if kind == "file_vs_provider" and not same_authors and "authors_unknown" not in reasons:
        reasons.append("authors_unknown")
    if kind == "title_author" and not any(authors_known):
        reasons.append("authors_unknown")
    if conflict and not titles_agree:
        reasons.append("titles_differ")
    score = {"high": 90, "medium": 60, "low": 30}[conf] + (5 if one_provider else 0) + (3 if same_authors else 0) + (2 if sigs & {"same_position"} else 0)
    return {"kind": kind, "confidence": conf, "score": score, "reasons": list(dict.fromkeys(reasons)), "shared_identifiers": sorted(f"{n}:{v}" for n, v in shared_all)[:5]}


def _signature(ids: list[str], facts: dict[str, dict[str, Any]], legacy: bool = False) -> str:
    """Identity of the members as a person would see it: a dismissed group comes back when this changes.
    ``legacy`` gives the old SHA-1 form (non-security use), only to recognise dismissals stored before the SHA-256 change."""
    h = hashlib.sha1(usedforsecurity=False) if legacy else hashlib.sha256()  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1  (legacy dismissal match only, not security)
    for i in sorted(ids):
        f = facts[i]
        h.update(f"{i}|{f['title']}|{int(f['provider_linked'])}|{','.join(sorted(f['authors']))}|{f['owned']}|{f['owned_files']}\n".encode())
    return h.hexdigest()[:20]


def members_key(ids: Iterable[str]) -> str:
    return ",".join(sorted(set(ids)))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _store(ids: list[str], info: dict[str, Any], facts: dict[str, dict[str, Any]], extras: dict[str, dict[str, Any]], scan_id: str, block_key: str | None) -> str:
    """Insert or refresh one group. A dismissal survives unless the members' signature changed."""
    rec = recommend(ids, facts, extras)
    key = members_key(ids)
    sig = _signature(ids, facts)
    t = db.now()
    series: dict[str, str] = {}
    for i in ids:
        for sid, s in facts[i]["series"].items():
            series[sid] = s["name"]
    authors = sorted({name for i in ids for name in facts[i]["authors"].values()})
    title = facts[rec["canonical_work_id"]]["title"]
    row = db.one("SELECT id,dismissed,dismissed_signature,first_seen_at FROM duplicate_groups WHERE members_key=?", (key,))
    dismissed = 0
    if row and row["dismissed"]:
        dismissed = 1 if row["dismissed_signature"] in (sig, _signature(ids, facts, legacy=True)) else 0
    values = (db.json_dump(sorted(ids)), info["kind"], info["confidence"], float(info["score"]), db.json_dump(info["reasons"] + [f"identifier:{x}" for x in info["shared_identifiers"]]),
              rec["canonical_work_id"], rec["reason"], title, ", ".join(authors), int(any(facts[i]["owned"] for i in ids)),
              db.json_dump([{"id": k, "name": v} for k, v in sorted(series.items())]), block_key, sig, t, scan_id)
    with db.tx() as conn:
        if row:
            conn.execute(
                "UPDATE duplicate_groups SET work_ids_json=?,kind=?,confidence=?,score=?,reasons_json=?,canonical_work_id=?,canonical_reason=?,title=?,authors=?,has_owned=?,"
                "series_json=?,block_key=?,signature=?,updated_at=?,scan_id=?,status=CASE WHEN status='merged' THEN 'open' ELSE status END,"
                "dismissed=?,dismissed_at=CASE WHEN ?=1 THEN dismissed_at ELSE NULL END,"
                "dismissed_signature=CASE WHEN ?=1 THEN ? ELSE dismissed_signature END WHERE id=?",
                (*values, dismissed, dismissed, dismissed, sig, row["id"]))
            return row["id"]
        gid = db.uid()
        conn.execute(
            "INSERT INTO duplicate_groups(id,members_key,work_ids_json,kind,confidence,score,reasons_json,canonical_work_id,canonical_reason,title,authors,has_owned,"
            "series_json,block_key,signature,first_seen_at,updated_at,scan_id,dismissed,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'open')",
            (gid, key, *values[:13], t, t, scan_id))
        return gid


MERGED_KEEP_SECONDS = 30 * 86400
LIVE = "NOT EXISTS (SELECT 1 FROM json_each(work_ids_json) j WHERE NOT EXISTS (SELECT 1 FROM works w WHERE w.id=j.value))"


def prune_missing() -> int:
    """Drop groups that name a Work that no longer exists (deleted, or merged some other way). A dismissal about a vanished
    Work means nothing. Groups a bulk merge consumed stay (hidden, status 'merged') for 30 days so the batch's Undo can
    open them again."""
    with db.tx() as conn:
        return conn.execute("DELETE FROM duplicate_groups WHERE (status<>'merged' OR updated_at<?) AND EXISTS "
                            "(SELECT 1 FROM json_each(work_ids_json) j WHERE NOT EXISTS (SELECT 1 FROM works w WHERE w.id=j.value))",
                            (db.now() - MERGED_KEEP_SECONDS,)).rowcount


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def _state() -> dict[str, Any]:
    row = db.one("SELECT * FROM duplicate_scan_state WHERE id=?", (STATE_ID,))
    if not row:
        with db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO duplicate_scan_state(id,status) VALUES (?, 'idle')", (STATE_ID,))
        row = db.one("SELECT * FROM duplicate_scan_state WHERE id=?", (STATE_ID,))
    return row


HEARTBEAT_KEY = "duplicates.scan_heartbeat"


def _beat(budget_seconds: float) -> None:
    db.set_setting(HEARTBEAT_KEY, {"at": db.now(), "budget": float(budget_seconds)})


def effective_status(st: dict[str, Any]) -> str:
    """The stored status, except that a scan marked 'running' whose heartbeat is older than twice its budget was interrupted
    (crash, restart, killed job): it reads as 'partial' (resumable) instead of 'running' forever. Read-only."""
    if st["status"] != "running":
        return st["status"]
    hb = db.get_setting(HEARTBEAT_KEY, {}) or {}
    last = max(float(hb.get("at") or 0), float(st["started_at"] or 0))
    budget = float(hb.get("budget") or BUDGET_SECONDS)
    return "partial" if db.now() - last > 2 * budget else "running"


def _set_state(**fields: Any) -> None:
    _state()
    cols = ",".join(f"{k}=?" for k in fields)
    with db.tx() as conn:
        conn.execute(f"UPDATE duplicate_scan_state SET {cols} WHERE id=?", (*fields.values(), STATE_ID))  # nosec B608


def _blocks(only_changed: set[str] | None) -> dict[str, list[str]]:
    """Title index: block key -> Work ids, only blocks holding 2+ Works (and, for an incremental run, a changed one)."""
    series_names: dict[str, list[str]] = {}
    for r in db.all("SELECT sm.work_id,s.name FROM series_memberships sm JOIN series s ON s.id=sm.series_id"):
        series_names.setdefault(r["work_id"], []).append(r["name"])
    index: dict[str, set[str]] = {}
    for r in db.all("SELECT id,title FROM works"):
        keys = {catalogdupes.title_key(r["title"])}
        for name in series_names.get(r["id"], []):
            keys.add(catalogdupes.title_key(r["title"], name))
        for k in keys:
            if k:
                index.setdefault(k, set()).add(r["id"])
    out = {}
    for k, ids in index.items():
        if len(ids) < 2 or len(ids) > MAX_BLOCK:
            continue
        if only_changed is not None and not (ids & only_changed):
            continue
        out[k] = sorted(ids)
    return out


def changed_since(since: float | None) -> set[str] | None:
    """Works edited (or whose assets changed) after ``since``; None means everything (first or forced full scan)."""
    if since is None:
        return None
    ids = {r["id"] for r in db.all("SELECT id FROM works WHERE updated_at>?", (since,))}
    ids |= {r["work_id"] for r in db.all("SELECT DISTINCT work_id FROM media_assets WHERE updated_at>?", (since,))}
    for r in db.all("SELECT work_id,conflict_work_id FROM work_refresh_conflicts WHERE detected_at>?", (since,)):
        ids |= {r["work_id"], r["conflict_work_id"]}
    return ids


def _components(ids: list[str], facts: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Cliques of mutually compatible Works; where a component is not a clique its compatible pairs stand alone (no transitivity)."""
    adj = {i: set() for i in ids}
    for a, b in itertools.combinations(ids, 2):
        if _pair_info(facts[a], facts[b])[0]:
            adj[a].add(b)
            adj[b].add(a)
    seen: set[str] = set()
    groups: list[list[str]] = []
    for i in ids:
        if i in seen or not adj[i]:
            continue
        comp, stack = set(), [i]
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            stack.extend(adj[n] - comp)
        seen |= comp
        if all(adj[x] >= comp - {x} for x in comp):
            if len(comp) <= MAX_GROUP:
                groups.append(sorted(comp))
        else:
            for a, b in itertools.combinations(sorted(comp), 2):
                if b in adj[a]:
                    groups.append([a, b])
    return groups


def _process_blocks(keys: list[str], blocks: dict[str, list[str]], scan_id: str) -> int:
    ids = sorted({i for k in keys for i in blocks[k]})
    facts = workdupes.load_facts(ids)
    extras = load_extras(ids)
    found = 0
    for k in keys:
        members = [i for i in blocks[k] if i in facts]
        for comp in _components(members, facts):
            info = classify(comp, facts, extras)
            _store(comp, info, facts, extras, scan_id, k)
            found += 1
    return found


def _process_identifiers(scan_id: str, changed: set[str] | None) -> int:
    """Works that share an ISBN / ASIN / provider id, whatever their titles say (titles that differ are labelled)."""
    by_value: dict[tuple[str, str], set[str]] = {}
    for r in db.all("SELECT i.namespace,LOWER(i.value) v,CASE i.entity_type WHEN 'work' THEN i.entity_id ELSE e.work_id END work_id "
                    "FROM identifiers i LEFT JOIN editions e ON i.entity_type='edition' AND e.id=i.entity_id WHERE i.entity_type IN ('work','edition')"):
        if r["work_id"]:
            by_value.setdefault((r["namespace"], str(r["v"])), set()).add(r["work_id"])
    cand: set[tuple[str, ...]] = set()
    for ids in by_value.values():
        if 2 <= len(ids) <= MAX_GROUP and (changed is None or ids & changed):
            cand.add(tuple(sorted(ids)))
    found = 0
    for chunk in _chunked(sorted(cand), 100):
        ids = sorted({i for c in chunk for i in c})
        facts = workdupes.load_facts(ids)
        extras = load_extras(ids)
        for c in chunk:
            if not all(i in facts for i in c):
                continue
            if len(c) == 2 or all(_pair_info(facts[a], facts[b])[0] for a, b in itertools.combinations(c, 2)):
                info = classify(list(c), facts, extras)
                # classify() only calls it an identifier match when the titles agree; make sure it IS shared.
                if info["kind"] == "title_author":
                    info["kind"], info["confidence"] = "identifier", "high"
                    info["reasons"].append("shared_identifier")
                _store(list(c), info, facts, extras, scan_id, "identifier")
                found += 1
            else:
                for a, b in itertools.combinations(c, 2):
                    if facts[a]["identifiers"] & facts[b]["identifiers"]:
                        info = classify([a, b], facts, extras)
                        _store([a, b], info, facts, extras, scan_id, "identifier")
                        found += 1
    return found


def _process_conflicts(scan_id: str) -> int:
    found = 0
    pairs = [(c["work_id"], c["conflict_work_id"]) for c in open_conflicts()]
    for chunk in _chunked(pairs, 100):
        ids = sorted({i for p in chunk for i in p})
        facts = workdupes.load_facts(ids)
        extras = load_extras(ids)
        for a, b in chunk:
            if a in facts and b in facts:
                info = classify([a, b], facts, extras, seed_reasons=["refresh_conflict"], conflict=True)
                _store([a, b], info, facts, extras, scan_id, "conflict")
                found += 1
    return found


def _chunked(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def scan(full: bool = False, budget_seconds: float = BUDGET_SECONDS, chunk_blocks: int = CHUNK_BLOCKS,
         on_progress: Callable[[dict[str, Any]], None] | None = None, max_chunks: int | None = None) -> dict[str, Any]:
    """Run (or resume) the library scan within a time budget. Returns the stored state; ``status`` is 'done', or 'partial'
    when the budget ran out (call again to resume from the cursor). ``max_chunks`` is for tests."""
    st = _state()
    t0 = time.monotonic()
    resuming = st["status"] in ("running", "partial") and st["scan_id"] and not full
    if resuming:
        scan_id, since, started = st["scan_id"], st["since"], st["started_at"]
        cursor = db.json_load(st["cursor"], {}) or {}
    else:
        scan_id, started = db.uid(), db.now()
        since = None if (full or not st["last_complete_at"]) else st["last_complete_at"]
        cursor = {}
    prune_missing()      # housekeeping lives here, never in a GET handler
    changed = changed_since(since)
    blocks = _blocks(changed)
    keys = sorted(blocks)
    done_key = cursor.get("key")
    pending = [k for k in keys if done_key is None or k > done_key]
    total = len(keys)
    prior = int(st["blocks_done"]) if resuming else 0
    _beat(budget_seconds)
    _set_state(status="running", scan_id=scan_id, mode="full" if since is None else "incremental", started_at=started, since=since, error=None,
               blocks_total=max(total, prior + len(pending)) if resuming else total, blocks_done=prior if resuming else 0, finished_at=None,
               groups_found=int(st["groups_found"]) if resuming else 0, works_total=len(changed) if changed is not None else db.one("SELECT COUNT(*) n FROM works")["n"])
    groups = int(st["groups_found"]) if resuming else 0
    done = prior
    chunks = 0
    try:
        while pending:
            if time.monotonic() - t0 > budget_seconds or (max_chunks is not None and chunks >= max_chunks):
                state = _state()
                _set_state(status="partial")      # resumable: the next scan() carries on from the cursor
                return {**_public(state), "status": "partial"}
            part, pending = pending[:chunk_blocks], pending[chunk_blocks:]
            groups += _process_blocks(part, blocks, scan_id)
            done += len(part)
            chunks += 1
            _beat(budget_seconds)
            _set_state(cursor=db.json_dump({"phase": "blocks", "key": part[-1]}), blocks_done=done, groups_found=groups)
            if on_progress:
                on_progress({"blocks_done": done, "blocks_total": total, "groups": groups})
        groups += _process_identifiers(scan_id, changed)
        groups += _process_conflicts(scan_id)
        _retire_unseen(scan_id, changed)
        counts = group_counts()
        finished = db.now()
        _set_state(status="idle", finished_at=finished, last_complete_at=started, cursor=None, blocks_done=total if not resuming else done,
                   groups_found=groups, counts_json=db.json_dump(counts))
        return {**_public(_state()), "status": "done"}
    except Exception as exc:
        _set_state(status="failed", error=str(exc)[:500])
        raise


def _retire_unseen(scan_id: str, changed: set[str] | None) -> None:
    """Open, undismissed groups with a changed Work that this scan did not find again no longer hold (a title was fixed,
    a provider link was made): remove them. A full scan retires every group it did not see."""
    # Merged groups are never retired here: a batch's Undo reopens them (prune_missing retires them after MERGED_KEEP_SECONDS).
    rows = db.all("SELECT id,work_ids_json FROM duplicate_groups WHERE (scan_id IS NOT ? ) AND dismissed=0 AND status<>'merged'", (scan_id,))
    gone = [r["id"] for r in rows if changed is None or set(db.json_load(r["work_ids_json"], [])) & changed]
    with db.tx() as conn:
        for chunk in _chunked(gone, 500):
            conn.execute(f"DELETE FROM duplicate_groups WHERE id IN ({','.join('?' * len(chunk))})", chunk)  # nosec B608
        if changed is None:   # a dismissal for a group a full scan no longer finds is moot
            conn.execute("DELETE FROM duplicate_groups WHERE scan_id IS NOT ? AND status<>'merged'", (scan_id,))


def _public(st: dict[str, Any]) -> dict[str, Any]:
    status = effective_status(st)
    return {"status": status, "resumable": status == "partial" and bool(st["scan_id"]), "interrupted": status == "partial" and st["status"] == "running",
            "mode": st["mode"], "scan_id": st["scan_id"], "started_at": st["started_at"], "finished_at": st["finished_at"],
            "last_complete_at": st["last_complete_at"], "blocks_total": st["blocks_total"], "blocks_done": st["blocks_done"],
            "works_considered": st["works_total"], "groups_found": st["groups_found"], "error": st["error"], "counts": db.json_load(st["counts_json"], {})}


def scan_job(job: dict[str, Any] | None = None, run_id: str | None = None) -> dict[str, Any]:
    """Scheduler entry (manual job): scan or resume; progress lands on the running job_runs row."""
    from . import scheduler
    config = db.json_load((job or {}).get("config_json"), {}) or {}

    def progress(p: dict[str, Any]) -> None:
        scheduler.touch_lease("duplicate-scan")
        with scheduler._active_lock:
            rid = scheduler._active.get("duplicate-scan", {}).get("run_id")
        if rid:
            with db.tx() as conn:
                conn.execute("UPDATE job_runs SET result_json=? WHERE id=? AND status='running'", (db.json_dump(p), rid))
    return scan(full=bool(config.get("full")), on_progress=progress)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def group_counts() -> dict[str, int]:
    rows = db.all("SELECT confidence,dismissed,COUNT(*) n FROM duplicate_groups WHERE status='open' AND " + LIVE + " GROUP BY confidence,dismissed")  # nosec B608
    out = {"high": 0, "medium": 0, "low": 0, "dismissed": 0, "open": 0}
    for r in rows:
        if r["dismissed"]:
            out["dismissed"] += r["n"]
        else:
            out[r["confidence"]] += r["n"]
            out["open"] += r["n"]
    return out


def status() -> dict[str, Any]:
    st = _public(_state())
    st["counts"] = group_counts()
    st["open_conflicts"] = len(open_conflicts())
    st["file_only_unrefreshed"] = db.one("SELECT COUNT(*) n FROM works WHERE metadata_id IS NULL AND metadata_source IS NOT 'manual'")["n"]
    return st


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "work_ids": db.json_load(r["work_ids_json"], []), "kind": r["kind"], "kind_label": KIND_LABEL.get(r["kind"], r["kind"]),
            "confidence": r["confidence"], "score": r["score"], "reasons": db.json_load(r["reasons_json"], []),
            "canonical_work_id": r["canonical_work_id"], "canonical_reason": r["canonical_reason"], "title": r["title"], "authors": r["authors"],
            "has_owned": bool(r["has_owned"]), "series": db.json_load(r["series_json"], []), "dismissed": bool(r["dismissed"]), "status": r["status"],
            "first_seen_at": r["first_seen_at"], "updated_at": r["updated_at"]}


def list_groups(confidence: str | None = None, kind: str | None = None, has_owned: bool | None = None, series_id: str | None = None,
                q: str | None = None, dismissed: bool = False, limit: int = 50, offset: int = 0, with_cards: bool = True) -> dict[str, Any]:
    # Read-only: groups naming a vanished Work are hidden here and deleted by the scan (prune_missing), never by a GET.
    where, params = ["status='open'", LIVE, "dismissed=?"], [int(dismissed)]
    if confidence in CONFIDENCE_ORDER:
        where.append("confidence=?")
        params.append(confidence)
    if kind in KINDS:
        where.append("kind=?")
        params.append(kind)
    if has_owned is not None:
        where.append("has_owned=?")
        params.append(int(has_owned))
    if series_id:
        where.append("EXISTS (SELECT 1 FROM json_each(series_json) j WHERE json_extract(j.value,'$.id')=?)")
        params.append(series_id)
    if q and q.strip():
        where.append("(title LIKE ? ESCAPE '\\' OR authors LIKE ? ESCAPE '\\')")
        params += [db.like_pattern(q.strip())] * 2
    clause = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM duplicate_groups WHERE {clause}", tuple(params))["n"]  # nosec B608
    rows = db.all(f"SELECT * FROM duplicate_groups WHERE {clause} ORDER BY CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, score DESC, title COLLATE NOCASE, id LIMIT ? OFFSET ?",  # nosec B608
                  (*params, max(1, min(limit, 200)), max(0, offset)))
    items = [_row(r) for r in rows]
    if with_cards:
        attach_cards(items)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def attach_cards(items: list[dict[str, Any]]) -> None:
    """Side-by-side summary of each member (title, authors, source, owned assets with formats and sizes, cover, series
    position, metadata quality), the recommended canonical first."""
    ids = list(dict.fromkeys(i for g in items for i in g["work_ids"]))
    if not ids:
        return
    facts = workdupes.load_facts(ids)
    extras = load_extras(ids)
    cards = {c["id"]: c for c in workdupes.work_cards(ids)}
    for g in items:
        members = []
        for wid in g["work_ids"]:
            c, f, x = cards.get(wid), facts.get(wid), extras.get(wid)
            if not (c and f and x):
                continue
            q = quality(f, x)
            members.append({
                "id": wid, "title": c["title"], "authors": c["authors"], "series": c["series"], "cover_url": c["cover_url"], "has_cover": bool(x["cover_url"] or x["cover_cached"]),
                "source": q["source"], "source_label": q["source_label"], "provider_linked": f["provider_linked"], "language": c["language"],
                "description": x["description"][:240], "first_publish_date": x["first_publish_date"], "quality": q,
                "assets": [{"id": a["id"], "kind": a["kind"], "format": a["format"], "status": a["status"], "monitored": bool(a["monitored"]),
                            "files": a["files"], "size_bytes": a["size_bytes"], "owned": a["owned"]} for a in c["assets"] if not a["derived"]],
                "owned_assets": c["owned_assets"], "owned_files": c["owned_files"], "total_bytes": c["total_bytes"], "identifiers": c["identifiers"],
                "recommended": wid == g["canonical_work_id"], "added_at": c["added_at"]})
        members.sort(key=lambda m: (not m["recommended"], m["title"].casefold()))
        g["members"] = members


def get_group(group_id: str) -> dict[str, Any] | None:
    r = db.one("SELECT * FROM duplicate_groups WHERE id=?", (group_id,))
    if not r:
        return None
    g = _row(r)
    attach_cards([g])
    return g


def dismiss(group_id: str, dismissed: bool, user: Any = None) -> dict[str, Any] | None:
    """Remember "not the same book" (or take it back). The group stays hidden until a member changes."""
    r = db.one("SELECT id,signature FROM duplicate_groups WHERE id=?", (group_id,))
    if not r:
        return None
    by = user.get("id") if isinstance(user, dict) else user
    with db.tx() as conn:
        if dismissed:
            conn.execute("UPDATE duplicate_groups SET dismissed=1,dismissed_at=?,dismissed_by=?,dismissed_signature=signature WHERE id=?", (db.now(), by, group_id))
        else:
            conn.execute("UPDATE duplicate_groups SET dismissed=0,dismissed_at=NULL,dismissed_by=NULL,dismissed_signature=NULL WHERE id=?", (group_id,))
    return {"id": group_id, "dismissed": dismissed}


# ---------------------------------------------------------------------------
# One-off: harvest refresh conflicts from file-only Works
# ---------------------------------------------------------------------------

def _lookup(work_id: str) -> dict[str, Any]:
    """Read-only provider lookup for one file-only Work: which provider record would it match? Nothing is linked and no
    metadata is written. Rate-limited / transient answers come back as that status (the caller stops and retries later)."""
    import httpx
    from . import metadata, provider_results
    waiting = provider_results.deferred(metadata.PROVIDER, "work", work_id)
    if waiting:
        return {"status": waiting.status, "retry_at": waiting.retry_at}
    w = db.one("SELECT title FROM works WHERE id=?", (work_id,))
    if not w:
        return {"status": "unmatched"}
    try:
        result = metadata.resolve(identifiers=metadata._work_identifiers(work_id), title=w["title"], authors=metadata._work_authors(work_id))
    except httpx.HTTPError as exc:
        failure = provider_results.classify_exception(exc, metadata.PROVIDER)
        if not failure.retryable:
            raise
        provider_results.record(metadata.PROVIDER, "work", work_id, failure)
        return {"status": failure.status, "retry_at": failure.retry_at}
    provider_results.record(metadata.PROVIDER, "work", work_id, provider_results.ProviderResult(provider_results.SUCCESS, metadata.PROVIDER))
    match = result.get("match")
    if match and float(result.get("confidence") or 0) >= 0.95:
        return {"status": "matched", "metadata_id": match["id"], "confidence": float(result.get("confidence") or 0)}
    return {"status": "unmatched"}


def harvest(limit: int = 25, delay: float = 1.0, sleep: Callable[[float], None] = time.sleep, lookup: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Ask the provider which record each of up to ``limit`` file-only Works (no provider record, not hand-edited) would
    match, in id order from a stored cursor, so repeated runs walk the whole library. READ-MOSTLY: nothing is linked and no
    metadata is overwritten from here; when the matched record is already held by another Work that pair is recorded as a
    refresh conflict (a top-ranked duplicate candidate), otherwise the Work is only counted as ``linkable`` (linking it
    is the ordinary Refresh metadata action). Rate limited: ``delay`` seconds between provider calls, and the run stops at
    the first rate-limited / transient answer WITHOUT advancing past that Work. Capped at 500 per run."""
    lookup = lookup or _lookup
    limit = max(1, min(int(limit), 500))
    cursor = db.get_setting(HARVEST_CURSOR_KEY) or ""
    rows = db.all("SELECT id FROM works WHERE metadata_id IS NULL AND metadata_source IS NOT 'manual' AND id>? ORDER BY id LIMIT ?", (cursor, limit))
    out = {"tried": 0, "linkable": 0, "conflicts": 0, "unmatched": 0, "stopped": None, "errors": [], "wrapped": False}
    last = cursor
    for i, w in enumerate(rows):
        if i:
            sleep(delay)
        try:
            res = lookup(w["id"])
        except Exception as exc:
            out["tried"] += 1
            out["errors"].append({"work_id": w["id"], "error": str(exc)[:200]})
            last = w["id"]
            continue
        status = res.get("status")
        if status in ("rate_limited", "transient", "deferred"):
            out["stopped"] = status          # the cursor stays BEFORE this Work: the next run retries it
            break
        out["tried"] += 1
        last = w["id"]
        if status == "matched":
            holder = db.one("SELECT id FROM works WHERE metadata_id=? AND id<>?", (res.get("metadata_id"), w["id"]))
            if holder:
                record_refresh_conflict(w["id"], holder["id"], res.get("metadata_id"))
                out["conflicts"] += 1
            else:
                out["linkable"] += 1
        else:
            out["unmatched"] += 1
    if len(rows) < limit and out["stopped"] is None:
        last, out["wrapped"] = "", True          # reached the end of the library: the next run starts over
    db.set_setting(HARVEST_CURSOR_KEY, last)
    out["open_conflicts"] = len(open_conflicts())
    return out


def harvest_job(job: dict[str, Any] | None = None) -> dict[str, Any]:
    config = db.json_load((job or {}).get("config_json"), {}) or {}
    return harvest(limit=int(config.get("batch") or 25), delay=float(config.get("delay") or 1.0))
