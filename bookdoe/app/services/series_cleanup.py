"""Re-run the series parser over what is already stored and fix what it finds.

`reparse_all(dry_run=True)` builds a plan; `dry_run=False` applies the same plan in ONE
transaction and journals the BEFORE state of every row it touches (series_cleanup_runs /
series_cleanup_changes, migration 37) so `undo_last_cleanup()` can put them back.

What it may change: series rows (rename, create, delete when empty), series_memberships
(position, which series), series_assertions (re-pointed, rejected, extra rows) and
series_monitors (re-pointed to the surviving series). What it never touches: works,
media_assets, files, tags, anything on disk. A membership the user edited by hand is left
alone (see _is_manual): there is no manual flag in the schema, so "manual" means the
assertion status is manually_*, the work was edited by hand and nothing on record backs
the series link, or - for the POSITION only - the position differs from every assertion.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from .. import db
from . import metadata as metadata_service
from . import seriesnorm

SAMPLE_LIMIT = 50
CATEGORIES = (
    "rename", "merge", "split", "drop_junk", "clear_position", "fill_position",
    "delete_empty_series", "monitor_repointed", "skipped_manual",
)
_PK = {
    "series": ("id",),
    "series_memberships": ("work_id", "series_id"),
    "series_assertions": ("id",),
    "series_monitors": ("series_id",),
}
_POLICY_RANK = {"none": 0, "future": 1, "all": 2}


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
class _Journal:
    """Applies row changes and records each one's before state."""

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        self.conn, self.run_id = conn, run_id

    def _log(self, table: str, op: str, key: dict[str, Any], before: dict[str, Any] | None) -> None:
        self.conn.execute(
            "INSERT INTO series_cleanup_changes(run_id,table_name,op,row_key,before_json) VALUES (?,?,?,?,?)",
            (self.run_id, table, op, json.dumps(key, sort_keys=True), json.dumps(before) if before is not None else None),
        )

    def _row(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        where = " AND ".join(f"{c}=?" for c in key)
        r = self.conn.execute(f"SELECT * FROM {table} WHERE {where}", tuple(key.values())).fetchone()  # nosec B608
        return dict(r) if r else None

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        self.conn.execute(f"INSERT INTO {table}({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))  # nosec B608
        self._log(table, "insert", {c: row[c] for c in _PK[table]}, None)

    def update(self, table: str, key: dict[str, Any], changes: dict[str, Any]) -> None:
        before = self._row(table, key)
        if before is None:
            return
        sets = ",".join(f"{c}=?" for c in changes)
        where = " AND ".join(f"{c}=?" for c in key)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", (*changes.values(), *key.values()))  # nosec B608
        after_key = {**key, **{c: v for c, v in changes.items() if c in _PK[table]}}
        self._log(table, "update", after_key, before)

    def delete(self, table: str, key: dict[str, Any]) -> None:
        before = self._row(table, key)
        if before is None:
            return
        where = " AND ".join(f"{c}=?" for c in key)
        self.conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(key.values()))  # nosec B608
        self._log(table, "delete", key, before)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def _norm_pos(p) -> str | None:
    if p is None or not str(p).strip():
        return None
    return seriesnorm.normalize_position(str(p))


def _is_manual(work_source: str | None, assertions: list[dict[str, Any]]) -> bool:
    """True when this membership must not be touched at all."""
    if any(a["status"] in ("manually_confirmed", "manually_suppressed") for a in assertions):
        return True
    # A hand-edited work with no recorded evidence for this link: the link IS the edit.
    return work_source == "manual" and not any(a["source"] != "manual" for a in assertions)


def _position_is_manual(position, assertions: list[dict[str, Any]]) -> bool:
    """A stored position that no assertion ever proposed was typed by a person."""
    if position is None or not str(position).strip() or not assertions:
        return False
    seen = {_norm_pos(a["proposed_position"]) for a in assertions}
    return _norm_pos(position) not in seen


class _Plan:
    def __init__(self, sample_limit: int):
        self.limit = sample_limit
        self.counts: Counter = Counter()
        self.samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def note(self, category: str, before: dict[str, Any], after: dict[str, Any] | None) -> None:
        self.counts[category] += 1
        if len(self.samples[category]) < self.limit:
            self.samples[category].append({"before": before, "after": after})


def _load(conn: sqlite3.Connection) -> dict[str, Any]:
    series = {r["id"]: dict(r) for r in conn.execute("SELECT id,name,sort_name,norm_key,metadata_id FROM series")}
    members = [dict(r) for r in conn.execute(
        """SELECT sm.work_id,sm.series_id,sm.position,w.title,w.metadata_source
           FROM series_memberships sm JOIN works w ON w.id=sm.work_id ORDER BY sm.series_id,sm.work_id"""
    )]
    assertions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM series_assertions WHERE series_id IS NOT NULL"):
        assertions[(r["work_id"], r["series_id"])].append(dict(r))
    monitors = {r["series_id"]: dict(r) for r in conn.execute("SELECT * FROM series_monitors")}
    return {"series": series, "members": members, "assertions": assertions, "monitors": monitors}


def _raw_positions(raw_values: set[str], target_key: str, opts: seriesnorm.SeriesOptions) -> str | None:
    """A sane position for `target_key` read from the assertions' raw strings (the audit trail
    keeps the original text, which can say more than the stored series name does)."""
    for raw in sorted(raw_values):
        for c in seriesnorm.parse_series(raw, ignored=opts.ignored, max_position=opts.max_position):
            if not c.ignored and c.position and seriesnorm.norm_key(c.name) == target_key:
                return c.position
    return None


def _decide(m: dict[str, Any], s: dict[str, Any], asserts: list[dict[str, Any]], opts: seriesnorm.SeriesOptions) -> dict[str, Any]:
    """What should happen to one membership: targets [(name, position)], rejected names."""
    cands = seriesnorm.parse_series(s["name"], ignored=opts.ignored, max_position=opts.max_position)
    real = [c for c in cands if not c.ignored]
    ignored = [c for c in cands if c.ignored]
    cur = m["position"]
    cur_text = str(cur).strip() if cur is not None else ""
    raw_values = {a["raw_value"] for a in asserts if a["raw_value"]}
    pos_manual = _position_is_manual(cur, asserts)
    cur_ok = bool(cur_text) and seriesnorm.position_problem(cur_text, opts.max_position) is None

    targets: list[tuple[str, str | None]] = []
    for c in real:
        pos = c.position
        if pos is None:
            pos = _raw_positions(raw_values, seriesnorm.norm_key(c.name), opts)
        targets.append((c.name, pos))

    skipped = False
    if len(targets) == 1:
        name, cpos = targets[0]
        if pos_manual:
            new_pos = cur_text
        elif cur_ok:
            new_pos = cur_text
        else:
            new_pos = cpos
        targets = [(name, new_pos)]
    elif len(targets) > 1 and pos_manual:
        skipped = True  # a hand-set position cannot be assigned to one of several series
    return {"targets": targets, "ignored": ignored, "skipped": skipped, "cands": cands, "cur_ok": cur_ok,
            "cur_text": cur_text}


def _sort_key(name: str) -> str:
    return metadata_service._sort_title(name)


def reparse_all(dry_run: bool = True, *, actor: str | None = None, sample_limit: int = SAMPLE_LIMIT) -> dict[str, Any]:
    """Plan (dry_run=True, no writes) or apply the series reparse. See the module docstring.

    Returns {dry_run, totals, categories: {name: {count, samples:[{before, after}]}}} and,
    when applied, the run id (undo_last_cleanup reverses it)."""
    if dry_run:
        conn = db.connect(readonly=True)
        try:
            return _run(conn, None, sample_limit, actor)
        finally:
            conn.close()
    with db.tx() as conn:
        run_id = db.uid()
        conn.execute(
            "INSERT INTO series_cleanup_runs(id,created_at,created_by,status,summary_json) VALUES (?,?,?,'applied','{}')",
            (run_id, db.now(), actor),
        )
        result = _run(conn, _Journal(conn, run_id), sample_limit, actor)
        conn.execute("UPDATE series_cleanup_runs SET summary_json=? WHERE id=?", (json.dumps(_summary(result)), run_id))
        result["run_id"] = run_id
        return result


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {"totals": result["totals"], "counts": {k: v["count"] for k, v in result["categories"].items()}}


def _run(conn: sqlite3.Connection, journal: _Journal | None, sample_limit: int, actor: str | None) -> dict[str, Any]:
    opts = seriesnorm.load_options(conn)
    data = _load(conn)
    series, members, assertions, monitors = data["series"], data["members"], data["assertions"], data["monitors"]
    plan = _Plan(sample_limit)
    by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in members:
        by_series[m["series_id"]].append(m)

    # 1. Decide per membership.
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    for m in members:
        s = series[m["series_id"]]
        asserts = assertions.get((m["work_id"], m["series_id"]), [])
        if _is_manual(m["metadata_source"], asserts):
            decisions[(m["work_id"], m["series_id"])] = {"manual": True}
            plan.note("skipped_manual", {"series": s["name"], "position": m["position"], "work": m["title"]}, None)
            continue
        d = _decide(m, s, asserts, opts)
        decisions[(m["work_id"], m["series_id"])] = d
        if d["skipped"]:
            d["manual"] = True
            plan.note("skipped_manual", {"series": s["name"], "position": m["position"], "work": m["title"]}, None)

    # 2. Which source series keep their name (they anchor a group) and which are renamed
    #    wholesale. A series that still has a member whose parse gives its own name, or a
    #    hand-edited member, keeps its name; the rest of its members move out.
    keep_name: set[str] = set()
    whole_rename: dict[str, str] = {}
    for sid, ms in by_series.items():
        ds = [decisions[(m["work_id"], sid)] for m in ms]
        own = series[sid]["name"]
        if any(d.get("manual") or any(n == own for n, _ in d["targets"]) for d in ds):
            keep_name.add(sid)
            continue
        names = {d["targets"][0][0] for d in ds if len(d["targets"]) == 1}
        if names and len(names) == 1 and all(len(d["targets"]) <= 1 for d in ds):
            whole_rename[sid] = next(iter(names))
    for sid in series:
        if sid not in by_series:
            keep_name.add(sid)  # empty series are not this tool's business

    # 3. Group targets by norm_key (exact, then similar() for changed names only).
    groups: dict[str, dict[str, Any]] = {}
    key_index: dict[str, str] = {}
    first_char: dict[str, list[str]] = defaultdict(list)

    def group_for(name: str) -> dict[str, Any]:
        key = seriesnorm.norm_key(name) or name.lower()
        gk = key_index.get(key)
        if gk is None:
            for other in first_char[key[:1]]:
                if seriesnorm.similar(key, other):
                    gk = key_index[other]
                    break
        if gk is None:
            gk = key
            groups[gk] = {"key": key, "names": Counter(), "anchors": [], "renames": []}
            first_char[key[:1]].append(key)
        key_index[key] = gk
        return groups[gk]

    for sid in sorted(keep_name):
        if sid in by_series:
            g = group_for(series[sid]["name"])
            g["anchors"].append(sid)
    for sid, ms in by_series.items():
        for m in ms:
            d = decisions[(m["work_id"], sid)]
            if d.get("manual"):
                continue
            for name, _ in d["targets"]:
                group_for(name)["names"][name] += 1
    for sid, name in whole_rename.items():
        group_for(name)["renames"].append(sid)

    created: dict[str, str] = {}  # group key -> new series id (apply) / placeholder (dry run)
    dest_of_group: dict[str, str] = {}
    display_name: dict[str, str] = {}
    for gk, g in groups.items():
        anchors = sorted(g["anchors"], key=lambda sid: (-len(by_series[sid]), sid))
        renames = sorted(g["renames"], key=lambda sid: (-len(by_series[sid]), sid))
        if anchors:
            dest = anchors[0]
            display_name[gk] = series[dest]["name"]
        else:
            names = [n for n, c in g["names"].items() for _ in range(c)] or [series[renames[0]]["name"]]
            display_name[gk] = seriesnorm.pick_canonical(names)
            if renames:
                dest = renames[0]
            else:
                dest = "new:" + gk
                created[gk] = dest
        dest_of_group[gk] = dest

    def dest_for(name: str) -> tuple[str, str]:
        g = group_for(name)
        gk = key_index[seriesnorm.norm_key(name) or name.lower()]
        return dest_of_group[gk], display_name[gk]

    # 4. Walk memberships, record the plan, remember what to execute.
    ops: list[tuple] = []
    remaining: Counter = Counter({sid: len(ms) for sid, ms in by_series.items()})
    renamed_series: dict[str, str] = {}
    for gk, dest in dest_of_group.items():
        if dest in whole_rename and not dest.startswith("new:") and series[dest]["name"] != display_name[gk]:
            renamed_series[dest] = display_name[gk]
    for sid, newname in renamed_series.items():
        plan.note("rename", {"series": series[sid]["name"], "books": len(by_series[sid])}, {"series": newname})
    moved_to: dict[str, Counter] = defaultdict(Counter)
    future_members: dict[tuple[str, str], str | None] = {(m["work_id"], m["series_id"]): m["position"] for m in members}

    for m in members:
        sid, wid = m["series_id"], m["work_id"]
        d = decisions[(wid, sid)]
        s = series[sid]
        if d.get("manual"):
            continue
        before = {"series": s["name"], "position": m["position"], "work": m["title"]}
        if not d["targets"]:
            plan.note("drop_junk", before, None)
            ops.append(("drop", m, d))
            remaining[sid] -= 1
            future_members.pop((wid, sid), None)
            continue
        placed = [(*dest_for(name), pos) for name, pos in d["targets"]]
        main_dest, main_name, main_pos = placed[0]
        cur_text = d["cur_text"]
        if len(placed) > 1:
            plan.note("split", before, {"series": [p[1] + (f" #{p[2]}" if p[2] else "") for p in placed]})
        elif main_dest != sid:
            plan.note("merge", before, {"series": main_name, "position": main_pos})
        if cur_text and not d["cur_ok"] and (main_pos or "") != cur_text:
            plan.note("clear_position", before, {"series": main_name, "position": main_pos})
        elif not cur_text and main_pos:
            plan.note("fill_position", before, {"series": main_name, "position": main_pos})
        for dest, _, _ in placed:
            moved_to[sid][dest] += 1
        if len(placed) == 1 and main_dest == sid:
            ops.append(("keep", m, d, main_pos))
            continue
        ops.append(("move", m, d, placed))
        remaining[sid] -= 1
        future_members.pop((wid, sid), None)

    # Series emptied by the moves: delete, re-pointing the monitor to where most members went.
    deletions: list[tuple[str, str | None]] = []
    for sid in by_series:
        if remaining[sid] > 0 or sid in renamed_series:
            continue
        dest = None
        for cand, _ in moved_to[sid].most_common():
            if cand != sid:
                dest = cand
                break
        if sid in monitors and dest is None:
            continue  # a followed series with nowhere to go stays (empty) rather than lose its monitor
        deletions.append((sid, dest))
        plan.note("delete_empty_series", {"series": series[sid]["name"]}, None)
        if sid in monitors and dest:
            plan.note("monitor_repointed", {"series": series[sid]["name"]},
                      {"series": display_name.get(next((k for k, v in dest_of_group.items() if v == dest), ""), series.get(dest, {}).get("name"))})

    totals = {
        "series_before": len(series),
        "memberships_before": len(members),
        "series_after": len(series) + len(created) - len(deletions),
        "memberships_after": None,
    }

    if journal is not None:
        _execute(conn, journal, series, assertions, monitors, ops, created, renamed_series, display_name, deletions,
                 dest_of_group, group_for, key_index)
        totals["memberships_after"] = conn.execute("SELECT COUNT(*) n FROM series_memberships").fetchone()["n"]
        totals["series_after"] = conn.execute("SELECT COUNT(*) n FROM series").fetchone()["n"]
    else:
        # Plain estimate: kept + placed (deduped per work/series) memberships.
        final = set(future_members)
        for op in ops:
            if op[0] == "move":
                for dest, _, _ in op[3]:
                    final.add((op[1]["work_id"], dest))
        totals["memberships_after"] = len(final)

    categories = {c: {"count": plan.counts.get(c, 0), "samples": plan.samples.get(c, [])} for c in CATEGORIES}
    return {"dry_run": journal is None, "totals": totals, "categories": categories}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _execute(conn, journal: _Journal, series, assertions, monitors, ops, created, renamed_series, display_name,
             deletions, dest_of_group, group_for, key_index) -> None:
    t = db.now()
    real_id: dict[str, str] = {}
    for gk, placeholder in created.items():
        sid = db.uid()
        real_id[placeholder] = sid
        name = display_name[gk]
        journal.insert("series", {"id": sid, "metadata_id": None, "name": name, "sort_name": _sort_key(name),
                                  "norm_key": seriesnorm.norm_key(name), "created_at": t, "updated_at": t})
    for sid, name in renamed_series.items():
        journal.update("series", {"id": sid}, {"name": name, "sort_name": _sort_key(name),
                                               "norm_key": seriesnorm.norm_key(name), "updated_at": t})

    def rid(x: str) -> str:
        return real_id.get(x, x)

    def has_member(wid: str, sid: str) -> dict[str, Any] | None:
        r = conn.execute("SELECT * FROM series_memberships WHERE work_id=? AND series_id=?", (wid, sid)).fetchone()
        return dict(r) if r else None

    for op in ops:
        kind, m, d = op[0], op[1], op[2]
        wid, sid = m["work_id"], m["series_id"]
        asserts = assertions.get((wid, sid), [])
        if kind == "drop":
            journal.delete("series_memberships", {"work_id": wid, "series_id": sid})
            for a in asserts:
                journal.update("series_assertions", {"id": a["id"]}, {"series_id": None, "status": "rejected", "updated_at": t})
            _reject_extra(conn, journal, wid, series[sid]["name"], d["ignored"], t)
            continue
        if kind == "keep":
            pos = op[3]
            if (pos or "") != d["cur_text"]:
                journal.update("series_memberships", {"work_id": wid, "series_id": sid}, {"position": pos})
                for a in asserts:
                    journal.update("series_assertions", {"id": a["id"]}, {"proposed_position": pos, "updated_at": t})
            _reject_extra(conn, journal, wid, series[sid]["name"], d["ignored"], t)
            continue
        placed = [(rid(dest), shown, pos) for dest, shown, pos in op[3]]
        main_dest, main_name, main_pos = placed[0]
        # The original membership row: move it to the main destination (or drop it on conflict).
        if main_dest == sid:
            if (main_pos or "") != d["cur_text"]:
                journal.update("series_memberships", {"work_id": wid, "series_id": sid}, {"position": main_pos})
        else:
            existing = has_member(wid, main_dest)
            if existing:
                if existing["position"] in (None, "") and main_pos:
                    journal.update("series_memberships", {"work_id": wid, "series_id": main_dest}, {"position": main_pos})
                journal.delete("series_memberships", {"work_id": wid, "series_id": sid})
            else:
                journal.update("series_memberships", {"work_id": wid, "series_id": sid},
                               {"series_id": main_dest, "position": main_pos})
        for a in asserts:
            journal.update("series_assertions", {"id": a["id"]},
                           {"series_id": main_dest, "normalized_name": main_name, "proposed_position": main_pos, "updated_at": t})
        for dest, shown, pos in placed[1:]:
            if dest != sid and not has_member(wid, dest):
                journal.insert("series_memberships", {"work_id": wid, "series_id": dest, "position": pos})
            journal.insert("series_assertions", {
                "id": db.uid(), "work_id": wid, "series_id": dest, "source": "reparse",
                "raw_value": series[sid]["name"], "normalized_name": shown, "proposed_position": pos,
                "evidence_type": "explicit_position" if pos else "free_text", "confidence": 0.8,
                "status": "auto_accepted", "created_at": t, "updated_at": t})
        _reject_extra(conn, journal, wid, series[sid]["name"], d["ignored"], t)

    for sid, dest in deletions:
        dest = rid(dest) if dest else None
        if conn.execute("SELECT 1 FROM series_memberships WHERE series_id=? LIMIT 1", (sid,)).fetchone():
            continue  # something still belongs here (concurrent edit): keep it
        for a in conn.execute("SELECT id FROM series_assertions WHERE series_id=?", (sid,)).fetchall():
            journal.update("series_assertions", {"id": a["id"]}, {"series_id": dest, "updated_at": t})
        if sid in monitors and dest:
            _repoint_monitor(conn, journal, monitors[sid], dest)
        elif sid in monitors:
            continue
        journal.delete("series", {"id": sid})


def _reject_extra(conn, journal: _Journal, wid: str, raw: str, ignored, t: float) -> None:
    """Record names the parser refused (publisher lines, collections) as rejected assertions."""
    for c in ignored:
        if conn.execute(
            "SELECT 1 FROM series_assertions WHERE work_id=? AND series_id IS NULL AND raw_value=? AND normalized_name=?",
            (wid, raw, c.name),
        ).fetchone():
            continue
        journal.insert("series_assertions", {
            "id": db.uid(), "work_id": wid, "series_id": None, "source": "reparse", "raw_value": raw,
            "normalized_name": c.name, "proposed_position": None, "evidence_type": "free_text",
            "confidence": 0.0, "status": "rejected", "created_at": t, "updated_at": t})


def _kind_policies(row: dict[str, Any]) -> tuple[str, str]:
    legacy = row["policy"] if row.get("policy") in _POLICY_RANK else "none"
    aud = row.get("audiobook_policy") or (legacy if row.get("audiobook") else "none")
    ebk = row.get("ebook_policy") or (legacy if row.get("ebook") else "none")
    return aud, ebk


def _repoint_monitor(conn, journal: _Journal, mon: dict[str, Any], dest: str) -> None:
    """Move a monitor to the surviving series; when it already has one, keep the stronger
    policy per kind so a merge never silently stops following something."""
    other = conn.execute("SELECT * FROM series_monitors WHERE series_id=?", (dest,)).fetchone()
    if not other:
        journal.update("series_monitors", {"series_id": mon["series_id"]}, {"series_id": dest})
        return
    other = dict(other)
    a1, e1 = _kind_policies(mon)
    a2, e2 = _kind_policies(other)
    aud = max(a1, a2, key=lambda p: _POLICY_RANK[p])
    ebk = max(e1, e2, key=lambda p: _POLICY_RANK[p])
    kinds = {aud, ebk}
    policy = "all" if "all" in kinds else "future" if "future" in kinds else "none"
    journal.update("series_monitors", {"series_id": dest}, {
        "audiobook_policy": aud, "ebook_policy": ebk, "policy": policy,
        "ebook": int(ebk != "none"), "audiobook": int(aud != "none"),
        "started_at": min(mon["started_at"], other["started_at"])})
    journal.delete("series_monitors", {"series_id": mon["series_id"]})


# ---------------------------------------------------------------------------
# Undo / status
# ---------------------------------------------------------------------------
def last_cleanup() -> dict[str, Any] | None:
    """The most recent applied (not yet undone) cleanup, for the "Undo last cleanup" button."""
    r = db.one("SELECT * FROM series_cleanup_runs WHERE status='applied' ORDER BY created_at DESC, rowid DESC LIMIT 1")
    if not r:
        return None
    return {"id": r["id"], "created_at": r["created_at"], "created_by": r["created_by"],
            "summary": json.loads(r["summary_json"] or "{}")}


def undo_last_cleanup(*, actor: str | None = None) -> dict[str, Any]:
    """Reverse the latest applied cleanup from its journal (newest change first)."""
    with db.tx() as conn:
        run = conn.execute(
            "SELECT * FROM series_cleanup_runs WHERE status='applied' ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if not run:
            raise ValueError("No series cleanup to undo")
        restored = skipped = 0
        rows = conn.execute("SELECT * FROM series_cleanup_changes WHERE run_id=? ORDER BY seq DESC", (run["id"],)).fetchall()
        for ch in rows:
            table, op = ch["table_name"], ch["op"]
            key = json.loads(ch["row_key"])
            before = json.loads(ch["before_json"]) if ch["before_json"] else None
            where = " AND ".join(f"{c}=?" for c in key)
            try:
                if op == "insert":
                    conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(key.values()))  # nosec B608
                elif op == "delete":
                    _restore(conn, table, before)
                else:  # update
                    if table in ("series_memberships", "series_monitors"):
                        conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(key.values()))  # nosec B608
                        _restore(conn, table, before)
                    else:
                        sets = ",".join(f"{c}=?" for c in before if c not in _PK[table])
                        pk = " AND ".join(f"{c}=?" for c in _PK[table])
                        conn.execute(f"UPDATE {table} SET {sets} WHERE {pk}",  # nosec B608
                                     (*[v for c, v in before.items() if c not in _PK[table]], *[before[c] for c in _PK[table]]))
                restored += 1
            except sqlite3.IntegrityError:
                skipped += 1  # e.g. the work was deleted since; the rest is still restored
        conn.execute("UPDATE series_cleanup_runs SET status='undone',undone_at=? WHERE id=?", (db.now(), run["id"]))
    return {"run_id": run["id"], "restored": restored, "skipped": skipped}


def _restore(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    """Put a journalled row back. Plain INSERT (never REPLACE, whose implicit delete would
    cascade through series children); UPDATE by key when the row is somehow present."""
    cols = list(row)
    try:
        conn.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(row.values()))  # nosec B608
    except sqlite3.IntegrityError:
        pk = _PK[table]
        sets = [c for c in cols if c not in pk]
        if not sets:
            return
        where = " AND ".join(f"{c}=?" for c in pk)
        cur = conn.execute(f"UPDATE {table} SET {','.join(f'{c}=?' for c in sets)} WHERE {where}",  # nosec B608
                           (*[row[c] for c in sets], *[row[c] for c in pk]))
        if cur.rowcount == 0:
            raise
