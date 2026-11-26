"""#635: the duplicate-group signature moved from SHA-1 to SHA-256. A group dismissed under the old signature stays
dismissed; a change in the members still brings it back."""
from __future__ import annotations

from app import db
from app.services import dupefinder


def _facts(title="Dune"):
    f = {"title": title, "provider_linked": False, "authors": {"a1": "Frank Herbert"}, "owned": 0, "owned_files": 0, "series": {}}
    return {"w1": dict(f), "w2": dict(f)}


def _store(monkeypatch, facts):
    monkeypatch.setattr(dupefinder, "recommend", lambda ids, facts, extras: {"canonical_work_id": "w1", "reason": "r"})
    info = {"kind": "x", "confidence": "high", "score": 90, "reasons": [], "shared_identifiers": []}
    return dupefinder._store(["w1", "w2"], info, facts, {}, "scan", None)


def _dismiss_with(gid, sig):
    with db.tx() as c:
        c.execute("UPDATE duplicate_groups SET dismissed=1,dismissed_at=1,dismissed_by='u',dismissed_signature=? WHERE id=?", (sig, gid))


def test_group_dismissed_under_the_sha1_signature_stays_dismissed(monkeypatch):
    facts = _facts()
    gid = _store(monkeypatch, facts)
    _dismiss_with(gid, dupefinder._signature(["w1", "w2"], facts, legacy=True))
    assert _store(monkeypatch, facts) == gid
    row = db.one("SELECT dismissed,dismissed_signature,signature FROM duplicate_groups WHERE id=?", (gid,))
    assert row["dismissed"] == 1
    assert row["dismissed_signature"] == row["signature"]  # migrated to the new form


def test_changed_members_bring_a_legacy_dismissed_group_back(monkeypatch):
    facts = _facts()
    gid = _store(monkeypatch, facts)
    _dismiss_with(gid, dupefinder._signature(["w1", "w2"], facts, legacy=True))
    _store(monkeypatch, _facts(title="Dune Messiah"))
    assert db.one("SELECT dismissed FROM duplicate_groups WHERE id=?", (gid,))["dismissed"] == 0
