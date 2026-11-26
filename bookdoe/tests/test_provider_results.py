"""F-08: provider result states and persisted retry (spec Part XXXIX)."""
import httpx
import pytest

from app import db
from app.services import metadata, provider_results as pr
from app.services import search_providers as sp


REAL_CLIENT = httpx.Client


def _mock_client(handler):
    real_client = REAL_CLIENT

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _state(provider, entity_type, entity_id):
    return db.one("SELECT * FROM provider_retry_state WHERE provider=? AND entity_type=? AND entity_id=?",
                  (provider, entity_type, entity_id))


def _ol_ok(request):
    return httpx.Response(200, json={"docs": [{"key": "/works/OL1W", "title": "Dune"}]})


# -- classification --------------------------------------------------------------

@pytest.mark.parametrize("code,status", [
    (429, pr.RATE_LIMITED), (403, pr.RATE_LIMITED), (503, pr.TRANSIENT), (500, pr.TRANSIENT),
    (408, pr.TRANSIENT), (404, pr.NOT_FOUND), (400, pr.PERMANENT),
])
def test_http_status_classification(code, status):
    assert pr.classify_status(code)[0] == status


def test_transport_error_is_transient():
    assert pr.classify_exception(httpx.ConnectTimeout("slow"), "x").status == pr.TRANSIENT


# -- search providers: the acceptance criterion ----------------------------------

def test_429_is_rate_limited_with_scheduled_retry_never_not_found(monkeypatch):
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(lambda r: httpx.Response(429, headers={"Retry-After": "900"})))
    out = sp.federated_search("dune", providers=["openlibrary"])
    outcome = out.outcomes["openlibrary"]
    assert outcome.status == pr.RATE_LIMITED
    assert outcome.status != pr.NOT_FOUND
    assert out.results == []
    row = _state("openlibrary", "search", "dune")
    assert row["status"] == "rate_limited" and row["attempts"] == 1
    assert row["next_retry_at"] >= db.now() + 800  # honours Retry-After
    assert outcome.retry_at == row["next_retry_at"]


def test_transient_failure_is_scheduled_not_not_found(monkeypatch):
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(lambda r: httpx.Response(503)))
    out = sp.federated_search("dune", providers=["googlebooks"])
    assert out.outcomes["googlebooks"].status == pr.TRANSIENT
    assert _state("googlebooks", "search", "dune")["next_retry_at"] > db.now()


def test_permanent_failure_is_recorded_without_retry(monkeypatch):
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(lambda r: httpx.Response(400)))
    out = sp.federated_search("dune", providers=["openlibrary"])
    assert out.outcomes["openlibrary"].status == pr.PERMANENT
    row = _state("openlibrary", "search", "dune")
    assert row["status"] == "permanent" and row["next_retry_at"] is None


def test_provider_with_pending_retry_is_deferred_not_called(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    monkeypatch.setattr(sp.httpx, "Client", _mock_client(handler))
    sp.federated_search("dune", providers=["openlibrary"])
    out = sp.federated_search("dune", providers=["openlibrary"])
    assert len(calls) == 1
    assert out.outcomes["openlibrary"].status == pr.DEFERRED
    assert out.outcomes["openlibrary"].retry_at == _state("openlibrary", "search", "dune")["next_retry_at"]
    assert _state("openlibrary", "search", "dune")["attempts"] == 1


def test_due_retry_is_attempted_again_and_success_clears_state(monkeypatch):
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(lambda r: httpx.Response(429)))
    sp.federated_search("dune", providers=["openlibrary"])
    with db.tx() as conn:
        conn.execute("UPDATE provider_retry_state SET next_retry_at=?", (db.now() - 1,))
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_ol_ok))
    out = sp.federated_search("dune", providers=["openlibrary"])
    assert out.outcomes["openlibrary"].status == pr.SUCCESS
    assert [r["title"] for r in out.results] == ["Dune"]
    assert _state("openlibrary", "search", "dune") is None


def test_attempts_back_off_on_repeated_rate_limits(monkeypatch):
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(lambda r: httpx.Response(429)))
    sp.federated_search("dune", providers=["openlibrary"])
    first = _state("openlibrary", "search", "dune")
    with db.tx() as conn:
        conn.execute("UPDATE provider_retry_state SET next_retry_at=?", (db.now() - 1,))
    sp.federated_search("dune", providers=["openlibrary"])
    second = _state("openlibrary", "search", "dune")
    assert second["attempts"] == 2
    assert second["next_retry_at"] - second["updated_at"] > first["next_retry_at"] - first["updated_at"]


def test_empty_answer_is_not_found_and_clears_retry_state(monkeypatch):
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(lambda r: httpx.Response(200, json={"docs": []})))
    out = sp.federated_search("zzzz", providers=["openlibrary"])
    assert out.outcomes["openlibrary"].status == pr.NOT_FOUND
    assert _state("openlibrary", "search", "zzzz") is None


def test_rate_limited_provider_does_not_hide_other_providers(monkeypatch):
    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(429)
        return httpx.Response(200, json={"items": [{"id": "g1", "volumeInfo": {"title": "Dune"}}]})

    monkeypatch.setattr(sp.httpx, "Client", _mock_client(handler))
    out = sp.federated_search("dune", providers=["openlibrary", "googlebooks"])
    assert [r["provider"] for r in out.results] == ["googlebooks"]
    assert out.outcomes["openlibrary"].status == pr.RATE_LIMITED
    assert out.outcomes["googlebooks"].status == pr.SUCCESS


# -- metadata refresh --------------------------------------------------------------

def _work(metadata_id=None, title="Dune"):
    wid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,sort_title,metadata_id,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                     (wid, title, title, metadata_id, db.now(), db.now()))
    return wid


def _raise_status(code):
    def fail(*args, **kwargs):
        request = httpx.Request("POST", "http://openreads/api/v2/resolve")
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))
    return fail


def test_refresh_rate_limited_keeps_data_and_schedules_retry(monkeypatch):
    wid = _work()
    monkeypatch.setattr(metadata, "resolve", _raise_status(429))
    result = metadata.refresh_work(wid)
    assert result["status"] == "rate_limited" and result["retry_at"] > db.now()
    assert _state("openreads", "work", wid)["status"] == "rate_limited"
    assert db.one("SELECT title,metadata_id FROM works WHERE id=?", (wid,)) == {"title": "Dune", "metadata_id": None}
    assert db.one("SELECT id FROM review_tasks WHERE entity_id=?", (wid,)) is None


def test_refresh_deferred_while_retry_pending_then_resumes(monkeypatch):
    wid = _work()
    monkeypatch.setattr(metadata, "resolve", _raise_status(503))
    assert metadata.refresh_work(wid)["status"] == "transient"

    def must_not_run(*a, **k):
        raise AssertionError("provider called before retry was due")

    monkeypatch.setattr(metadata, "resolve", must_not_run)
    assert metadata.refresh_work(wid)["status"] == "deferred"
    with db.tx() as conn:
        conn.execute("UPDATE provider_retry_state SET next_retry_at=?", (db.now() - 1,))
    monkeypatch.setattr(metadata, "resolve", lambda **k: {"match": None, "confidence": 0, "candidates": []})
    assert metadata.refresh_work(wid)["status"] == "unmatched"
    assert _state("openreads", "work", wid) is None


def test_refresh_permanent_failure_still_raises(monkeypatch):
    wid = _work()
    monkeypatch.setattr(metadata, "resolve", _raise_status(400))
    with pytest.raises(httpx.HTTPStatusError):
        metadata.refresh_work(wid)
