"""Federated search providers (spec Part XVII.C): per-provider adapters and merging."""
import httpx
import pytest

from app import db
from app.services import search_providers as sp


def _mock_client(handler):
    """Swap app.services.search_providers.httpx.Client for one backed by MockTransport."""
    real_client = httpx.Client

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **{k: v for k, v in kwargs.items() if k not in ("base_url",)}, base_url=kwargs.get("base_url", ""))

    return factory


def _json_handler(payload, status=200):
    def handler(request):
        return httpx.Response(status, json=payload)

    return handler


# -- OpenLibraryProvider -------------------------------------------------------

def test_openlibrary_maps_docs_to_common_format(monkeypatch):
    payload = {
        "docs": [
            {
                "key": "/works/OL123W",
                "title": "The Great Book",
                "author_name": ["Jane Author"],
                "isbn": ["9780000000001"],
                "first_publish_year": 2001,
                "cover_i": 555,
            }
        ]
    }
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler(payload)))

    results = sp.OpenLibraryProvider().search("great book", 10)

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "The Great Book"
    assert r["authors"] == ["Jane Author"]
    assert r["provider"] == "openlibrary"
    assert {"namespace": "isbn", "value": "9780000000001"} in r["identifiers"]
    assert r["publication_date"] == "2001"
    assert r["cover_url"] == "https://covers.openlibrary.org/b/id/555-L.jpg"
    assert r["match_score"] == 0.5


def test_openlibrary_handles_missing_optional_fields(monkeypatch):
    payload = {"docs": [{"key": "/works/OL999W", "title": "Bare Bones"}]}
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler(payload)))

    results = sp.OpenLibraryProvider().search("bare bones", 10)

    assert results[0]["authors"] == []
    assert results[0]["cover_url"] is None
    assert results[0]["publication_date"] is None


# -- HardcoverProvider ----------------------------------------------------------
# hardcover.app blocks all requests with a 403 (Cloudflare) -- the provider is
# disabled via enabled() but its mapping logic is still exercised directly
# below in case the upstream becomes reachable again.

def test_hardcover_provider_is_disabled():
    assert sp.HardcoverProvider().enabled() is False


def test_hardcover_maps_results_to_common_format(monkeypatch):
    payload = {
        "results": [
            {
                "id": 42,
                "title": "Hardcover Title",
                "subtitle": "A Subtitle",
                "authors": [{"name": "Some Author"}],
                "series": "Some Series",
                "isbn": "9781111111111",
                "release_date": "2020-05-01",
                "cover_url": "https://example.invalid/cover.jpg",
            }
        ]
    }
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler(payload)))

    results = sp.HardcoverProvider().search("hardcover title", 10)

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Hardcover Title"
    assert r["subtitle"] == "A Subtitle"
    assert r["authors"] == ["Some Author"]
    assert r["series"] == ["Some Series"]
    assert {"namespace": "isbn", "value": "9781111111111"} in r["identifiers"]
    assert {"namespace": "hardcover", "value": "42"} in r["identifiers"]
    assert r["provider"] == "hardcover"


def test_hardcover_falls_back_to_data_key(monkeypatch):
    payload = {"data": [{"id": 1, "title": "Alt Shape"}]}
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler(payload)))

    results = sp.HardcoverProvider().search("alt shape", 10)

    assert results[0]["title"] == "Alt Shape"


# -- AudiobookDBProvider ---------------------------------------------------------
# api.audiobookdb.com returns 404 for all requests -- the provider is disabled
# via enabled() but its mapping logic is still exercised directly below in
# case the upstream becomes reachable again.

def test_audiobookdb_provider_is_disabled():
    assert sp.AudiobookDBProvider().enabled() is False


def test_audiobookdb_maps_jsonapi_style_results(monkeypatch):
    payload = {
        "data": [
            {
                "id": "abc123",
                "attributes": {
                    "title": "Audio Title",
                    "authors": [{"name": "Narrator Author"}],
                    "series": [{"name": "Audio Series"}],
                    "isbn": "9782222222222",
                    "release_date": "2019-01-01",
                    "cover_url": "https://example.invalid/audio.jpg",
                },
            }
        ]
    }
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler(payload)))

    results = sp.AudiobookDBProvider().search("audio title", 10)

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Audio Title"
    assert r["authors"] == ["Narrator Author"]
    assert r["series"] == ["Audio Series"]
    assert {"namespace": "isbn", "value": "9782222222222"} in r["identifiers"]
    assert {"namespace": "audiobookdb", "value": "abc123"} in r["identifiers"]
    assert r["provider"] == "audiobookdb"


def test_audiobookdb_sends_api_key_header_when_configured(monkeypatch):
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(sp.httpx, "Client", _mock_client(handler))
    provider = sp.AudiobookDBProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "secret-key-value")

    provider.search("anything", 10)

    assert captured["auth"] == "secret-key-value"


def test_audiobookdb_omits_api_key_header_when_unset(monkeypatch):
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(sp.httpx, "Client", _mock_client(handler))
    provider = sp.AudiobookDBProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "")

    provider.search("anything", 10)

    assert captured["auth"] is None


# -- GoogleBooksProvider ----------------------------------------------------------

def test_googlebooks_maps_volumes_to_common_format(monkeypatch):
    payload = {
        "items": [
            {
                "id": "vol123",
                "volumeInfo": {
                    "title": "Google Title",
                    "subtitle": "Google Subtitle",
                    "authors": ["Google Author"],
                    "publishedDate": "2015-03-01",
                    "industryIdentifiers": [
                        {"type": "ISBN_13", "identifier": "9783333333333"},
                        {"type": "ISBN_10", "identifier": "3333333333"},
                    ],
                    "imageLinks": {"thumbnail": "http://example.invalid/thumb.jpg"},
                },
            }
        ]
    }
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler(payload)))

    results = sp.GoogleBooksProvider().search("google title", 10)

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Google Title"
    assert r["subtitle"] == "Google Subtitle"
    assert r["authors"] == ["Google Author"]
    assert {"namespace": "isbn", "value": "9783333333333"} in r["identifiers"]
    assert {"namespace": "isbn10", "value": "3333333333"} in r["identifiers"]
    assert r["cover_url"] == "https://example.invalid/thumb.jpg", "http image links must be upgraded to https"
    assert r["provider"] == "googlebooks"


def test_googlebooks_handles_empty_items(monkeypatch):
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler({})))

    assert sp.GoogleBooksProvider().search("nothing", 10) == []


# -- LocalMetadataProvider --------------------------------------------------------

def _make_work(title, author=None):
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'resolved',?,?)",
            (wid, title, title, t, t),
        )
        if author:
            cid = db.contributor_id_for_name(conn, author)
            conn.execute("INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?, 'author',0)", (wid, cid))
    return wid


def test_local_provider_wraps_search_local_results():
    _make_work("Local Only Book", author="Local Author")

    results = sp.LocalMetadataProvider().search("Local Only Book", 10)

    assert len(results) == 1
    assert results[0]["title"] == "Local Only Book"
    assert results[0]["authors"] == ["Local Author"]
    assert results[0]["provider"] == "local"


def test_local_provider_empty_query_returns_no_results():
    assert sp.LocalMetadataProvider().search("", 10) == []


# -- get_provider ------------------------------------------------------------------

def test_get_provider_returns_named_instance():
    assert isinstance(sp.get_provider("openlibrary"), sp.OpenLibraryProvider)
    assert isinstance(sp.get_provider("hardcover"), sp.HardcoverProvider)
    assert isinstance(sp.get_provider("audiobookdb"), sp.AudiobookDBProvider)
    assert isinstance(sp.get_provider("googlebooks"), sp.GoogleBooksProvider)
    assert isinstance(sp.get_provider("local"), sp.LocalMetadataProvider)


def test_get_provider_rejects_unknown_name():
    with pytest.raises(ValueError):
        sp.get_provider("not-a-real-provider")


# -- search_federated: merging, errors, configuration -----------------------------

def _stub_provider(name, results, *, raise_error=False, enabled=True):
    class Stub(sp.SearchProvider):
        def __init__(self):
            self.name = name
            self.calls = []

        def enabled(self):
            return enabled

        def search(self, query, limit):
            self.calls.append((query, limit))
            if raise_error:
                raise RuntimeError("simulated provider failure")
            return results

    return Stub()


def test_search_federated_merges_results_from_multiple_providers(monkeypatch):
    p1 = _stub_provider("p1", [sp._result("p1", title="Alpha Book", match_score=0.5)])
    p2 = _stub_provider("p2", [sp._result("p2", title="Beta Book", match_score=0.9)])
    monkeypatch.setitem(sp._PROVIDERS, "p1", p1)
    monkeypatch.setitem(sp._PROVIDERS, "p2", p2)

    results = sp.search_federated("query", limit=10, providers=["p1", "p2"])

    titles = {r["title"] for r in results}
    assert titles == {"Alpha Book", "Beta Book"}
    # Higher match_score sorts first.
    assert results[0]["title"] == "Beta Book"


def test_search_federated_tags_each_result_with_its_provider(monkeypatch):
    p1 = _stub_provider("p1", [sp._result("p1", title="Tagged Book")])
    monkeypatch.setitem(sp._PROVIDERS, "p1", p1)

    results = sp.search_federated("query", limit=10, providers=["p1"])

    assert results[0]["provider"] == "p1"


def test_search_federated_survives_one_provider_raising(monkeypatch):
    good = _stub_provider("good", [sp._result("good", title="Survives", match_score=1.0)])
    bad = _stub_provider("bad", [], raise_error=True)
    monkeypatch.setitem(sp._PROVIDERS, "good", good)
    monkeypatch.setitem(sp._PROVIDERS, "bad", bad)

    results = sp.search_federated("query", limit=10, providers=["good", "bad"])

    assert len(results) == 1
    assert results[0]["title"] == "Survives"


def test_search_federated_survives_provider_timeout(monkeypatch):
    def slow_search(query, limit):
        raise httpx.TimeoutException("simulated timeout")

    timeout_provider = _stub_provider("timeout", [])
    timeout_provider.search = slow_search
    good = _stub_provider("good", [sp._result("good", title="Still Works")])
    monkeypatch.setitem(sp._PROVIDERS, "timeout", timeout_provider)
    monkeypatch.setitem(sp._PROVIDERS, "good", good)

    results = sp.search_federated("query", limit=10, providers=["timeout", "good"])

    assert [r["title"] for r in results] == ["Still Works"]


def test_search_federated_all_providers_failing_returns_empty_list_not_error(monkeypatch):
    bad1 = _stub_provider("bad1", [], raise_error=True)
    bad2 = _stub_provider("bad2", [], raise_error=True)
    monkeypatch.setitem(sp._PROVIDERS, "bad1", bad1)
    monkeypatch.setitem(sp._PROVIDERS, "bad2", bad2)

    results = sp.search_federated("query", limit=10, providers=["bad1", "bad2"])

    assert results == []


def test_search_federated_disabled_provider_is_skipped(monkeypatch):
    disabled = _stub_provider("disabled", [sp._result("disabled", title="Should Not Appear")], enabled=False)
    monkeypatch.setitem(sp._PROVIDERS, "disabled", disabled)

    results = sp.search_federated("query", limit=10, providers=["disabled"])

    assert results == []
    assert disabled.calls == []


def test_search_federated_respects_limit_across_providers(monkeypatch):
    p1 = _stub_provider("p1", [sp._result("p1", title=f"P1 Book {i}", match_score=0.5) for i in range(5)])
    p2 = _stub_provider("p2", [sp._result("p2", title=f"P2 Book {i}", match_score=0.5) for i in range(5)])
    monkeypatch.setitem(sp._PROVIDERS, "p1", p1)
    monkeypatch.setitem(sp._PROVIDERS, "p2", p2)

    results = sp.search_federated("query", limit=3, providers=["p1", "p2"])

    assert len(results) == 3


def test_search_federated_empty_query_returns_empty_list_without_calling_providers(monkeypatch):
    p1 = _stub_provider("p1", [sp._result("p1", title="Should Not Be Called")])
    monkeypatch.setitem(sp._PROVIDERS, "p1", p1)

    assert sp.search_federated("", limit=10, providers=["p1"]) == []
    assert sp.search_federated("   ", limit=10, providers=["p1"]) == []
    assert p1.calls == []


def test_search_federated_defaults_to_local_first_provider_set(monkeypatch):
    for name in sp.DEFAULT_PROVIDERS:
        monkeypatch.setitem(sp._PROVIDERS, name, _stub_provider(name, []))

    # Should not raise even though every provider returns nothing.
    assert sp.search_federated("some query") == []
    assert "local" in sp.DEFAULT_PROVIDERS
    assert sp.DEFAULT_PROVIDERS[0] == "local"


def test_default_providers_excludes_non_functional_providers():
    # hardcover.app 403s (Cloudflare) and api.audiobookdb.com 404s -- both are
    # disabled via enabled() and excluded from the default provider set.
    assert "hardcover" not in sp.DEFAULT_PROVIDERS
    assert "audiobookdb" not in sp.DEFAULT_PROVIDERS
    assert sp.DEFAULT_PROVIDERS == ["local", "openlibrary", "googlebooks"]


def test_search_federated_ranks_local_results_above_remote_results(monkeypatch):
    """Local catalog results carry a higher base match_score than remote
    provider results, so search_federated's descending-score sort puts owned
    copies ahead of remote candidates even when titles are identical."""
    _make_work("Shared Title Book", author="Local Author")

    payload = {
        "docs": [
            {
                "key": "/works/OL1W",
                "title": "Shared Title Book",
                "author_name": ["Remote Author"],
            }
        ]
    }
    monkeypatch.setattr(sp.httpx, "Client", _mock_client(_json_handler(payload)))

    results = sp.search_federated("Shared Title Book", limit=10, providers=["openlibrary", "local"])

    assert [r["provider"] for r in results] == ["local", "openlibrary"]


def test_search_federated_rejects_unknown_provider_name():
    with pytest.raises(ValueError):
        sp.search_federated("query", providers=["not-a-real-provider"])


def test_search_federated_deduplicates_repeated_provider_names(monkeypatch):
    calls = []
    p1 = _stub_provider("p1", [sp._result("p1", title="Once")])
    original_search = p1.search

    def counting_search(query, limit):
        calls.append(1)
        return original_search(query, limit)

    p1.search = counting_search
    monkeypatch.setitem(sp._PROVIDERS, "p1", p1)

    sp.search_federated("query", providers=["p1", "p1"])

    assert len(calls) == 1
