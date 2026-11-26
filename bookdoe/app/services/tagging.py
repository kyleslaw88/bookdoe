from __future__ import annotations

import logging
import time
from typing import Any

from .. import db
from ..config import settings
from . import audience, kidsgate, llmclient

# Patchable clock for the batch budget (tests drive it; never a real sleep).
_monotonic = time.monotonic

log = logging.getLogger(__name__)

KIDS_TAG_NAME = "kids"
# The one user-facing name for the kids system tag, in Management and the Player alike.
# The stored name stays 'kids' (the access boundary and the classifier key on it).
KIDS_TAG_LABEL = "Kids shelf"
RESERVED_TAG_LABELS = {KIDS_TAG_NAME, KIDS_TAG_LABEL.lower()}


def tag_label(name: str) -> str:
    return KIDS_TAG_LABEL if name == KIDS_TAG_NAME else name


def _labelled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        r["label"] = tag_label(r["name"])
    return rows

# A kids verdict below this confidence is recorded but NOT put in front of a person:
# the tag is an access-control gate, not a taste signal, and a queue full of
# low-confidence "maybe"s invites rubber-stamping. Above it the verdict becomes a
# review task - never a tag. Only an admin approving that task writes the tag.
KIDS_PROPOSE_THRESHOLD = 0.75
KIDS_REVIEW_KIND = "kids_tag"

# Auto-approval (the owner, 2026-10-11): a confident proposal is applied at once instead of
# waiting in the Inbox. ON by default. The threshold can be raised, never lowered below
# the proposal threshold: whatever is auto-approved would have been proposed anyway.
# 2026-10-12: the default threshold is 0.95 and "confident" is no longer enough on its
# own - kidsgate.evaluate_gate (strong audience signal, no hard adult signal, a real
# description) must also pass; a kids verdict that fails it becomes an Inbox proposal.
AUTO_APPROVE_KEY = "tagging.kids_auto_approve"
AUTO_THRESHOLD_KEY = "tagging.kids_auto_threshold"
AUTO_THRESHOLD_DEFAULT = 0.95
REQUIRE_STRONG_KEY = "tagging.kids_require_strong_evidence"
AUTO_ACTOR = "classifier"
RECHECK_REVIEW_KIND = "kids_recheck"


def auto_approve_enabled() -> bool:
    return bool(db.get_setting(AUTO_APPROVE_KEY, True))


def auto_threshold() -> float:
    try:
        value = float(db.get_setting(AUTO_THRESHOLD_KEY, AUTO_THRESHOLD_DEFAULT))
    except (TypeError, ValueError):
        value = AUTO_THRESHOLD_DEFAULT
    return max(KIDS_PROPOSE_THRESHOLD, min(1.0, value))


def require_strong_evidence() -> bool:
    """Auto-approval needs a strong audience signal from the metadata. ON by default."""
    return bool(db.get_setting(REQUIRE_STRONG_KEY, True))


def set_auto_approve(enabled: bool | None = None, threshold: float | None = None, require_strong: bool | None = None) -> dict[str, Any]:
    if threshold is not None:
        if not 0.0 <= float(threshold) <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if float(threshold) < KIDS_PROPOSE_THRESHOLD:
            raise ValueError(f"threshold cannot be below the proposal threshold ({KIDS_PROPOSE_THRESHOLD})")
        db.set_setting(AUTO_THRESHOLD_KEY, float(threshold))
    if enabled is not None:
        db.set_setting(AUTO_APPROVE_KEY, bool(enabled))
    if require_strong is not None:
        db.set_setting(REQUIRE_STRONG_KEY, bool(require_strong))
    return {"kids_auto_approve": auto_approve_enabled(), "kids_auto_threshold": auto_threshold(), "propose_threshold": KIDS_PROPOSE_THRESHOLD,
            "kids_require_strong_evidence": require_strong_evidence()}


KIDS_SYSTEM_PROMPT = kidsgate.KIDS_SYSTEM_PROMPT


def kids_tag_id() -> str:
    row = db.one("SELECT id FROM tags WHERE name=?", (KIDS_TAG_NAME,))
    if not row:
        # init_db() seeds this at startup; only missing if called before that ran.
        raise RuntimeError("system 'kids' tag is missing - has db.init_db() run?")
    return row["id"]


def list_tags(viewer: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if viewer and viewer.get("access_scope") == "kids":
        # A kids account may only learn tag names that are attached to works it can
        # see (the same predicate as the library), with counts over those works only.
        clause, params = kids_visibility(viewer)
        return _labelled(db.all(
            f"""SELECT t.*, COUNT(wt.work_id) work_count FROM tags t
               JOIN work_tags wt ON wt.tag_id=t.id JOIN works w ON w.id=wt.work_id
               WHERE {clause}
               GROUP BY t.id ORDER BY t.kind DESC, t.name COLLATE NOCASE""",  # nosec B608
            params,
        ))
    return _labelled(db.all(
        """SELECT t.*, COUNT(wt.work_id) work_count FROM tags t
           LEFT JOIN work_tags wt ON wt.tag_id=t.id
           GROUP BY t.id ORDER BY t.kind DESC, t.name COLLATE NOCASE"""
    ))


def create_tag(name: str, color: str | None = None) -> str:
    name = name.strip()
    if not name:
        raise ValueError("Tag name cannot be empty")
    existing = db.one("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (name,))
    if name.lower() == KIDS_TAG_LABEL.lower():
        # The display label of the system tag: a second tag by that name would look
        # identical in every picker while granting nothing.
        raise ValueError(f"'{KIDS_TAG_LABEL}' is the name of a protected tag and cannot be used for a new one")
    if existing:
        return existing["id"]
    tid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO tags(id,name,kind,color,created_at) VALUES (?,?,'manual',?,?)",
            (tid, name, color, db.now()),
        )
    return tid


def delete_tag(tag_id: str) -> None:
    row = db.one("SELECT * FROM tags WHERE id=?", (tag_id,))
    if not row:
        raise ValueError("Tag not found")
    if row["kind"] == "system":
        # This is the tag a kids account's entire access boundary is built on.
        # Deleting it would be silent and irreversible for every account depending on it.
        raise ValueError(f"'{row['name']}' is a system tag and cannot be deleted")
    with db.tx() as conn:
        conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))


def work_tags(work_id: str) -> list[dict[str, Any]]:
    return _labelled(db.all(
        """SELECT t.id,t.name,t.kind,t.color,wt.source,wt.confidence FROM work_tags wt
           JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? ORDER BY t.kind DESC,t.name COLLATE NOCASE""",
        (work_id,),
    ))


def add_tag(work_id: str, tag_id: str, *, source: str = "manual", confidence: float | None = None) -> None:
    if not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        raise ValueError("Work not found")
    if not db.one("SELECT id FROM tags WHERE id=?", (tag_id,)):
        raise ValueError("Tag not found")
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO work_tags(work_id,tag_id,source,confidence,created_at) VALUES (?,?,?,?,?)
               ON CONFLICT(work_id,tag_id) DO UPDATE SET source=excluded.source,confidence=excluded.confidence""",
            (work_id, tag_id, source, confidence, db.now()),
        )


def remove_tag(work_id: str, tag_id: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM work_tags WHERE work_id=? AND tag_id=?", (work_id, tag_id))


# ---------- manual override (kids_decisions) ----------
# A human's decision about the kids shelf is final until a human changes it. The
# classifier consults this on every path (single work, batch, scheduled job, force)
# and stays away from any work that has a row; proposals already open are closed by
# a manual untag so nobody can approve a stale one later.

def kids_decision(work_id: str) -> dict[str, Any] | None:
    row = db.one("SELECT d.*,u.username decided_by_name FROM kids_decisions d LEFT JOIN users u ON u.id=d.decided_by WHERE d.work_id=?", (work_id,))
    return row


def set_kids_decision(work_id: str, state: str, *, via: str, by: str | None, note: str | None = None) -> None:
    if state not in ("on", "off"):
        raise ValueError("state must be 'on' or 'off'")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO kids_decisions(work_id,state,via,decided_by,decided_at,note) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(work_id) DO UPDATE SET state=excluded.state,via=excluded.via,decided_by=excluded.decided_by,decided_at=excluded.decided_at,note=excluded.note",
            (work_id, state, via, by, db.now(), note),
        )
        if state == "off":
            # Close any open proposal so a stale suggestion cannot re-add the tag later.
            conn.execute("UPDATE review_tasks SET status='ignored',resolved_at=?,resolution_json=? WHERE kind IN (?,?) AND entity_type='work' AND entity_id=? AND status='open'",
                         (db.now(), db.json_dump({"action": "superseded_by_manual_decision", "by": by}), KIDS_REVIEW_KIND, RECHECK_REVIEW_KIND, work_id))


def set_kids_tag_manually(work_id: str, on: bool, *, by_user_id: str | None, by_name: str | None) -> None:
    """An administrator adding or removing the kids tag by hand: the tag changes AND a
    decision is recorded, so the classifier leaves the work alone from now on."""
    tid = kids_tag_id()
    if on:
        add_tag(work_id, tid, source="manual")
    else:
        remove_tag(work_id, tid)
    set_kids_decision(work_id, "on" if on else "off", via="manual_tag" if on else "manual_untag", by=by_user_id)
    if on:
        # A person's yes supersedes any open suggestion or recheck for this work, so
        # nobody approves a stale one later (an 'off' already closes them in set_kids_decision).
        with db.tx() as conn:
            conn.execute("UPDATE review_tasks SET status='resolved',resolved_at=?,resolution_json=? WHERE kind IN (?,?) AND entity_type='work' AND entity_id=? AND status='open'",
                         (db.now(), db.json_dump({"action": "superseded_by_manual_decision", "by": by_user_id}), KIDS_REVIEW_KIND, "kids_recheck", work_id))
    db.add_history("kids.manual_on" if on else "kids.manual_off",
                   f"Kids shelf set {'on' if on else 'off'} by hand ({by_name or by_user_id or 'admin'})", work_id=work_id,
                   data={"by": by_user_id})


BULK_TAG_MAX = 500


def remove_tag_from_works(tag_id: str, work_ids: list[str], *, by_user_id: str | None, by_name: str | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Remove one tag from many works, one result per work. The kids tag takes the same
    path as removing it from a single book (set_kids_tag_manually), so every removal is a
    recorded manual_untag the classifier will never undo. Even a work that was not on the
    shelf is locked off, which is what 'not for kids' means to the person asking."""
    tag = db.one("SELECT * FROM tags WHERE id=?", (tag_id,))
    if not tag:
        raise ValueError("Tag not found")
    is_kids = tag["name"] == KIDS_TAG_NAME
    results, removed = [], 0
    for wid in dict.fromkeys(work_ids):
        if not db.one("SELECT id FROM works WHERE id=?", (wid,)):
            results.append({"work_id": wid, "status": "not_found"})
            continue
        had = db.one("SELECT 1 FROM work_tags WHERE work_id=? AND tag_id=?", (wid, tag_id)) is not None
        if is_kids:
            set_kids_tag_manually(wid, False, by_user_id=by_user_id, by_name=by_name)
        else:
            remove_tag(wid, tag_id)
        removed += had
        results.append({"work_id": wid, "status": "removed" if had else "not_tagged"})
    if removed:
        db.add_history("tag.bulk_removed", f"Removed '{tag_label(tag['name'])}' from {removed} book(s) by {by_name or by_user_id or 'admin'}",
                       data={"tag_id": tag_id, "work_ids": [r["work_id"] for r in results if r["status"] == "removed"], "by": by_user_id,
                             "count": removed, "requested": len(results), **({"context": context} if context else {})})
    return {"tag_id": tag_id, "removed": removed, "results": results}


def add_tag_to_works(tag_id: str, work_ids: list[str], *, by_user_id: str | None, by_name: str | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add one tag to many works, one result per work (applied / already / skipped /
    failed). The kids tag takes the single-book path (set_kids_tag_manually), so each work
    gets a recorded manual_tag the classifier never overrides; it is never a plain insert."""
    tag = db.one("SELECT * FROM tags WHERE id=?", (tag_id,))
    if not tag:
        raise ValueError("Tag not found")
    is_kids = tag["name"] == KIDS_TAG_NAME
    results = []
    for wid in dict.fromkeys(work_ids):
        if not db.one("SELECT id FROM works WHERE id=?", (wid,)):
            results.append({"work_id": wid, "status": "skipped", "reason": "Book not found"})
            continue
        try:
            has = db.one("SELECT 1 FROM work_tags WHERE work_id=? AND tag_id=?", (wid, tag_id)) is not None
            if has:
                # Already on the shelf: report it, but make sure a person's decision exists
                # so the classifier stays away (an existing decision is left as it is).
                if is_kids and not kids_decision(wid):
                    set_kids_tag_manually(wid, True, by_user_id=by_user_id, by_name=by_name)
                results.append({"work_id": wid, "status": "already"})
                continue
            if is_kids:
                set_kids_tag_manually(wid, True, by_user_id=by_user_id, by_name=by_name)
            else:
                add_tag(wid, tag_id, source="manual")
            results.append({"work_id": wid, "status": "applied"})
        except Exception as exc:  # one bad book must not sink the batch
            results.append({"work_id": wid, "status": "failed", "reason": str(exc) or exc.__class__.__name__})
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("applied", "already", "skipped", "failed")}
    if counts["applied"]:
        db.add_history("tag.bulk_added", f"Added '{tag_label(tag['name'])}' to {counts['applied']} book(s) by {by_name or by_user_id or 'admin'}",
                       data={"tag_id": tag_id, "count": counts["applied"], "requested": len(results), "by": by_user_id,
                             "work_ids": [r["work_id"] for r in results if r["status"] == "applied"],
                             **({"context": context} if context else {})})
    return {"tag_id": tag_id, **counts, "results": results}


def kids_visibility(user: dict[str, Any] | None) -> tuple[str, tuple[Any, ...]]:
    """(SQL boolean expression, bound parameters) to AND into a query that aliases works
    as `w`. The caller splices the expression and places the parameters where the
    expression sits among its own; nothing about the viewer is ever interpolated.

    ('1=1', ()) for an admin or adult. For a kid, a work is visible when it carries
    the `kids` system tag (the household-wide shelf), when this kid holds a book-level
    grant on it, or when the kid holds a grant on a tag the work carries (an approved
    collection). Anything else - unclassified, unapproved - is invisible: default
    deny. 'kids' is a fixed constant, never user input.
    """
    if not user or user.get("access_scope") != "kids":
        return "1=1", ()
    uid = user.get("id") or ""
    clause = (
        "(EXISTS (SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id "  # nosec B608
        f"WHERE wt.work_id=w.id AND t.name='{KIDS_TAG_NAME}') "
        "OR EXISTS (SELECT 1 FROM content_grants cg WHERE cg.work_id=w.id AND cg.user_id=?) "
        "OR EXISTS (SELECT 1 FROM work_tags wt2 JOIN tag_grants tg ON tg.tag_id=wt2.tag_id "
        "WHERE wt2.work_id=w.id AND tg.user_id=?))"
    )
    return clause, (uid, uid)


def is_visible_to(work_id: str, user: dict[str, Any] | None) -> bool:
    if not user or user.get("access_scope") != "kids":
        return True
    clause, params = kids_visibility(user)
    return db.one(f"SELECT 1 FROM works w WHERE w.id=? AND {clause}", (work_id, *params)) is not None  # nosec B608


def _authors(work_id: str) -> list[str]:
    return [a["name"] for a in db.all(
        """SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id
           WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord""",
        (work_id,),
    )]


def _open_kids_proposal(work_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT id FROM review_tasks WHERE kind=? AND entity_type='work' AND entity_id=? AND status='open'",
        (KIDS_REVIEW_KIND, work_id),
    )


def _parse_verdict(raw: Any) -> dict[str, Any] | None:
    """The LLM's answer, strictly (kidsgate.parse_answer): None for anything that is not a
    well-formed verdict. The only safe reading of an unexpected shape is "no answer",
    never "kids"."""
    return kidsgate.parse_answer(raw)


def _gate_for(ev: dict[str, Any], verdict: str | None, confidence: float | None, llm_adult: list[str] | None) -> dict[str, Any]:
    return kidsgate.evaluate_gate(ev, verdict=verdict, confidence=confidence, threshold=auto_threshold(),
                                  require_strong=require_strong_evidence(), llm_adult_signals=llm_adult)


def _not_auto_reason(gate: dict[str, Any]) -> str | None:
    """Why a proposal was not applied at once - the sentence the Inbox shows."""
    if not auto_approve_enabled():
        return "Auto-approval is switched off."
    if gate["passed"]:
        return None
    return "Not auto-approved: " + "; ".join(gate["reasons"]) + "."


def _gate_payload(gate: dict[str, Any]) -> dict[str, Any]:
    return {k: gate[k] for k in ("passed", "blockers", "reasons", "strong_signals", "adult_signals", "threshold", "require_strong")}


def _store_classification(work_id: str, verdict: str, confidence: float | None, reason: str | None, detail: dict[str, Any]) -> None:
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO kids_classification(work_id,verdict,confidence,reason,checked_at,detail_json) VALUES (?,?,?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET verdict=excluded.verdict,confidence=excluded.confidence,
                 reason=excluded.reason,checked_at=excluded.checked_at,detail_json=excluded.detail_json""",
            (work_id, verdict, confidence, reason, db.now(), db.json_dump(detail)),
        )


def classify_kids_for_work(work_id: str, *, force: bool = False) -> dict[str, Any]:
    """Ask the LLM whether a single work belongs on the kids shelf, and PROPOSE it.

    This writes work_tags only through the deterministic gate (kidsgate.evaluate_gate):
    a "kids" verdict at or above the auto threshold that ALSO has a strong audience signal
    in the metadata, no hard adult signal and a real description is auto-approved (when
    that setting is on); every other confident "kids" verdict opens a `kids_tag` review
    task (the Inbox's "Kids shelf suggestions") that records why it was not applied.
    Hard adult signals decide not_kids in code, without asking the model. Wrongly putting
    a book on a child's shelf is worse than leaving it off, so a person stays in the loop.

    Skips, cheapest first: a work a person decided about, one already tagged kids (nothing
    to decide, no LLM call), one with an open proposal, one already checked (verdict
    remembered, unless force). A disabled/unreachable LLM or a malformed answer abstains:
    no kids_classification row is written, so it is retried once the LLM is back.
    """
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not work:
        raise ValueError("Work not found")
    if db.one("SELECT 1 FROM kids_decisions WHERE work_id=?", (work_id,)):
        # A person decided; force does not override a human. Nothing is asked, written
        # or proposed.
        return {"work_id": work_id, "status": "manually_decided"}
    if db.one("SELECT 1 FROM work_tags WHERE work_id=? AND tag_id=?", (work_id, kids_tag_id())):
        return {"work_id": work_id, "status": "already_tagged"}
    if not force and _open_kids_proposal(work_id):
        return {"work_id": work_id, "status": "already_proposed"}
    if not force and db.one("SELECT 1 FROM kids_classification WHERE work_id=?", (work_id,)):
        return {"work_id": work_id, "status": "already_checked"}

    # Text metadata already on record only - never file contents or cover images.
    ev = kidsgate.gather_evidence(work_id)
    authors = ev["authors"]
    ehash = kidsgate.evidence_hash(ev)
    hard = kidsgate.adult_signals(ev)
    if hard:
        reason = "Hard adult signal: " + "; ".join(f"{kidsgate.HARD_ADULT_SIGNALS[x['id']]} ({x['detail']})" for x in hard[:3])
        _store_classification(work_id, "not_kids", None, reason[:500],
                              {"source": "deterministic", "evidence_hash": ehash, "adult_signals": [f"{x['id']}: {x['detail']}" for x in hard], "audience_evidence": []})
        existing = _open_kids_proposal(work_id)
        if existing:
            with db.tx() as conn:
                conn.execute("UPDATE review_tasks SET status='ignored',resolved_at=?,resolution_json=? WHERE id=?",
                             (db.now(), db.json_dump({"action": "superseded_by_hard_adult_signal"}), existing["id"]))
        return {"work_id": work_id, "status": "classified", "verdict": "not_kids", "confidence": None, "proposed": False,
                "auto_approved": False, "decided_by": "adult_signals"}

    verdict = _parse_verdict(llmclient.chat_json(kidsgate.KIDS_SYSTEM_PROMPT, kidsgate.render_prompt(ev), timeout=settings.classify_llm_timeout))
    if verdict is None:
        return {"work_id": work_id, "status": "abstained"}

    gate = _gate_for(ev, verdict["verdict"], verdict["confidence"], verdict["adult_signals"])
    model = llmclient.model_name()
    _store_classification(work_id, verdict["verdict"], verdict["confidence"], verdict["reason"],
                          {"source": "llm", "model": model, "evidence_hash": ehash, "audience_evidence": verdict["audience_evidence"],
                           "adult_signals": verdict["adult_signals"], "gate": _gate_payload(gate)})

    t = db.now()
    proposed = False
    if verdict["verdict"] == "kids" and verdict["confidence"] >= KIDS_PROPOSE_THRESHOLD:
        payload = db.json_dump({
            "verdict": verdict["verdict"], "confidence": verdict["confidence"], "reason": verdict["reason"],
            "source": {"author": ", ".join(authors) or None, "authors": authors,
                       "description": (work.get("description") or "")[:400] or None},
            "signal": "llm", "model": model, "audience_evidence": verdict["audience_evidence"], "llm_adult_signals": verdict["adult_signals"],
            "gate": _gate_payload(gate), "not_auto_approved": _not_auto_reason(gate), "evidence_hash": ehash,
        })
        existing = _open_kids_proposal(work_id)
        with db.tx() as conn:
            if existing:
                conn.execute("UPDATE review_tasks SET payload_json=?,title=? WHERE id=?", (payload, work["title"], existing["id"]))
            else:
                conn.execute(
                    """INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at)
                       VALUES (?,?,'work',?,?,?,'open',40,?)""",
                    (db.uid(), KIDS_REVIEW_KIND, work_id, work["title"], payload, t),
                )
        proposed = True
    auto = False
    if proposed and auto_approve_enabled() and gate["passed"]:
        task = _open_kids_proposal(work_id)
        if task:
            approve_kids_proposal(task["id"], decided_by=AUTO_ACTOR, decided_by_id=None, auto=True)
            auto = True
    return {"work_id": work_id, "status": "classified", "verdict": verdict["verdict"],
            "confidence": verdict["confidence"], "proposed": proposed, "auto_approved": auto,
            "gate_blockers": gate["blockers"] if proposed and not auto else []}


def classify_kids_missing(limit: int | None = None, *, budget_seconds: float | None = None) -> dict[str, Any]:
    """One batch of the library scan: owned works never put to the LLM, oldest first.

    Owned only - the kids shelf is about books a child can actually open, and every
    call is slow on a local reasoning model. Works already tagged kids or already
    proposed are excluded in SQL so they never cost a call. The batch stops starting
    new books once `budget_seconds` of wall clock have gone (the call in flight is
    allowed to finish); the rest wait for the next run. Defaults come from
    BOOKDOE_CLASSIFY_BATCH / _BUDGET_SECONDS."""
    if limit is None:
        limit = settings.classify_batch
    if budget_seconds is None:
        budget_seconds = settings.classify_budget_seconds
    if not llmclient.enabled():
        return {"skipped": True, "reason": "LLM not enabled (BOOKDOE_LLM_ENABLED)"}
    rows = db.all(
        """SELECT w.id FROM works w LEFT JOIN kids_classification kc ON kc.work_id=w.id
           WHERE kc.work_id IS NULL
             AND EXISTS (SELECT 1 FROM media_assets a WHERE a.work_id=w.id AND a.status='owned')
             AND NOT EXISTS (SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=w.id AND t.name=?)
             AND NOT EXISTS (SELECT 1 FROM review_tasks r WHERE r.kind=? AND r.entity_type='work' AND r.entity_id=w.id AND r.status='open')
             AND NOT EXISTS (SELECT 1 FROM kids_decisions d WHERE d.work_id=w.id)
           ORDER BY w.added_at LIMIT ?""",
        (KIDS_TAG_NAME, KIDS_REVIEW_KIND, limit),
    )
    swept = auto_approve_pending()
    proposed = errors = abstained = auto = 0
    started_at, budget_stopped = _monotonic(), 0
    for i, row in enumerate(rows):
        if budget_seconds and i and _monotonic() - started_at >= budget_seconds:
            budget_stopped = len(rows) - i
            break
        try:
            result = classify_kids_for_work(row["id"])
            if result.get("proposed"):
                proposed += 1
            if result.get("auto_approved"):
                auto += 1
            if result.get("status") == "abstained":
                abstained += 1
        except Exception as exc:
            errors += 1
            log.warning("kids classification failed for %s: %s", row["id"], exc)
    out = {"considered": len(rows) - budget_stopped, "proposed": proposed, "auto_approved": auto, "abstained": abstained, "errors": errors,
           "swept_pending": swept["approved"], "budget_stopped": budget_stopped, "elapsed_seconds": round(_monotonic() - started_at, 1),
           "remaining": kids_scan_pending()}
    log.info("kids classification: %s", out)
    return out


def auto_approve_pending(limit: int = 500) -> dict[str, Any]:
    """One-shot, idempotent sweep: approve proposals already waiting in the Inbox that
    pass the same gate a fresh verdict must (confidence at the auto threshold AND the
    deterministic evidence checks, evaluated on the metadata as it is now). Only OPEN
    tasks qualify, so a proposal a person rejected (status ignored) or already decided
    is never touched; a work with a manual 'off' decision is skipped and left in the
    Inbox. A proposal that fails has its recorded reason refreshed. No-op when
    auto-approval is off."""
    if not auto_approve_enabled():
        return {"enabled": False, "approved": 0, "skipped": 0}
    threshold = auto_threshold()
    rows = db.all("SELECT id,entity_id,payload_json FROM review_tasks WHERE kind=? AND entity_type='work' AND status='open' ORDER BY created_at LIMIT ?",
                  (KIDS_REVIEW_KIND, limit))
    approved = skipped = 0
    for r in rows:
        payload = db.json_load(r["payload_json"], {}) or {}
        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            skipped += 1
            continue
        try:
            gate = _gate_for(kidsgate.gather_evidence(r["entity_id"]), payload.get("verdict") or "kids", float(confidence), payload.get("llm_adult_signals"))
        except ValueError:  # the work is gone
            skipped += 1
            continue
        if not gate["passed"]:
            skipped += 1
            payload.update({"gate": _gate_payload(gate), "not_auto_approved": _not_auto_reason(gate)})
            with db.tx() as conn:
                conn.execute("UPDATE review_tasks SET payload_json=? WHERE id=?", (db.json_dump(payload), r["id"]))
            continue
        try:
            approve_kids_proposal(r["id"], decided_by=AUTO_ACTOR, decided_by_id=None, auto=True)
            approved += 1
        except ValueError:  # manual 'off' decision or already decided: leave it alone
            skipped += 1
    if approved:
        log.info("kids auto-approval sweep: approved %d pending proposal(s), skipped %d", approved, skipped)
    return {"enabled": True, "approved": approved, "skipped": skipped, "threshold": threshold}


def auto_approved_works(*, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
    total = db.one("SELECT COUNT(*) n FROM kids_decisions WHERE via='auto_approved'")["n"]
    rows = db.all("SELECT d.work_id,d.decided_at,d.note,w.title FROM kids_decisions d JOIN works w ON w.id=d.work_id WHERE d.via='auto_approved' ORDER BY d.decided_at DESC LIMIT ? OFFSET ?",
                  (limit, offset))
    for r in rows:
        meta = db.json_load(r.pop("note"), {}) or {}
        r["confidence"], r["model"] = meta.get("confidence"), meta.get("model")
    return {"items": rows, "total": total}


def revert_auto_approved(work_ids: list[str] | None, *, mode: str, by_user_id: str | None, by_name: str | None) -> dict[str, Any]:
    """Bulk undo of auto-approvals. mode 'lock_off' removes the tag and records a manual
    'off' decision (the classifier never touches the work again); 'reevaluate' removes
    the tag, the decision and the remembered verdict, so the next scan asks the model
    again (and may auto-approve again if the setting is on). Only works whose decision
    is 'auto_approved' are affected: a manual decision is never reverted here."""
    if mode not in ("lock_off", "reevaluate"):
        raise ValueError("mode must be 'lock_off' or 'reevaluate'")
    where, params = "via='auto_approved'", []
    if work_ids is not None:
        if not work_ids:
            return {"reverted": 0, "mode": mode}
        where += f" AND work_id IN ({','.join('?' * len(work_ids))})"; params = list(work_ids)
    targets = [r["work_id"] for r in db.all(f"SELECT work_id FROM kids_decisions WHERE {where}", tuple(params))]  # nosec B608
    tid = kids_tag_id()
    for wid in targets:
        remove_tag(wid, tid)
        if mode == "lock_off":
            set_kids_decision(wid, "off", via="manual_untag", by=by_user_id, note="reverted auto-approval")
        else:
            with db.tx() as conn:
                conn.execute("DELETE FROM kids_decisions WHERE work_id=?", (wid,))
                conn.execute("DELETE FROM kids_classification WHERE work_id=?", (wid,))
    if targets:
        db.add_history("kids.auto_reverted", f"Reverted {len(targets)} auto-approved kids tag(s) ({mode}) by {by_name or by_user_id or 'admin'}",
                       data={"mode": mode, "work_ids": targets, "by": by_user_id})
    return {"reverted": len(targets), "mode": mode, "work_ids": targets}


def kids_scan_pending() -> int:
    """Owned, untagged works the classifier has never answered for (abstentions included)."""
    return db.one(
        """SELECT COUNT(*) n FROM works w LEFT JOIN kids_classification kc ON kc.work_id=w.id
           WHERE kc.work_id IS NULL AND EXISTS (SELECT 1 FROM media_assets a WHERE a.work_id=w.id AND a.status='owned')
             AND NOT EXISTS (SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=w.id AND t.name=?)
             AND NOT EXISTS (SELECT 1 FROM kids_decisions d WHERE d.work_id=w.id)""",
        (KIDS_TAG_NAME,),
    )["n"]


def _kids_proposal(task_id: str) -> dict[str, Any]:
    task = db.one("SELECT * FROM review_tasks WHERE id=? AND kind=?", (task_id, KIDS_REVIEW_KIND))
    if not task:
        raise ValueError("Kids shelf suggestion not found")
    if task["status"] != "open":
        raise ValueError("Kids shelf suggestion already decided")
    return task


def approve_kids_proposal(task_id: str, *, decided_by: str | None, decided_by_id: str | None = None, auto: bool = False) -> dict[str, Any]:
    """The one and only path from a suggestion to the tag, for a person's click and
    for auto-approval alike (`auto=True`: via 'auto_approved', the model and confidence
    kept on the decision, its own history event). A manual 'off' decision always
    wins: approving against it is refused, by hand or automatically."""
    task = _kids_proposal(task_id)
    decision = db.one("SELECT state FROM kids_decisions WHERE work_id=?", (task["entity_id"],))
    if decision and decision["state"] == "off":
        raise ValueError("This book was removed from the kids shelf by hand; tag it by hand if that has changed")
    payload = db.json_load(task["payload_json"], {}) or {}
    add_tag(task["entity_id"], kids_tag_id(), source="llm", confidence=payload.get("confidence"))
    resolution = {"action": "auto_approved" if auto else "approved", "by": decided_by, "confidence": payload.get("confidence"), "model": payload.get("model")}
    with db.tx() as conn:
        conn.execute(
            "UPDATE review_tasks SET status='resolved',resolved_at=?,resolution_json=? WHERE id=?",
            (db.now(), db.json_dump(resolution), task_id),
        )
    if auto:
        set_kids_decision(task["entity_id"], "on", via="auto_approved", by=None,
                          note=db.json_dump({"confidence": payload.get("confidence"), "model": payload.get("model"), "threshold": auto_threshold(),
                                             "strong_signals": [x["id"] for x in (payload.get("gate") or {}).get("strong_signals", [])]}))
        db.add_history("kids.auto_approved", f"Auto-approved for the kids shelf: {task['title']} (confidence {payload.get('confidence')}, {payload.get('model') or 'model unknown'})",
                       work_id=task["entity_id"], data=resolution)
    else:
        set_kids_decision(task["entity_id"], "on", via="proposal_approved", by=decided_by_id, note=f"classifier proposal approved by {decided_by}")
        db.add_history("kids.approved", f"Added to the kids shelf: {task['title']}", work_id=task["entity_id"], data=resolution)
    return {"ok": True, "work_id": task["entity_id"], "tagged": True, "auto": auto}


def reject_kids_proposal(task_id: str, *, decided_by: str | None, decided_by_id: str | None = None) -> dict[str, Any]:
    """Nothing about the work changes; the suggestion stops being offered. The LLM's
    verdict stays in kids_classification, so the scan does not ask about it again."""
    task = _kids_proposal(task_id)
    resolution = {"action": "rejected", "by": decided_by}
    with db.tx() as conn:
        conn.execute(
            "UPDATE review_tasks SET status='ignored',resolved_at=?,resolution_json=? WHERE id=?",
            (db.now(), db.json_dump(resolution), task_id),
        )
    set_kids_decision(task["entity_id"], "off", via="proposal_rejected", by=decided_by_id, note=f"classifier proposal rejected by {decided_by}")
    return {"ok": True, "work_id": task["entity_id"], "tagged": False}


def kids_explanation(work_id: str) -> dict[str, Any] | None:
    """Why a book is (or is not) on the shelf, for the admin's book page: what the model
    said and cited, the signals the gate sees in the metadata NOW, and - for an
    auto-approved book - whether it passes today's gate. None when the classifier has
    never looked at the work and nobody tagged it."""
    kc = db.one("SELECT * FROM kids_classification WHERE work_id=?", (work_id,))
    tagged = db.one("SELECT 1 FROM work_tags WHERE work_id=? AND tag_id=?", (work_id, kids_tag_id())) is not None
    if not kc and not tagged:
        return None
    detail = db.json_load((kc or {}).get("detail_json"), {}) or {}
    ev = kidsgate.gather_evidence(work_id)
    d = db.one("SELECT via,note FROM kids_decisions WHERE work_id=?", (work_id,))
    verdict, confidence = (kc["verdict"], kc["confidence"]) if kc else (None, None)
    if not kc and d and d["via"] == "auto_approved":  # the verdict row was pruned: the decision's own note still has the score
        verdict, confidence = "kids", (db.json_load(d["note"], {}) or {}).get("confidence")
    gate = _gate_for(ev, verdict, confidence, detail.get("adult_signals") if detail.get("source") == "llm" else None)
    return {
        "verdict": verdict, "confidence": confidence, "reason": kc["reason"] if kc else None,
        "model": detail.get("model"), "source": detail.get("source"), "checked_at": kc["checked_at"] if kc else None,
        "audience_evidence": detail.get("audience_evidence") or [], "llm_adult_signals": detail.get("adult_signals") or [],
        "strong_signals": gate["strong_signals"], "hard_adult_signals": gate["adult_signals"],
        "provider_conflict": [x["detail"] for x in gate.get("provider_conflict", [])],
        "audience_metadata": audience.signals_for_work(work_id),
        "gate": {"passed": gate["passed"], "reasons": gate["reasons"], "blockers": gate["blockers"], "threshold": gate["threshold"], "require_strong": gate["require_strong"]},
        "auto_approved": bool(d and d["via"] == "auto_approved"),
        "evidence_changed": bool(detail.get("evidence_hash")) and detail["evidence_hash"] != kidsgate.evidence_hash(ev),
        "has_cited_evidence": bool(detail),
    }
