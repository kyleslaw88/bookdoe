"""Maintenance issue store, detector registry and Detect/Review/Dismiss lifecycle (P5-18, Part XXV).

An issue is one row per stable fingerprint, so reruns never duplicate an issue or lose a
review decision. Detectors are read-only: they inspect and yield Findings, they never
write to or move files (adopted and observe roots are inspected like managed ones).
Repairs are a separate task (P5-22a); this module only stores the repair hints.

Fingerprint = ``kind|subject_type|subject_id|discriminator``. It is deterministic and
never derived from detail text, which may change between runs. The discriminator
separates several issues of the same kind on the same subject; it is empty when the
subject can carry at most one issue of that kind. Rule per kind (a detector adding a
kind documents its rule here):

  job_failing   subject job/<job id>, discriminator ''  (a job has one failing state)
  duplicate_work, split_duplicate_work
                subject work_pair/<lower id>:<higher id>, discriminator ''  (one verdict per
                pair and kind; sorted ids make the pair order independent) (P5-21a)
  duplicate_edition
                subject edition_pair/<lower id>:<higher id>, discriminator ''  (P5-21a)

  missing_file, file_outside_roots   subject file/<file id>, discriminator ''
  db_path_mismatch   subject file/<file id> or asset/<asset id>; discriminator names the case:
                     relative_path | root_folder | owned_no_live_file | live_file_status | duplicate_path
                     (duplicate_path is keyed on the lowest file id of the colliding rows)
  invalid_epub, damaged_audio, missing_chapters, missing_embedded_metadata   subject file/<file id>, discriminator ''
  missing_cover      subject file/<file id> 'embedded' (no embedded art) or work/<work id> 'no_cover' (P5-20)

Lifecycle of a fingerprint across runs:

  not stored                         -> open            (event 'detected')
  open / acknowledged, re-found      -> last_seen and detail refreshed
  resolved / repaired, re-found      -> open            (event 'reopened')
  dismissed, re-found                -> stays dismissed (last_seen refreshed only)
  open / acknowledged, not re-found  -> resolved        (event 'auto_resolved'), but only
                                        when the run was COMPLETE; a run cut short by its
                                        budget or by an error never resolves anything.

Auto-resolve is scoped by kind: a complete run resolves the open issues of the detector's
declared kinds that it did not re-find. Two detectors sharing a kind would therefore resolve
each other's issues, so ``register_detector`` rejects a kind another detector already owns.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .. import db
from . import failures

SEVERITIES = ("info", "warning", "error")
STATUSES = ("open", "acknowledged", "dismissed", "resolved", "repaired")
DEFAULT_LIMIT = 1000
DEFAULT_SECONDS = 20.0

# One run at a time: a scheduled scan and a manual run must not interleave their
# resolve passes. The upserts are idempotent, so this only guards the resolve step.
_run_lock = threading.Lock()


class MaintenanceBusy(RuntimeError):
    """Another maintenance run is in progress."""


class IssueConflict(ValueError):
    """The requested lifecycle transition is not allowed from the issue's current status."""


@dataclass
class Finding:
    kind: str
    severity: str
    subject_type: str
    subject_id: str
    discriminator: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    repair_action: str | None = None
    bulk_safe: bool = False

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}|{self.subject_type}|{self.subject_id}|{self.discriminator}"


class DetectorContext:
    """Handed to a detector: its budget, and a way to report that it stopped early."""

    def __init__(self, limit: int, seconds: float):
        self.limit = limit
        self.deadline = time.monotonic() + seconds
        self.truncated = False

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


@dataclass(frozen=True)
class Detector:
    name: str
    kinds: tuple[str, ...]
    fn: Callable[[DetectorContext], Iterable[Finding]]
    limit: int = DEFAULT_LIMIT
    seconds: float = DEFAULT_SECONDS


DETECTORS: dict[str, Detector] = {}


def register_detector(name: str, kinds: Iterable[str], fn: Callable[[DetectorContext], Iterable[Finding]],
                      limit: int = DEFAULT_LIMIT, seconds: float = DEFAULT_SECONDS) -> Detector:
    """Register *fn* under *name*. A kind belongs to one detector (see the module docstring)."""
    det = Detector(name, tuple(kinds), fn, limit, seconds)
    for other in DETECTORS.values():
        taken = set(det.kinds) & set(other.kinds)
        if other.name != name and taken:
            raise ValueError(f"detector {name} declares kind(s) {sorted(taken)} already owned by {other.name}")
    DETECTORS[name] = det
    return det


def _failing_jobs(ctx: DetectorContext) -> Iterable[Finding]:
    """Built-in: a job whose most recent run failed (the same set the dashboard counts)."""
    for j in failures.failed_jobs():
        yield Finding("job_failing", "warning", "job", j["job_id"], detail={"name": j["name"], "kind": j["kind"], "error": j["error"]})


register_detector("failing-jobs", ("job_failing",), _failing_jobs, limit=200)


def _collect(det: Detector, limit: int, seconds: float) -> tuple[list[Finding], bool]:
    """Run the detector within budget. Returns (findings, complete)."""
    ctx = DetectorContext(limit, seconds)
    found: dict[str, Finding] = {}
    it = iter(det.fn(ctx))
    while True:
        if ctx.expired():
            ctx.truncated = True
            break
        try:
            f = next(it)
        except StopIteration:
            break
        if f.kind not in det.kinds:
            raise ValueError(f"detector {det.name} yielded undeclared kind {f.kind!r}")
        if f.severity not in SEVERITIES:
            raise ValueError(f"detector {det.name} yielded invalid severity {f.severity!r}")
        if f.fingerprint not in found and len(found) >= limit:
            ctx.truncated = True  # one more distinct finding than the budget allows
            break
        found[f.fingerprint] = f
    close = getattr(it, "close", None)
    if close:
        close()
    return list(found.values()), not ctx.truncated


def _event(conn, issue_id: str, event: str, detail: dict[str, Any] | None = None, user_id: str | None = None, at: float | None = None) -> None:
    conn.execute(
        "INSERT INTO maintenance_events(id,issue_id,event,detail_json,user_id,created_at) VALUES (?,?,?,?,?,?)",
        (db.uid(), issue_id, event, db.json_dump(detail or {}), user_id, at if at is not None else db.now()),
    )


def _apply(det: Detector, findings: list[Finding], complete: bool) -> dict[str, int]:
    """Upsert the findings and, for a complete run, resolve what was not re-found."""
    t = db.now()
    counts = {"found": len(findings), "opened": 0, "refreshed": 0, "reopened": 0, "auto_resolved": 0}
    seen = {f.fingerprint for f in findings}
    with db.tx() as conn:
        for f in findings:
            row = conn.execute("SELECT id,status FROM maintenance_issues WHERE fingerprint=?", (f.fingerprint,)).fetchone()
            if row is None:
                iid = db.uid()
                conn.execute(
                    "INSERT INTO maintenance_issues(id,kind,severity,subject_type,subject_id,fingerprint,status,detail_json,"
                    "repair_action,bulk_safe,first_seen,last_seen) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?)",
                    (iid, f.kind, f.severity, f.subject_type, f.subject_id, f.fingerprint, db.json_dump(f.detail),
                     f.repair_action, int(f.bulk_safe), t, t),
                )
                _event(conn, iid, "detected", {"detector": det.name}, at=t)
                counts["opened"] += 1
                continue
            reopen = row["status"] in ("resolved", "repaired")
            conn.execute(
                "UPDATE maintenance_issues SET severity=?,detail_json=?,repair_action=?,bulk_safe=?,last_seen=?,"
                "status=CASE WHEN ? THEN 'open' ELSE status END,resolved_at=CASE WHEN ? THEN NULL ELSE resolved_at END WHERE id=?",
                (f.severity, db.json_dump(f.detail), f.repair_action, int(f.bulk_safe), t, int(reopen), int(reopen), row["id"]),
            )
            if reopen:
                _event(conn, row["id"], "reopened", {"detector": det.name, "from": row["status"]}, at=t)
                counts["reopened"] += 1
            else:
                counts["refreshed"] += 1
        if complete and det.kinds:
            marks = ",".join("?" * len(det.kinds))
            stale = conn.execute(
                f"SELECT id,fingerprint FROM maintenance_issues WHERE kind IN ({marks}) AND status IN ('open','acknowledged')",  # nosec B608
                det.kinds,
            ).fetchall()
            for r in stale:
                if r["fingerprint"] in seen:
                    continue
                conn.execute("UPDATE maintenance_issues SET status='resolved',resolved_at=? WHERE id=?", (t, r["id"]))
                _event(conn, r["id"], "auto_resolved", {"detector": det.name}, at=t)
                counts["auto_resolved"] += 1
    return counts


def _run_one(det: Detector, limit: int | None, seconds: float | None) -> dict[str, Any]:
    # None means the detector's own budget; 0 is a real budget (nothing collected, run incomplete).
    limit = det.limit if limit is None else limit
    seconds = det.seconds if seconds is None else seconds
    try:
        findings, complete = _collect(det, limit, seconds)
    except Exception as exc:
        # A crashed detector saw only part of the library: never resolve on that.
        return {"complete": False, "error": str(exc), "found": 0, "opened": 0, "refreshed": 0, "reopened": 0, "auto_resolved": 0}
    return {"complete": complete, **_apply(det, findings, complete)}


def run_detector(name: str, limit: int | None = None, seconds: float | None = None) -> dict[str, Any]:
    """Run one detector; ``limit`` (findings) and ``seconds`` override its budget."""
    if name not in DETECTORS:
        raise KeyError(name)
    if not _run_lock.acquire(blocking=False):
        raise MaintenanceBusy("A maintenance run is already in progress")
    try:
        return {"detector": name, **_run_one(DETECTORS[name], limit, seconds)}
    finally:
        _run_lock.release()


def run_all(names: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the named detectors (default: all). The result is what job_runs.result_json
    stores: ``complete`` is true only when every detector ran to completion."""
    chosen = list(names) if names is not None else list(DETECTORS)
    unknown = [n for n in chosen if n not in DETECTORS]
    if unknown:
        raise KeyError(", ".join(unknown))
    if not _run_lock.acquire(blocking=False):
        raise MaintenanceBusy("A maintenance run is already in progress")
    try:
        per = {n: _run_one(DETECTORS[n], None, None) for n in chosen}
    finally:
        _run_lock.release()
    return {"complete": all(r["complete"] for r in per.values()), "detectors": per,
            "opened": sum(r["opened"] for r in per.values()), "auto_resolved": sum(r["auto_resolved"] for r in per.values())}


def scan_job() -> dict[str, Any]:
    """The maintenance-scan job body: every detector within its own budget."""
    try:
        result = run_all()
    except MaintenanceBusy as exc:
        return {"skipped": True, "reason": str(exc)}
    from . import repair
    result["asset_status"] = repair.sync_asset_status()
    return result


# ---------------------------------------------------------------------------
# Review lifecycle and read side
# ---------------------------------------------------------------------------

def _present(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["detail"] = db.json_load(out.pop("detail_json"), {})
    out["bulk_safe"] = bool(out["bulk_safe"])
    return out


def list_issues(kind: str | None = None, status: str | None = None, severity: str | None = None,
                limit: int = 50, offset: int = 0) -> dict[str, Any]:
    where, params = [], []
    for col, val in (("kind", kind), ("status", status), ("severity", severity)):
        if val:
            where.append(f"{col}=?")
            params.append(val)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = db.one(f"SELECT COUNT(*) n FROM maintenance_issues{clause}", params)["n"]  # nosec B608
    rows = db.all(f"SELECT * FROM maintenance_issues{clause} ORDER BY last_seen DESC, id LIMIT ? OFFSET ?", [*params, limit, offset])  # nosec B608
    return {"items": [_present(r) for r in rows], "total": total, "limit": limit, "offset": offset}


def get_issue(issue_id: str) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM maintenance_issues WHERE id=?", (issue_id,))
    if row is None:
        return None
    events = db.all("SELECT * FROM maintenance_events WHERE issue_id=? ORDER BY created_at, rowid", (issue_id,))
    return {**_present(row), "events": [{**{k: v for k, v in e.items() if k != "detail_json"}, "detail": db.json_load(e["detail_json"], {})} for e in events]}


def _transition(issue_id: str, event: str, target: str, allowed: tuple[str, ...], user_id: str | None) -> dict[str, Any] | None:
    with db.tx() as conn:
        row = conn.execute("SELECT status FROM maintenance_issues WHERE id=?", (issue_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != target:  # repeating a decision is a no-op, not a second audit event
            if row["status"] not in allowed:
                raise IssueConflict(f"Cannot {event} an issue that is {row['status']}")
            conn.execute("UPDATE maintenance_issues SET status=? WHERE id=?", (target, issue_id))
            _event(conn, issue_id, event, {"from": row["status"]}, user_id)
    return get_issue(issue_id)


def acknowledge(issue_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    return _transition(issue_id, "acknowledged", "acknowledged", ("open",), user_id)


def dismiss(issue_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    return _transition(issue_id, "dismissed", "dismissed", ("open", "acknowledged"), user_id)


def summary() -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = {}
    by_status = dict.fromkeys(STATUSES, 0)
    for r in db.all("SELECT kind,status,COUNT(*) n FROM maintenance_issues GROUP BY kind,status"):
        by_kind.setdefault(r["kind"], {})[r["status"]] = r["n"]
        by_status[r["status"]] += r["n"]
    return {"total": sum(by_status.values()), "by_status": by_status, "by_kind": by_kind,
            "unknown": detectors_quality.unknown_counts()}


# Registers the catalog duplicate detectors; at the bottom because they import this module.
from . import catalogdupes  # noqa: E402,F401

# Registers the DB-driven detectors (P5-19a); imported last because it needs the names above.
from . import detectors_db  # noqa: E402,F401

# Registers the file-quality detectors (P5-20).
from . import detectors_quality  # noqa: E402,F401
