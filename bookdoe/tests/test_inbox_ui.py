"""P4-02: the Inbox (Part XX matching UI). The action tests load the real app.js into node
(a vm context with a minimal DOM shim), point its fetch() at a real uvicorn server running
this app on the test database, call the Inbox's own handlers, then check the database."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import db
from app.main import app


STATIC_DIR = Path(__file__).parent.parent / "app" / "static"
APP_JS = STATIC_DIR / "app.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

# Loads app.js as index.html does, minus the final initAuth() bootstrap, then runs BODY
# as an async function in the same realm so it sees app.js's top-level bindings.
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [appJs, base, body] = process.argv.slice(1);
function el() {
  return { innerHTML: '', textContent: '', value: '', dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, appendChild() {}, remove() {}, closest: () => null,
    querySelector: () => el(), querySelectorAll: () => [] };
}
const ctx = {
  console, setTimeout, clearTimeout, URLSearchParams,
  fetch: (p, o) => fetch(base + p, o),
  document: Object.assign(el(), { documentElement: el(), createElement: () => el() }),
  window: { addEventListener() {} }, location: { hash: '', origin: base, reload() {} },
  localStorage: { getItem: () => null, setItem() {} }, confirm: () => true,
  done: r => { console.log(JSON.stringify(r ?? null)); process.exit(0); },
  fail: er => { console.error(er && er.stack || String(er)); process.exit(1); },
};
vm.createContext(ctx);
const src = fs.readFileSync(appJs, 'utf8');
const boot = /\ninitAuth\(\);\s*$/;
if (!boot.test(src)) throw new Error('app.js no longer ends with initAuth();');
vm.runInContext(src.replace(boot, '\n'), ctx, { filename: 'app.js' });
vm.runInContext(`(async () => {\n${body}\n})().then(done, fail)`, ctx, { filename: 'test-body.js' });
"""


def run_ui(body: str, base: str = "http://127.0.0.1:9") -> object:
    out = subprocess.run([NODE, "-e", HARNESS, str(APP_JS), base, body], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


class _Server(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.ready = threading.Event()

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self.ready.set()


@pytest.fixture
def live():
    """This app, served over real HTTP on a free port, on the test database."""
    server = _Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    assert server.ready.wait(20), "uvicorn did not start"
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(20)


def _seed_scan_candidate(path: str, title: str, *, authors=("Test Author",), match_work_id=None) -> str:
    rid, cid, t = db.uid(), db.uid(), db.now()
    detected = {"title": title, "authors": list(authors), "series": "Test Series", "series_position": "1"}
    with db.tx() as c:
        c.execute("INSERT INTO scan_runs(id,status,started_at,finished_at) VALUES (?,'done',?,?)", (rid, t, t))
        c.execute(
            "INSERT INTO scan_candidates(id,scan_run_id,path,media_kind,detected_json,confidence,status,reasons_json,match_work_id,created_at) VALUES (?,?,?,'ebook',?,0.4,'unmatched','[\"title exact match\"]',?,?)",
            (cid, rid, path, json.dumps(detected), match_work_id, t),
        )
    return cid


def _seed_search_candidate(query: str, title: str, *, status: str = "pending") -> str:
    cid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            """INSERT INTO search_candidates(id,query,title,authors_json,series_json,series_position,identifiers_json,providers_json,match_score,match_explanation,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, query, title, '["Nobody Yet"]', '["Test Series"]', "1", '[{"namespace": "isbn", "value": "9781111111111"}]',
             '["openlibrary"]', 0.85, "title match; identifier match", status, t, t),
        )
    return cid


def _seed_work(title: str, *, author: str | None = None, identifiers: dict[str, str] | None = None) -> str:
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,added_at,updated_at) VALUES (?,?,?,?,?)", (wid, title, title, t, t))
        if author:
            aid = db.uid()
            c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (aid, author, author, t, t))
            c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)", (wid, aid, "author", 0))
        for ns, value in (identifiers or {}).items():
            c.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES (?,?,?,?)", ("work", wid, ns, value))
    return wid


def _scan(cid: str) -> dict:
    row = db.one("SELECT * FROM scan_candidates WHERE id=?", (cid,))
    row["detected"] = json.loads(row["detected_json"])
    return row


def _asset_work(path: str) -> str | None:
    row = db.one("SELECT a.work_id FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE f.path=?", (path,))
    return row and row["work_id"]


def _status(cid: str) -> str:
    return db.one("SELECT status FROM search_candidates WHERE id=?", (cid,))["status"]


def _promoted(cid: str) -> str | None:
    return db.one("SELECT promoted_work_id FROM search_candidates WHERE id=?", (cid,))["promoted_work_id"]


@needs_node
def test_scan_candidate_edit_then_link_to_existing_work(live):
    cid = _seed_scan_candidate("/books/edit-me.epub", "Wrong Title")
    wid = _seed_work("Right Title")
    actions = run_ui(f"""
      const c = await api('/api/v1/scan-candidates/{cid}');
      await saveScanEdit(c, {{title: 'Right Title', authors: 'Test Author', series: 'Test Series', series_position: '1', identifiers: 'isbn:9780000000000'}});
      await adoptScanCandidate(c.id, '{wid}');  // what 'Link to existing book' calls with the picked Work
      return scanCandidateActions(c).map(a => a.label);
    """, live)
    assert "Edit parsed details" in actions and "Link to existing book" in actions
    detected = _scan(cid)["detected"]
    assert detected["title"] == "Right Title" and detected["identifiers"] == {"isbn": "9780000000000"}
    assert detected["authors"] == ["Test Author"] and detected["series"] == "Test Series"
    assert _asset_work("/books/edit-me.epub") == wid


@needs_node
def test_scan_candidate_edit_then_adopt_a_search_match_as_a_new_book(live):
    """Also F2: adopt needs an admin, promote does not; checking first means no empty Work."""
    cid = _seed_scan_candidate("/books/new.epub", "Brand New Bok")
    sid = _seed_search_candidate("brand new book", "Brand New Book")
    err = run_ui(f"""
      const c = await api('/api/v1/scan-candidates/{cid}'), s = await api('/api/v1/search-candidates/{sid}');
      await saveScanEdit(c, {{title: 'Brand New Book', authors: 'Test Author', series: 'Test Series', series_position: '1', identifiers: ''}});
      state.me = {{is_admin: false}};
      let err; try {{ await useSearchMatchForScan('{cid}', s); }} catch (er) {{ err = [er.message, (await api('/api/v1/search-candidates/{sid}')).status]; }}
      state.me = {{is_admin: true}};
      await useSearchMatchForScan('{cid}', s);
      return err;
    """, live)
    assert err == ["Only an admin can add a file to the library", "pending"]  # nothing written before the check
    assert _scan(cid)["detected"]["title"] == "Brand New Book"
    assert _promoted(sid) and db.one("SELECT title FROM works WHERE id=?", (_promoted(sid),))["title"] == "Brand New Book"
    assert _asset_work("/books/new.epub") == _promoted(sid)


@needs_node
def test_search_match_for_a_file_joins_the_existing_work_and_clears_the_other_results(live):
    """F2: no duplicate Work when the library has the book, and a card search does not
    leave its unpicked results pending in the Inbox."""
    existing = _seed_work("Existing Book", author="Shared Author", identifiers={"isbn": "9782222222222"})
    _seed_work("Other Book", author="Shared Author")
    cid = _seed_scan_candidate("/books/existing.epub", "Existing Book", authors=("Shared Author",))
    picked, ids = run_ui(f"""
      state.me = {{is_admin: true}};
      const items = (await api(matchSearchUrl({{mode: 'author', q: 'Shared Author', providers: 'local'}}))).items;
      const item = items.find(i => i.title === 'Existing Book');
      await pickSearchResult(new Set(items.map(i => i.id)), item, it => useSearchMatchForScan('{cid}', it));
      return [item.id, items.map(i => i.id)];
    """, live)
    assert len(ids) >= 2 and db.one("SELECT COUNT(*) n FROM works")["n"] == 2
    assert _asset_work("/books/existing.epub") == existing
    assert {_status(i) for i in ids} == {"dismissed"} and _status(picked) == "dismissed"


@needs_node
def test_scan_candidate_ignore_file_and_adopt_match_offered_only_when_found(live):
    cid = _seed_scan_candidate("/books/ignore-me.epub", "Ignore Me")
    labels = run_ui(f"""
      const c = await api('/api/v1/scan-candidates/{cid}');
      await scanCandidateActions(c).find(a => a.label === 'Ignore file').handler();
      return [scanCandidateActions(c).map(a => a.label), scanCandidateActions({{...c, match_work_id: 'w'}}).map(a => a.label)];
    """, live)
    assert _scan(cid)["status"] == "ignored"
    assert "Adopt match" not in labels[0] and labels[1][0] == "Adopt match"


@needs_node
def test_dismiss_scan_candidate_via_dismissScanCandidate_and_afterInboxChange(live):
    """dismissScanCandidate calls the ignore API and afterInboxChange refreshes the inbox."""
    cid = _seed_scan_candidate("/books/dismiss-test.epub", "Dismiss Test")
    run_ui(f"""
      const c = await api('/api/v1/scan-candidates/{cid}');
      // confirm() returns true in the harness, so dismissScanCandidate proceeds.
      await dismissScanCandidate(c.id);
    """, live)
    assert _scan(cid)["status"] == "ignored"


@needs_node
def test_scan_candidate_ignore_file_via_scanCandidateActions_handler(live):
    """F3: clicking 'Ignore file' button calls dismissScanCandidate → post → afterInboxChange."""
    cid = _seed_scan_candidate("/books/ignore-ui.epub", "Ignore UI Test")
    run_ui(f"""
      const c = await api('/api/v1/scan-candidates/{cid}');
      // The 'Ignore file' action handler calls dismissScanCandidate(c.id),
      // which calls confirm() (returns true in the harness), post(), then afterInboxChange().
      await scanCandidateActions(c).find(a => a.label === 'Ignore file').handler();
    """, live)
    assert _scan(cid)["status"] == "ignored"


@needs_node
def test_edit_sends_only_the_fields_the_user_changed():
    """F3: an untouched or emptied box must not overwrite what the scan found."""
    out = run_ui("""
      const d = {title: 'T', authors: ['A', 'B'], series: 'S', series_position: '2', identifiers: [{namespace: 'isbn', value: '978'}]};
      const form = over => Object.assign({title: 'T', authors: 'A, B', series: 'S', series_position: '2', identifiers: 'isbn:978'}, over);
      const err = f => { try { scanEditBody(d, f); return null; } catch (er) { return er.message; } };
      return {
        untouched: scanEditBody(d, form({})),
        title: scanEditBody(d, form({title: 'New'})),
        authorsCleared: scanEditBody(d, form({authors: ''})),
        authorsReordered: scanEditBody(d, form({authors: 'B, A'})),
        seriesCleared: scanEditBody(d, form({series: ''})),
        idAdded: scanEditBody(d, form({identifiers: 'isbn:978\\nASIN:B0'})),
        noAuthorsBefore: scanEditBody({title: 'T'}, {title: 'T', authors: '', series: '', series_position: '', identifiers: ''}),
        emptyTitle: err(form({title: ' '})),
        badId: err(form({identifiers: 'nonsense'})),
      };
    """)
    assert out["untouched"] == {} and out["noAuthorsBefore"] == {}
    assert out["title"] == {"title": "New"}
    assert out["authorsCleared"] == {"authors": []} and out["authorsReordered"] == {"authors": ["B", "A"]}
    assert out["seriesCleared"] == {"series": ""}
    assert out["idAdded"] == {"identifiers": {"isbn": "978", "asin": "B0"}}
    assert out["emptyTitle"] == "A title is required"
    assert out["badId"] == 'Identifier "nonsense" must look like namespace:value'


@needs_node
def test_search_candidate_create_local_link_and_leave_unmatched(live):
    new_id = _seed_search_candidate("q", "Brand New Book")
    link_id = _seed_search_candidate("q", "Linked Book")
    gone_id = _seed_search_candidate("q", "Dismissed Book")
    wid = _seed_work("Existing")
    labels = run_ui(f"""
      const act = async (id, label) => searchCandidateActions(await api('/api/v1/search-candidates/' + id)).find(a => a.label === label).handler();
      await act('{new_id}', 'Create local book');
      await promoteSearchCandidate('{link_id}', '{wid}');  // what 'Link to existing book' calls with the picked Work
      await act('{gone_id}', 'Leave unmatched');
      return searchCandidateActions({{id: 'x'}}).map(a => a.label);
    """, live)
    assert labels == ["Create local book", "Link to existing book", "Search again", "Search all providers", "Search by ISBN / ASIN / author", "Leave unmatched"]
    assert db.one("SELECT title, metadata_source FROM works WHERE id=?", (_promoted(new_id),)) == {"title": "Brand New Book", "metadata_source": "candidate"}
    assert _promoted(link_id) == wid and _status(gone_id) == "dismissed"


@needs_node
def test_search_candidate_card_offers_no_edit_button(live):
    """Known P4-01 gap: without a PATCH endpoint, only scan cards may offer 'Edit parsed details'."""
    sid = _seed_search_candidate("q", "No Edit Book")
    out = run_ui(f"""
      const s = await api('/api/v1/search-candidates/{sid}');
      return {{search: searchCandidateActions(s).map(a => a.label), scan: scanCandidateActions({{id: 'x', detected: {{}}}}).map(a => a.label)}};
    """, live)
    assert not any("Edit" in label for label in out["search"])
    assert "Edit parsed details" in out["scan"]


@needs_node
def test_search_again_on_a_match_replaces_it(live):
    old = _seed_search_candidate("wrong", "Wrong Book")
    better = _seed_search_candidate("right", "Right Book")
    run_ui(f"""
      state.me = {{is_admin: true}};
      const item = await api('/api/v1/search-candidates/{better}');
      await pickSearchResult(new Set([item.id]), item, it => replaceSearchCandidate('{old}', it));
    """, live)
    assert _status(old) == "dismissed" and _status(better) == "promoted"

@needs_node
def test_search_again_joins_existing_work_and_clears_other_results(live):
    """Picking a search match that matches an existing Work joins it instead of
    creating a duplicate, and all other results from that search are dismissed."""
    existing = _seed_work("Existing Book", author="Shared Author", identifiers={"isbn": "9783333333333"})
    _seed_work("Other Book", author="Shared Author")
    old = _seed_search_candidate("shared author", "Old Book")
    picked, ids = run_ui(f"""
      state.me = {{is_admin: true}};
      const items = (await api(matchSearchUrl({{mode: 'author', q: 'Shared Author', providers: 'local'}}))).items;
      const item = items.find(i => i.title === 'Existing Book');
      await pickSearchResult(new Set(items.map(i => i.id)), item, it => replaceSearchCandidate('{old}', it));
      return [item.id, items.map(i => i.id)];
    """, live)
    assert len(ids) >= 2 and db.one("SELECT COUNT(*) n FROM works")["n"] == 2
    assert _promoted(picked) == existing
    assert _status(old) == "dismissed"
    assert _status(picked) == "promoted"
    for i in ids:
        if i != picked:
            assert _status(i) == "dismissed"


@needs_node
def test_replace_search_candidate_refuses_non_admin(live):
    """Non-admin is refused before any write when replacing a search candidate."""
    old = _seed_search_candidate("test", "Test Book")
    better = _seed_search_candidate("test", "Better Book")
    err = run_ui(f"""
      state.me = {{is_admin: false}};
      const item = await api('/api/v1/search-candidates/{better}');
      let err; try {{ await pickSearchResult(new Set([item.id]), item, it => replaceSearchCandidate('{old}', it)); }} catch (er) {{ err = [er.message, (await api('/api/v1/search-candidates/{better}')).status]; }}
      state.me = {{is_admin: true}};
      await pickSearchResult(new Set([item.id]), item, it => replaceSearchCandidate('{old}', it));
      return err;
    """, live)
    assert err == ["Only an admin can add a book to the library", "pending"]
    assert _status(old) == "dismissed" and _status(better) == "promoted"


@needs_node
def test_use_search_match_for_scan_refuses_non_admin(live):
    """Non-admin is refused before any write when picking a search result for a scan card."""
    cid = _seed_scan_candidate("/books/test.epub", "Test Book")
    sid = _seed_search_candidate("test", "Test Book")
    err = run_ui(f"""
      state.me = {{is_admin: false}};
      const item = await api('/api/v1/search-candidates/{sid}');
      let err; try {{ await pickSearchResult(new Set([item.id]), item, it => useSearchMatchForScan('{cid}', it)); }} catch (er) {{ err = er.message; }}
      state.me = {{is_admin: true}};
      await pickSearchResult(new Set([item.id]), item, it => useSearchMatchForScan('{cid}', it));
      return err;
    """, live)
    assert err == "Only an admin can add a file to the library"


@needs_node
def test_scan_card_joins_existing_work_and_dismisses_other_results(live):
    """Scan card: picking a search result that matches an existing Work adopts the
    scan file into that Work, dismisses the picked search result, and dismisses
    other results from the same search."""
    existing = _seed_work("Existing Book", author="Shared Author", identifiers={"isbn": "9784444444444"})
    _seed_work("Other Book", author="Shared Author")
    cid = _seed_scan_candidate("/books/shared.epub", "Shared Book")
    picked, ids = run_ui(f"""
      state.me = {{is_admin: true}};
      const items = (await api(matchSearchUrl({{mode: 'author', q: 'Shared Author', providers: 'local'}}))).items;
      const item = items.find(i => i.title === 'Existing Book');
      await pickSearchResult(new Set(items.map(i => i.id)), item, it => useSearchMatchForScan('{cid}', it));
      return [item.id, items.map(i => i.id)];
    """, live)
    assert len(ids) >= 2 and db.one("SELECT COUNT(*) n FROM works")["n"] == 2
    # The scan file is adopted into the existing Work
    assert _asset_work("/books/shared.epub") == existing
    # The picked search result is dismissed (not promoted, since it joined)
    assert _status(picked) == "dismissed"
    # Other results from the search are also dismissed
    for i in ids:
        if i != picked:
            assert _status(i) == "dismissed"


@needs_node
def test_pick_search_result_promote_then_adopt_from_scan_card(live):
    """Full pick→promote→adopt flow from a scan card when the result does not match
    an existing Work: the result is promoted to a new Work, then the scan file is
    adopted into that Work."""
    cid = _seed_scan_candidate("/books/new.epub", "Brand New Book")
    sid = _seed_search_candidate("brand new book", "Brand New Book")
    run_ui(f"""
      state.me = {{is_admin: true}};
      const item = await api('/api/v1/search-candidates/{sid}');
      await pickSearchResult(new Set([item.id]), item, it => useSearchMatchForScan('{cid}', it));
    """, live)
    promoted = _promoted(sid)
    assert promoted and db.one("SELECT title FROM works WHERE id=?", (promoted,))["title"] == "Brand New Book"
    assert _asset_work("/books/new.epub") == promoted


@needs_node
def test_pick_search_result_promote_from_search_card(live):
    """Full pick→promote flow from a search card when the result does not match an
    existing Work: the result is promoted to a new Work and the old match is dismissed."""
    old = _seed_search_candidate("old query", "Old Book")
    new_book = _seed_search_candidate("new query", "New Book")
    run_ui(f"""
      state.me = {{is_admin: true}};
      const item = await api('/api/v1/search-candidates/{new_book}');
      await pickSearchResult(new Set([item.id]), item, it => replaceSearchCandidate('{old}', it));
    """, live)
    promoted = _promoted(new_book)
    assert promoted and db.one("SELECT title FROM works WHERE id=?", (promoted,))["title"] == "New Book"
    assert _status(old) == "dismissed" and _status(new_book) == "promoted"


@needs_node
def test_find_existing_work_matches_by_title_and_author(live):
    """findExistingWork matches by title+author when no identifier is shared.
    The return value is asserted to equal the seeded Work's id."""
    existing = _seed_work("Same Title", author="Same Author", identifiers={"isbn": "9781111111111"})
    _seed_work("Other Book", author="Other Author")
    old = _seed_search_candidate("same title", "Same Title")
    result = run_ui(f"""
      state.me = {{is_admin: true}};
      const item = await api('/api/v1/search-candidates/{old}');
      // Simulate a search result with matching title+author but different identifier
      const match = {{ ...item, identifiers: {{ asin: "B000000001" }}, title: "Same Title", authors: ["Same Author"] }};
      const found = await findExistingWork(match.identifiers, match.title, match.authors);
      return found ? found.id : null;
    """, live)
    # The search result should match the existing Work by title+author
    assert result == existing


@needs_node
def test_find_existing_work_matches_by_identifier_alone(live):
    """findExistingWork matches by shared identifier even when title and author differ."""
    existing = _seed_work("Different Title", author="Different Author", identifiers={"isbn": "9789999999999"})
    _seed_work("Other Book", author="Other Author")
    result = run_ui(f"""
      state.me = {{is_admin: true}};
      // Simulate a search result with the same identifier but different title and author
      const match = {{ identifiers: {{ isbn: "9789999999999" }}, title: "Not The Same", authors: ["Not The Author"] }};
      const found = await findExistingWork(match.identifiers, match.title, match.authors);
      return found ? found.id : null;
    """, live)
    # The search result should match the existing Work by identifier alone
    assert result == existing


@needs_node
def test_card_ui_actions_wired_to_pick_search_result(live):
    """Scan and search cards expose 'Find a match', 'Search again', and
    'Search by ISBN / ASIN / author' actions that wire into pickSearchResult."""
    out = run_ui("""
      const scan = scanCandidateActions({id: 's1', detected: {title: 'Test', authors: ['Author'], identifiers: {isbn: '9780000000000'}}});
      const search = searchCandidateActions({id: 'sc1', title: 'Test', authors: ['Author'], query: 'test'});
      return {
        scan_labels: scan.map(a => a.label),
        search_labels: search.map(a => a.label),
        scan_has_find: scan.some(a => a.label === 'Find a match'),
        scan_has_isbn: scan.some(a => a.label === 'Search by ISBN / ASIN / author'),
        search_has_again: search.some(a => a.label === 'Search again'),
        search_has_isbn: search.some(a => a.label === 'Search by ISBN / ASIN / author'),
      };
    """, live)
    assert out["scan_has_find"]
    assert out["scan_has_isbn"]
    assert out["search_has_again"]
    assert out["search_has_isbn"]


@needs_node
def test_every_card_search_mode_returns_usable_results(live):
    _seed_work("Dune", author="Frank Herbert", identifiers={"isbn": "9780441013593", "asin": "B00B7NPRY8"})
    out = run_ui("""
      const titles = async (mode, q) => (await api(matchSearchUrl({mode, q, providers: 'local'}))).items.map(i => i.title);
      return {
        again: await titles('text', 'Dune'), isbn: await titles('isbn', '978-0-441-01359-3'),
        asin: await titles('asin', 'B00B7NPRY8'), author: await titles('author', 'Frank Herbert'),
        allUrl: matchSearchUrl({mode: 'text', q: 'x', providers: 'all'}),
        seed: [identifierSeed([{namespace: 'isbn', value: '1'}], []), identifierSeed({asin: 'B1'}, []), identifierSeed([], ['Au'])],
      };
    """, live)
    for mode in ("again", "isbn", "asin", "author"):
        assert "Dune" in out[mode], (mode, out[mode])
    assert out["allUrl"].endswith("&providers=local,openlibrary,hardcover,audiobookdb,googlebooks")
    assert out["seed"] == [{"mode": "isbn", "value": "1"}, {"mode": "asin", "value": "B1"}, {"mode": "author", "value": "Au"}]


# A DOM that remembers elements by selector, so the real matchSearchDialog can be opened from a
# card action, driven through its own Search button, and have its results read back. modal()
# and the code under test are untouched; only the page they draw into is faked.
DIALOG_DOM = r"""
const els = {}, picks = {};
const mk = sel => {
  const o = { sel, dataset: {}, style: {}, listeners: [], waiters: [], _html: '', value: '',
    classList: { contains: () => true, add() {}, remove() {}, toggle() {} },
    addEventListener(t, f) { this.listeners.push(f); }, appendChild() {}, remove() {}, querySelector: get,
    querySelectorAll(q) {
      if (sel !== '#ms-results' || q !== '[data-result]') return [];
      return [...this._html.matchAll(/data-result="([^"]*)"/g)].map(m => ({ dataset: { result: m[1] },
        querySelectorAll: () => [picks[m[1]] = { dataset: { actionIndex: '0' } }] }));
    } };
  Object.defineProperty(o, 'innerHTML', { get: () => o._html, set: v => { o._html = v; if (!String(v).includes('spinner')) o.waiters.splice(0).forEach(r => r()); } });
  return o;
};
const get = sel => els[sel] ||= mk(sel);
document.querySelector = get;
const origModal = modal;
modal = (...a) => {  // the real modal(); then fill the inputs from the markup it rendered
  const r = origModal(...a), h = get('#modal-root').innerHTML;
  const selected = id => { const s = h.match(new RegExp('id="' + id + '"[\\s\\S]*?</select>')); const o = s && s[0].match(/<option value="([^"]*)" selected/); return o ? o[1] : ''; };
  get('#ms-mode').value = selected('ms-mode'); get('#ms-providers').value = selected('ms-providers');
  get('#ms-q').value = (h.match(/id="ms-q" value="([^"]*)"/) || [])[1] || '';
  return r;
};
const settled = () => new Promise(r => get('#ms-results').waiters.push(r));
const resultIds = () => get('#ms-results').querySelectorAll('[data-result]').map(c => c.dataset.result);
const titles = async ids => Promise.all(ids.map(async id => (await api('/api/v1/search-candidates/' + id)).title));
// Open a card's action (its dialog runs the seeded search by itself) and wait for the results.
const open = async (actions, label) => {
  const done = settled(), a = actions.find(x => x.label === label);
  if (!a) throw new Error('no action ' + label);
  await a.handler(); await done;
  return { ids: resultIds(), html: get('#ms-results').innerHTML, mode: get('#ms-mode').value, q: get('#ms-q').value, providers: get('#ms-providers').value };
};
"""


@pytest.fixture
def offline(monkeypatch):
    """Only the local provider answers, so 'all providers' needs no network."""
    from app.services import search_providers as sp
    for name, p in sp._PROVIDERS.items():
        if name != "local":
            monkeypatch.setattr(type(p), "enabled", lambda self: False)


@needs_node
def test_every_search_mode_runs_the_real_dialog_from_a_scan_card_and_a_search_card(live, offline):
    """Search again / all providers / ISBN-ASIN-author, each from both card types, through the
    real action -> matchSearchDialog -> run -> federated endpoint -> results path. A scan card's
    'Search again' is its 'Find a match' (same query, default providers)."""
    _seed_work("Dune", author="Frank Herbert", identifiers={"isbn": "9781111111111"})
    sid = _seed_search_candidate("Dune", "Dune")
    cid = _seed_scan_candidate("/books/dune.epub", "Dune", authors=())  # the local provider matches one field at a time
    out = run_ui(DIALOG_DOM + f"""
      const search = await api('/api/v1/search-candidates/{sid}'), scan = await api('/api/v1/scan-candidates/{cid}');
      scan.detected.identifiers = {{isbn: '9781111111111'}};
      const res = {{}};
      for (const [card, actions, again] of [['search', searchCandidateActions(search), 'Search again'], ['scan', scanCandidateActions(scan), 'Find a match']]) {{
        for (const [mode, label] of [['again', again], ['all', 'Search all providers'], ['ids', 'Search by ISBN / ASIN / author']]) {{
          const r = await open(actions, label);
          res[card + ':' + mode] = {{mode: r.mode, q: r.q, providers: r.providers, titles: await titles(r.ids), cards: r.html.includes('Use this')}};
        }}
      }}
      return res;
    """, live)
    assert len(out) == 6
    for key, r in out.items():
        assert "Dune" in r["titles"] and r["cards"], (key, r)
        assert r["providers"] == ("all" if key.endswith(":all") else ""), (key, r)
    assert out["search:ids"]["mode"] == out["scan:ids"]["mode"] == "isbn" and out["scan:ids"]["q"] == "9781111111111"


@needs_node
def test_search_again_replaces_the_stale_results_and_closing_clears_the_rest(live, offline):
    _seed_work("Dune", author="Frank Herbert")
    _seed_work("Emma", author="Jane Austen")
    sid = _seed_search_candidate("Dune", "Dune")
    out = run_ui(DIALOG_DOM + f"""
      const actions = searchCandidateActions(await api('/api/v1/search-candidates/{sid}'));
      const first = await open(actions, 'Search again');
      const done = settled(); get('#ms-q').value = 'Emma'; get('#ms-go').onclick(); await done;  // search again, new query
      const second = resultIds(), html = get('#ms-results').innerHTML;
      const states = async ids => Promise.all(ids.map(async id => (await api('/api/v1/search-candidates/' + id)).status));
      const mid = {{first: await states(first.ids), second: await states(second), titles: await titles(second), shows: [html.includes('Dune'), html.includes('Emma')]}};
      await get('.modal-cancel').onclick();
      const closed = await states(second);
      const reopened = await open(actions, 'Search again');  // backdrop path closes the same way
      await get('.modal-backdrop').listeners.at(-1)({{target: get('.modal-backdrop')}});
      return {{mid, closed, modalRoot: get('#modal-root').innerHTML, backdrop: await states(reopened.ids), firstIds: first.ids, secondIds: second}};
    """, live)
    assert out["firstIds"] and not set(out["firstIds"]) & set(out["secondIds"])
    assert set(out["mid"]["first"]) == {"dismissed"}                 # stale results discarded
    assert set(out["mid"]["second"]) == {"pending"} and out["mid"]["titles"] == ["Emma"]
    assert out["mid"]["shows"] == [False, True]                      # the panel shows only the fresh ones
    assert set(out["closed"]) == {"dismissed"}                       # Close: unpicked results cleared
    assert out["modalRoot"] == "" and set(out["backdrop"]) == {"dismissed"}  # backdrop: closes and clears too


def test_all_providers_constant_matches_the_real_registry(monkeypatch):
    """'Search all providers' must name exactly the registered providers: an unknown name
    makes the federated endpoint fail and a missing one silently narrows results."""
    from app.services import search_providers as sp
    names = re.search(r"const ALL_PROVIDERS = '([^']*)'", APP_JS.read_text()).group(1)
    assert set(names.split(",")) == set(sp._PROVIDERS)
    for p in sp._PROVIDERS.values():  # real registry, real endpoint; disabled so no network is needed
        monkeypatch.setattr(type(p), "enabled", lambda self: False)
    with TestClient(app) as c:
        assert c.get(f"/api/v1/search/federated?q=anything&providers={names}").status_code == 200


@needs_node
def test_evidence_card_renders_inferred_evidence_and_actions_separately_and_binds_them():
    out, wired = run_ui("""
      const m = scanCandidateModel({id: '1', media_kind: 'ebook', status: 'probable', confidence: 0.72, reasons: ['title exact match'],
        detected: {title: 'Dune <b>', authors: ['Frank Herbert'], series: 'Dune', series_position: '1', identifiers: {isbn: '9780441013593'}}});
      const calls = [], buttons = [];
      const actions = [{label: 'Ignore file', handler: () => calls.push('ignore')}, {label: 'Two', handler: () => calls.push('two')}];
      // Real $$ and bindEvidenceActions over the buttons evidenceCard actually rendered.
      const root = { innerHTML: evidenceCard(m.inferred, m.evidence, actions), querySelectorAll(sel) {
        if (sel !== '[data-action-index]') return [];
        for (const x of this.innerHTML.matchAll(/data-action-index="(\\d+)"/g)) buttons.push({dataset: {actionIndex: x[1]}});
        return buttons;
      } };
      bindEvidenceActions(root, actions);
      await buttons[1].onclick();
      return [root.innerHTML, [buttons.length, calls]];
    """)
    assert out.index("bookdoe inferred") < out.index("What you can do")
    assert "Dune &lt;b&gt;" in out and "<b>" not in out
    for expected in ("Frank Herbert", "Dune #1", "ebook", "isbn: 9780441013593", "title exact match", "72% match", "Ignore file"):
        assert expected in out
    assert wired == [2, ["two"]]


@needs_node
def test_evidence_card_shows_search_match_explanation_and_is_generic():
    card, plain = run_ui("""
      const m = searchCandidateModel({title: 'T', authors: ['A'], series: [{name: 'S'}], series_position: '2',
        identifiers: [{namespace: 'asin', value: 'B000'}], match_score: 0.95, match_explanation: 'identifier match', query: 't a', providers: ['openlibrary']});
      // The same renderer serves a non-candidate record (Metadata Studio): no actions, no evidence.
      return [evidenceCard(m.inferred, m.evidence, []), evidenceCard({title: 'Plain'}, {}, [])];
    """)
    assert "identifier match" in card and "95% match" in card and "S #2" in card and "asin: B000" in card and "openlibrary" in card
    assert "What you can do" not in plain and "Plain" in plain


def test_legacy_review_dialog_removed_and_routes_reach_inbox():
    js = APP_JS.read_text()
    for name in ("paintReview", "reviewCard", "reviewSource", "reviewCandidates", "reviewFindMatch", "reviewPick", "reviewFindAll", "renderReview"):
        assert not re.search(rf"function\s+{name}\s*\(", js), f"{name} should be removed"
    assert "if(r==='inbox') return await renderInbox();" in js
    assert "if(r==='review') return await renderInbox();" in js  # old bookmarks
    assert "['inbox','Inbox'" in js and "['review','Review'" not in js
    assert "inbox:state.dashboard.counts.review" in js
    assert "refreshWorkMetadata" in js  # Work-detail entry point untouched


@needs_node
def test_review_tasks_fallback_is_listed_and_ignorable_from_the_inbox(live):
    """review_tasks are still written by the resolver and the Readarr migration and no
    candidate table covers them, so the Inbox keeps a section for them."""
    wid = _seed_work("Unmatched Stub")
    tid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "metadata_match", "work", wid, "Unmatched Stub", json.dumps({"source": {"author": "Someone"}}), "open", 0, t))
    listed = run_ui(f"""
      await loadInbox('review');
      const t = inboxState.review.items.find(x => x.id === '{tid}');
      await reviewTaskActions(t).find(a => a.label === 'Ignore').handler();
      return reviewTaskModel(t).inferred;
    """, live)
    assert listed["title"] == "Unmatched Stub" and listed["authors"] == ["Someone"]
    assert db.one("SELECT status FROM review_tasks WHERE id=?", (tid,))["status"] != "open"


@needs_node
def test_inbox_review_section_shows_categories_and_bulk_triage_runs(live):
    """The review backlog is grouped by category with counts, and the admin bulk action
    (confirm() is auto-accepted by the harness) goes through the real triage route."""
    wid = _seed_work("Nothing To Match")
    tid = db.uid()
    with db.tx() as c:
        c.execute(
            "INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "metadata_match", "work", wid, "Nothing To Match", "{}", "open", 0, db.now()))
    out = run_ui("""
      await loadInbox('review');
      const before = {...inboxState.review.categories};
      await runReviewTriage('ignore_hopeless');
      return {before, after: inboxState.review.categories, total: inboxState.review.total};
    """, live)
    assert out["before"]["hopeless"] == 1 and out["after"]["hopeless"] == 0 and out["total"] == 0
    assert db.one("SELECT status FROM review_tasks WHERE id=?", (tid,))["status"] == "ignored"


@needs_node
def test_inbox_loads_pending_search_matches_and_unhidden_scans(live):
    visible = _seed_scan_candidate("/books/visible.epub", "Visible")
    ignored = _seed_scan_candidate("/books/ignored.epub", "Ignored")
    with db.tx() as c:
        c.execute("UPDATE scan_candidates SET status='ignored' WHERE id=?", (ignored,))
    pending = _seed_search_candidate("q", "Pending")
    _seed_search_candidate("q", "Promoted", status="promoted")
    out = run_ui("""
      await loadInbox('scan'); await loadInbox('search');
      return {scan: inboxState.scan.items.map(x => x.id), search: inboxState.search.items.map(x => x.id),
              model: searchCandidateModel(inboxState.search.items[0])};
    """, live)
    assert out["scan"] == [visible] and out["search"] == [pending]
    assert out["model"]["evidence"] == {"source": 'Search "q" via openlibrary', "explanation": "title match; identifier match", "confidence": 0.85}


def test_search_candidate_edit_endpoint_is_still_missing():
    """Known P4-01 gap, reported as a follow-up: there is no PATCH for a search candidate,
    so search cards cannot offer 'Edit parsed details'. When the endpoint lands this fails,
    as a reminder to wire it into searchCandidateActions."""
    assert not any(getattr(r, "path", "") == "/api/v1/search-candidates/{candidate_id}" and "PATCH" in r.methods for r in app.routes)


@needs_node
def test_metadata_studio_shell_is_routed_with_three_tabs(live):
    """P4-06a: the Work page links to a routed (not modal) Studio with Work/Edition/File tabs.
    P4-06e: cover controls live in the Studio Work tab."""
    wid = _seed_work("Studio Book", author="Test Author")
    out = run_ui(f"""
      const view = {{ innerHTML: '', querySelector: () => view, querySelectorAll: () => [], addEventListener() {{}}, setAttribute() {{}}, insertAdjacentHTML() {{}}, appendChild() {{}}, classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }} }};
      document.querySelector = () => view;
      document.querySelectorAll = () => [];
      document.body = {{ style: {{}}, classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }} }};
      state.me = {{ is_admin: true }};
      await renderWork('{wid}');
      const workPage = view.innerHTML;
      const tabs = {{}};
      for (const t of [undefined, 'work', 'edition', 'file']) {{
        location.hash = '#library/{wid}/studio' + (t ? '/' + t : '');
        await route();
        tabs[t || 'default'] = view.innerHTML;
      }}
      return {{ workPage, tabs }};
    """, live)
    page = out["workPage"]
    assert 'id="open-studio" data-route="library/%s/studio"' % wid in page and "Metadata Studio</button>" in page
    assert 'id="edit-work"' not in page  # cover controls migrated to Studio in P4-06e
    for key, label, text in (("work", "Work", "Select a field to edit"), ("edition", "Edition", "Edition metadata (read-only)"), ("file", "File", "File metadata (read-only)")):
        html = out["tabs"][key]
        assert html.count('data-studio-tab="') == 3
        assert f'class="tab active" role="tab" aria-selected="true" data-studio-tab="{key}"' in html
        assert f'data-studio-panel="{key}"' in html and text in html
        assert "modal" not in html
        # Layout comes from the shared stylesheet classes. The one inline style is the Work
        # tab's cover thumbnail fallback, whose per-book hue is data, not layout.
        assert html.count("style=") == (1 if key == "work" else 0)
        assert key != "work" or re.search(r'<div class="cover-fallback" style="--hue:\d+">', html)
        assert 'class="tabs studio-tabs"' in html and 'class="panel studio-panel"' in html
        for other in ("Edition metadata (read-only)", "File metadata (read-only)", "Select a field to edit"):
            assert (other in html) == (other == text)
    assert 'data-studio-panel="work"' in out["tabs"]["default"]
    # Cover controls are present in the Work tab (P4-06e)
    work_html = out["tabs"]["default"]
    assert 'data-studio-field="cover"' in work_html
    assert 'id="studio-cover-fetch"' in work_html
    assert 'id="studio-cover-remove"' in work_html
    assert 'id="studio-cover-url-input"' in work_html
    assert 'id="studio-cover-file-input"' in work_html


@needs_node
def test_metadata_studio_edition_and_file_tabs_list_part_xxi_fields_read_only(live):
    """P4-06d: the Edition and File tabs list every Part XXI EDITION / FILE METADATA field,
    read-only, from the existing Work detail payload (no extra request)."""
    wid = _seed_work("Studio Fields", author="Test Author", identifiers={"isbn13": "9780316129084", "asin": "B0047Y171G"})
    eid, aid, t = db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO editions(id,work_id,title,publisher,release_date,language,pages,cover_url,duration_seconds,abridged,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, wid, "Studio Fields (Audio)", "Orbit", "2011-06-15", "en", 592, "https://covers.example/e.jpg", 73800, 0, t, t),
        )
        c.execute(
            "INSERT INTO media_assets(id,work_id,edition_id,kind,format,status,narrator_json,added_at,updated_at) VALUES (?,?,?,'audiobook','m4b','owned','[\"Jefferson Mays\"]',?,?)",
            (aid, wid, eid, t, t),
        )
        c.execute(
            "INSERT INTO media_files(id,asset_id,path,size_bytes,container,codec,bitrate,duration_seconds,discovered_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (db.uid(), aid, "/audiobooks/Studio Fields/Studio Fields.m4b", 734003200, "m4b", "aac", 64000, 73800, t),
        )
        c.execute("UPDATE works SET preferred_audiobook_edition_id=? WHERE id=?", (eid, wid))
    out = run_ui(f"""
      const view = {{ innerHTML: '', querySelector: () => view, querySelectorAll: () => [], addEventListener() {{}}, setAttribute() {{}}, insertAdjacentHTML() {{}}, appendChild() {{}}, classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }} }};
      document.querySelector = () => view;
      document.querySelectorAll = () => [];
      document.body = {{ style: {{}}, classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }} }};
      state.me = {{ is_admin: true }};
      const calls = [], realFetch = fetch;
      globalThis.fetch = (p, o) => {{ calls.push(p); return realFetch(p, o); }};
      const tabs = {{}};
      for (const t of ['edition', 'file']) {{
        location.hash = '#library/{wid}/studio/' + t;
        await route();
        tabs[t] = view.innerHTML;
      }}
      return {{ tabs, calls }};
    """, live)
    # Sourced from the Work detail payload the shell already loads: one GET per tab render.
    assert out["calls"] == [f"/api/v1/library/{wid}"] * 2
    out = out["tabs"]

    def fields(html):
        return dict(re.findall(r'<tr data-studio-field="([^"]+)"><th scope="row">[^<]+</th><td>(.*?)</td></tr>', html))

    edition = fields(out["edition"])
    assert edition == {
        "ISBN (work-level)": "9780316129084", "ASIN (work-level)": "B0047Y171G", "Publisher": "Orbit", "Publication date": "2011-06-15",
        "Language": "en", "Page count": "592", "Narrator": "Jefferson Mays", "Audiobook duration": "20h 30m",
        "Abridged/unabridged": "Unabridged", "Edition cover": "https://covers.example/e.jpg",
    }
    assert "Studio Fields (Audio)" in out["edition"] and "Preferred audiobook" in out["edition"]
    not_recorded = '<span class="muted">Not recorded</span>'
    assert fields(out["file"]) == {
        "Format": "M4B", "Codec": "aac", "Bitrate": "64 kbps", "Sample rate": not_recorded, "Channels": not_recorded,
        "Duration": "20h 30m", "Chapter count": not_recorded, "Path": "/audiobooks/Studio Fields/Studio Fields.m4b",
        "File size": "700.0 MB", "Embedded metadata state": not_recorded,
    }
    assert "Audiobook · Studio Fields.m4b" in out["file"]
    for html in out.values():
        panel = html[html.index("data-studio-panel="):]
        # Strictly read-only: no controls, no evidence cards, no routes out of the panel.
        for forbidden in ("<button", "<input", "<select", "<textarea", "<form", "data-route", "evidence-card", "data-action-index", "style="):
            assert forbidden not in panel, forbidden


def test_metadata_studio_reuses_shared_stylesheet_classes():
    """P4-06a / Part L: the Studio shell's layout classes all come from the shared stylesheet,
    and its source opens no modal and sets no inline style."""
    css = (STATIC_DIR / "styles.css").read_text()
    for sel in (".tabs", ".tab", ".tab.active", ".panel", ".panel-body", ".page-head"):
        assert re.search(r"^%s\s*\{" % re.escape(sel), css, re.M), sel
    src = APP_JS.read_text()
    studio = src[src.index("const STUDIO_TABS="):src.index("function recommendationsHtml")]
    assert "style=" not in studio and "modal" not in studio.lower()
