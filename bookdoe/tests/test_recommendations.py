"""Recommendations: local heuristic by default, LLM when enabled - except for a
kids-scope viewer, who must never be shown an LLM's open-ended, unvetted suggestions.
"""
from app import db
from app.services import migration, recommendations, tagging, llmclient


def _work(title, author, description=""):
    wid = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
    if description:
        with db.tx() as conn:
            conn.execute("UPDATE works SET description=? WHERE id=?", (description, wid))
    return wid


def test_same_series_outranks_same_author(monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: False)
    target = _work("Book One", "Jane Author", "A tale of dragons and swords.")
    same_series = _work("Book Two", "Jane Author")
    same_author_only = _work("Unrelated Tale", "Jane Author")
    with db.tx() as conn:
        for wid in (target, same_series):
            row = conn.execute("SELECT id FROM series WHERE name='Trilogy'").fetchone()
            sid = row["id"] if row else db.uid()
            if not row:
                conn.execute("INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,NULL,'Trilogy','Trilogy',?,?)", (sid, db.now(), db.now()))
            conn.execute("INSERT OR IGNORE INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (wid, sid, "1"))

    result = recommendations.recommend_for_work(target)
    ids = [i["work_id"] for i in result["items"]]
    assert ids.index(same_series) < ids.index(same_author_only)
    assert result["algorithm"] == "heuristic"


def test_self_is_never_recommended():
    target = _work("Solo Book", "Someone")
    result = recommendations.recommend_for_work(target)
    assert target not in [i["work_id"] for i in result["items"]]


def test_unrelated_works_are_not_recommended():
    target = _work("Dragons and Magic", "Author A", "A tale of dragons and ancient magic in a far kingdom.")
    _work("Tax Law Basics", "Author B", "An introduction to corporate tax law and accounting.")
    result = recommendations.recommend_for_work(target)
    assert result["items"] == []


def test_llm_suggestions_are_matched_against_the_local_catalog(monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    target = _work("Dune", "Frank Herbert", "A desert planet, a prophecy, a spice.")
    owned = _work("Foundation", "Isaac Asimov")
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {
        "items": [
            {"title": "Foundation", "author": "Isaac Asimov", "reason": "epic scale sci-fi"},
            {"title": "Some Book Nobody Owns", "author": "Nobody Famous", "reason": "similar vibe"},
        ]
    })

    result = recommendations.recommend_for_work(target)
    assert result["algorithm"] == "llm"
    by_title = {i["title"]: i for i in result["items"]}
    assert by_title["Foundation"]["in_library"] is True
    assert by_title["Foundation"]["work_id"] == owned
    assert by_title["Some Book Nobody Owns"]["in_library"] is False
    assert by_title["Some Book Nobody Owns"]["work_id"] is None


def test_llm_failure_falls_back_to_heuristic(monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: None)
    target = _work("Book One", "Jane Author")
    _work("Book Two", "Jane Author")
    result = recommendations.recommend_for_work(target)
    assert result["algorithm"] == "heuristic"


def test_kids_viewer_never_gets_llm_recommendations(monkeypatch):
    """The critical safety property: even with the LLM fully enabled and eager to
    answer, a kids-scope viewer must be routed to the restricted local heuristic."""
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    called = []
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: called.append(1) or {"items": [{"title": "Whatever", "author": "X"}]})
    target = _work("Kids Book One", "Author A")
    tagging.add_tag(target, tagging.kids_tag_id())
    sibling = _work("Kids Book Two", "Author A")
    tagging.add_tag(sibling, tagging.kids_tag_id())
    non_kids_same_author = _work("Adult Book", "Author A")

    result = recommendations.recommend_for_work(target, viewer={"access_scope": "kids"})

    assert called == [], "the open-ended LLM must never be consulted for a kids viewer"
    assert result["algorithm"] == "heuristic"
    ids = [i["work_id"] for i in result["items"]]
    assert sibling in ids
    assert non_kids_same_author not in ids, "a non-kids-tagged book must never be suggested to a kids viewer"


def test_results_are_cached_and_reused_within_ttl(monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: False)
    calls = []
    from app.services import recommendations as rec_module
    original = rec_module._heuristic_candidates
    def spy(*a, **k):
        calls.append(1)
        return original(*a, **k)
    monkeypatch.setattr(rec_module, "_heuristic_candidates", spy)

    target = _work("Book One", "Jane Author")
    recommendations.recommend_for_work(target)
    recommendations.recommend_for_work(target)
    assert len(calls) == 1, "a cached result must not recompute"

    recommendations.recommend_for_work(target, force=True)
    assert len(calls) == 2


def test_kids_viewer_bypasses_a_cached_llm_result(monkeypatch):
    """A cache entry written for a full-access viewer's LLM answer must never be
    served back to a kids-scope viewer looking at the same work."""
    target = _work("Book One", "Jane Author")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO recommendation_cache(work_id,algorithm,items_json,generated_at) VALUES (?,?,?,?)",
            (target, "llm", db.json_dump([{"title": "Something Unvetted", "work_id": None, "in_library": False}]), db.now()),
        )
    result = recommendations.recommend_for_work(target, viewer={"access_scope": "kids"})
    assert result["algorithm"] == "heuristic"


def test_missing_work_raises():
    import pytest
    with pytest.raises(ValueError):
        recommendations.recommend_for_work("does-not-exist")
