"""One-time (and safely repeatable) re-check of books the classifier auto-approved.

Before 2026-10-12 a confident "kids" verdict was applied on the strength of a title and
a blurb alone. This re-evaluates every kids_decisions row with via='auto_approved' against
the deterministic gate (services/kidsgate) using the verdict already stored in
kids_classification and the metadata on record NOW. No LLM call is made: the stored
verdict is the model's word; the gate is code.

Nothing is ever untagged here. A work that fails the gate gets an open `kids_recheck`
review task (Inbox group "Auto-tagged without audience evidence") carrying the reasons;
only an administrator's keep (recorded as manual_tag) or remove (manual_untag) changes
the shelf, singly or through the Inbox bulk bar. Running it again adds no duplicates,
refreshes the reasons on tasks still open, and closes tasks for books that now pass or
that a person has since decided.
"""
from __future__ import annotations

from typing import Any

from .. import db
from . import audience, kidsgate, tagging

KIND = tagging.RECHECK_REVIEW_KIND
GROUP_LABEL = "Auto-tagged without audience evidence"
PRIORITY = 45


def _open_task(work_id: str) -> dict[str, Any] | None:
    return db.one("SELECT id FROM review_tasks WHERE kind=? AND entity_type='work' AND entity_id=? AND status='open'", (KIND, work_id))


def evaluate_work(work_id: str, decision_note: str | None) -> dict[str, Any]:
    """The gate outcome for one auto-approved work, from stored verdict data only."""
    kc = db.one("SELECT * FROM kids_classification WHERE work_id=?", (work_id,))
    detail = db.json_load((kc or {}).get("detail_json"), {}) or {}
    note = db.json_load(decision_note, {}) or {}
    verdict = kc["verdict"] if kc else "kids"  # an auto-approval implies a kids verdict even if the row was pruned
    confidence = kc["confidence"] if kc and kc["confidence"] is not None else note.get("confidence")
    ev = kidsgate.gather_evidence(work_id)
    gate = tagging._gate_for(ev, verdict, confidence if isinstance(confidence, (int, float)) else None,
                             detail.get("adult_signals") if detail.get("source") == "llm" else None)
    return {"gate": gate, "ev": ev, "verdict": verdict, "confidence": confidence, "model": detail.get("model") or note.get("model"),
            "audience_evidence": detail.get("audience_evidence") or [],
            "evidence_changed": bool(detail.get("evidence_hash")) and detail["evidence_hash"] != kidsgate.evidence_hash(ev)}


def _payload(row: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    g, ev = r["gate"], r["ev"]
    return {
        "title": row["title"], "verdict": r["verdict"], "confidence": r["confidence"], "model": r["model"],
        "reasons": g["reasons"], "blockers": g["blockers"], "strong_signals": g["strong_signals"], "adult_signals": g["adult_signals"],
        "audience_evidence": r["audience_evidence"], "evidence_changed": r["evidence_changed"], "auto_approved_at": row["decided_at"],
        "threshold": g["threshold"], "require_strong": g["require_strong"],
        "source": {"authors": ev["authors"], "author": ", ".join(ev["authors"]) or None, "description": ev.get("description", "")[:400] or None},
    }


def recheck_auto_approved(*, dry_run: bool = True, by: str | None = None) -> dict[str, Any]:
    """Evaluate every auto-approved work. dry_run reports the counts and writes nothing."""
    rows = db.all("""SELECT d.work_id,d.decided_at,d.note,w.title FROM kids_decisions d JOIN works w ON w.id=d.work_id
                     WHERE d.via='auto_approved' AND d.state='on' ORDER BY d.decided_at""")
    kid_tag = tagging.kids_tag_id()
    tagged = {r["work_id"] for r in db.all("SELECT work_id FROM work_tags WHERE tag_id=?", (kid_tag,))}
    passes = fails = queued = refreshed = closed = with_meta = 0
    by_blocker: dict[str, int] = {}
    failing: list[dict[str, Any]] = []
    for row in rows:
        wid = row["work_id"]
        if wid not in tagged:  # decision says on but the tag is gone: nothing to protect
            continue
        r = evaluate_work(wid, row["note"])
        with_meta += 1 if r["ev"].get("audience_sources") else 0  # provider categories / subjects / maturity on record
        existing = _open_task(wid)
        if r["gate"]["passed"]:
            passes += 1
            if existing and not dry_run:
                with db.tx() as conn:
                    conn.execute("UPDATE review_tasks SET status='resolved',resolved_at=?,resolution_json=? WHERE id=?",
                                 (db.now(), db.json_dump({"action": "now_passes_gate"}), existing["id"]))
                closed += 1
            continue
        fails += 1
        for b in r["gate"]["blockers"]:
            by_blocker[b] = by_blocker.get(b, 0) + 1
        payload = _payload(row, r)
        if len(failing) < 25:
            failing.append({"work_id": wid, "title": row["title"], "reasons": r["gate"]["reasons"]})
        if dry_run:
            continue
        with db.tx() as conn:
            if existing:
                conn.execute("UPDATE review_tasks SET payload_json=?,title=? WHERE id=?", (db.json_dump(payload), row["title"], existing["id"]))
                refreshed += 1
            else:
                conn.execute(
                    """INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at)
                       VALUES (?,?,'work',?,?,?,'open',?,?)""",
                    (db.uid(), KIND, wid, row["title"], db.json_dump(payload), PRIORITY, db.now()))
                queued += 1
    # Tasks for books a person has decided since (or whose tag is gone) are closed, not left to rot.
    if not dry_run:
        for t in db.all("SELECT t.id,t.entity_id FROM review_tasks t WHERE t.kind=? AND t.status='open'", (KIND,)):
            d = db.one("SELECT via,state FROM kids_decisions WHERE work_id=?", (t["entity_id"],))
            if not d or d["via"] != "auto_approved" or t["entity_id"] not in tagged:
                with db.tx() as conn:
                    conn.execute("UPDATE review_tasks SET status='ignored',resolved_at=?,resolution_json=? WHERE id=?",
                                 (db.now(), db.json_dump({"action": "superseded_by_manual_decision"}), t["id"]))
                closed += 1
        db.add_history("kids.recheck", f"Re-checked {len(rows)} auto-approved kids book(s): {passes} pass, {fails} need review ({queued} newly queued) (by {by or 'admin'})",
                       data={"checked": len(rows), "pass": passes, "fail": fails, "queued": queued, "by": by})
    return {"dry_run": dry_run, "checked": passes + fails, "pass": passes, "fail": fails, "queued": queued, "refreshed": refreshed, "closed": closed,
            "by_blocker": by_blocker, "examples": failing, "group": GROUP_LABEL,
            "with_audience_metadata": with_meta, "audience_coverage": audience.coverage(),
            "open_in_inbox": db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind=? AND status='open'", (KIND,))["n"]}


def resolve_task(task_id: str, action: str, *, by_user_id: str | None, by_name: str | None) -> dict[str, Any]:
    """Keep (manual_tag, final) or remove (manual_untag, final) one re-check book. Both go
    through the exact function the book page and the bulk tag tools use."""
    if action not in ("keep", "remove"):
        raise ValueError("action must be keep or remove")
    task = db.one("SELECT * FROM review_tasks WHERE id=? AND kind=?", (task_id, KIND))
    if not task:
        raise ValueError("Re-check item not found")
    if task["status"] != "open":
        raise ValueError(f"already {task['status']}")
    work_id = task["entity_id"]
    d = db.one("SELECT via FROM kids_decisions WHERE work_id=?", (work_id,))
    if not d or d["via"] != "auto_approved":
        with db.tx() as conn:
            conn.execute("UPDATE review_tasks SET status='ignored',resolved_at=?,resolution_json=? WHERE id=?",
                         (db.now(), db.json_dump({"action": "superseded_by_manual_decision"}), task_id))
        raise ValueError("a person has already decided this book")
    tagging.set_kids_tag_manually(work_id, action == "keep", by_user_id=by_user_id, by_name=by_name)
    with db.tx() as conn:
        conn.execute("UPDATE review_tasks SET status='resolved',resolved_at=?,resolution_json=? WHERE id=?",
                     (db.now(), db.json_dump({"action": "kept" if action == "keep" else "removed", "by": by_name or by_user_id}), task_id))
    return {"ok": True, "work_id": work_id, "tagged": action == "keep"}
