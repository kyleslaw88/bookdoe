from pathlib import Path
import zipfile

import pytest

from app import db
from app.services import scanner


def make_epub(path: Path, isbn="9781234567897"):
    container = '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    opf = f'''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title><dc:creator>Jane Author</dc:creator><dc:identifier opf:scheme="ISBN" xmlns:opf="http://www.idpf.org/2007/opf">{isbn}</dc:identifier><dc:language>en</dc:language></metadata></package>'''
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)


def seed_work(isbn="9781234567897"):
    wid, eid, cid, t = db.uid(), db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)", (wid,"Test Book","Test Book",t,t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (cid,"Jane Author","Author, Jane",t,t))
        c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)", (wid,cid))
        c.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", (eid,wid,"Test Book",t,t))
        c.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('edition',?, 'isbn13', ?, 'test',1,1)", (eid,isbn))
    return wid,eid


def test_epub_inspection_and_exact_match(tmp_path):
    p=tmp_path/"test.epub"; make_epub(p); wid,eid=seed_work()
    detected=scanner.inspect_file(p)
    assert detected["title"]=="Test Book"
    assert detected["authors"]==["Jane Author"]
    assert detected["identifiers"]["isbn13"]=="9781234567897"
    match=scanner.match_detected(detected)
    assert match["status"]=="matched"
    assert match["work_id"]==wid
    assert match["edition_id"]==eid


def test_scan_adopts_without_moving(tmp_path):
    p=tmp_path/"Test Book.epub"; make_epub(p); wid,_=seed_work()
    rid=db.uid(); t=db.now()
    with db.tx() as c:
        c.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,'ebook','adopt',?,?)", (rid,"Books",str(tmp_path),t,t))
    before=p.read_bytes()
    result=scanner.scan_root(rid)
    assert result["matched"]==1
    assert p.exists() and p.read_bytes()==before
    mf=db.one("SELECT * FROM media_files WHERE path=?",(str(p),))
    assert mf and mf["adopted"]==1 and mf["managed"]==0
    asset=db.one("SELECT * FROM media_assets WHERE id=?",(mf["asset_id"],))
    assert asset["work_id"]==wid and asset["status"]=="owned"


def seed_work_with_identifier(title, ns, value):
    """Seed a work+edition carrying one identifier in the given namespace (no title collision)."""
    wid, eid, t = db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)", (wid,title,title,t,t))
        c.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", (eid,wid,title,t,t))
        c.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('edition',?,?,?, 'test',1,1)", (eid,ns,value))
    return wid,eid


def seed_work_title_only(title):
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)", (wid,title,title,t,t))
    return wid


def test_isbn13_identifier_match_wins_over_different_work_title():
    # Detected file carries Work A's ISBN13, but its embedded title text
    # exactly matches Work B's catalog title. The identifier must win.
    work_a,_=seed_work_with_identifier("Dune", "isbn13", "9780000000013")
    work_b=seed_work_title_only("Mistborn")
    detected={"identifiers":{"isbn13":"9780000000013"}, "title":"Mistborn"}
    match=scanner.match_detected(detected)
    assert match["status"]=="matched"
    assert match["work_id"]==work_a
    assert match["work_id"]!=work_b
    assert match["confidence"]==1.0


def test_isbn10_identifier_match_wins_over_different_work_title():
    work_a,_=seed_work_with_identifier("Dune", "isbn10", "0000000010")
    work_b=seed_work_title_only("Mistborn")
    detected={"identifiers":{"isbn10":"0000000010"}, "title":"Mistborn"}
    match=scanner.match_detected(detected)
    assert match["status"]=="matched"
    assert match["work_id"]==work_a
    assert match["work_id"]!=work_b
    assert match["confidence"]==1.0


def test_asin_identifier_match_wins_over_different_work_title():
    work_a,_=seed_work_with_identifier("Dune", "asin", "B000000ASIN")
    work_b=seed_work_title_only("Mistborn")
    detected={"identifiers":{"asin":"B000000ASIN"}, "title":"Mistborn"}
    match=scanner.match_detected(detected)
    assert match["status"]=="matched"
    assert match["work_id"]==work_a
    assert match["work_id"]!=work_b
    assert match["confidence"]==1.0


def test_unique_exact_title_match_is_probable(monkeypatch):
    # No identifier present, so match_detected falls through to the OpenReads
    # resolver before local title matching; stub it out as "no match" so this
    # test exercises the local-title fallback deterministically, not real network I/O.
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kwargs: {})
    work_a=seed_work_title_only("Unique Title Book")
    detected={"identifiers":{}, "title":"Unique Title Book"}
    match=scanner.match_detected(detected)
    assert match["status"]=="probable"
    assert match["work_id"]==work_a
    assert match["confidence"]==0.85


def test_multiple_title_matches_is_ambiguous(monkeypatch):
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kwargs: {})
    seed_work_title_only("Duplicate Title")
    seed_work_title_only("Duplicate Title")
    detected={"identifiers":{}, "title":"Duplicate Title"}
    match=scanner.match_detected(detected)
    assert match["status"]=="ambiguous"


def test_no_match_returns_unmatched_with_reason(monkeypatch):
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kwargs: {})
    detected={"identifiers":{}, "title":"Nothing In The Catalog Resembles This"}
    match=scanner.match_detected(detected)
    assert match["status"]=="unmatched"
    assert match["confidence"]==0
    assert match["reasons"]


def test_identifier_match_overrides_conflicting_title_match():
    # Explicit conflict case: the file is genuinely Work A (correct ISBN13),
    # but its embedded title text exactly matches Work B's catalog title --
    # simulating a mislabeled/misnamed file. The result must still follow the
    # identifier (Work A), proving priority order explicitly rather than only
    # implicitly through the single-namespace cases above.
    work_a,_=seed_work_with_identifier("Alpha Original", "isbn13", "9789999999999")
    work_b=seed_work_title_only("Bravo Rewrite")
    detected={"identifiers":{"isbn13":"9789999999999"}, "title":"Bravo Rewrite"}
    match=scanner.match_detected(detected)
    assert match["status"]=="matched"
    assert match["work_id"]==work_a
    assert match["work_id"]!=work_b
    assert match["confidence"]==1.0
    assert any("isbn13" in r for r in match["reasons"])


# ---------- P4-01: edit_candidate / ignore_candidate (service layer) ----------

def _seed_scan_candidate(status="unmatched", detected=None):
    rid, cid, t = db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO scan_runs(id,status,started_at,finished_at) VALUES (?,?,?,?)", (rid, "done", t, t))
        c.execute(
            """INSERT INTO scan_candidates(id,scan_run_id,path,media_kind,detected_json,confidence,status,reasons_json,created_at)
               VALUES (?,?,?,'ebook',?,0.5,?,'[]',?)""",
            (cid, rid, "/books/a.epub", db.json_dump(detected or {"title": "Original", "authors": ["Original Author"]}), status, t),
        )
    return cid


def test_edit_candidate_merges_only_the_allowed_fields():
    cid = _seed_scan_candidate()
    result = scanner.edit_candidate(cid, {"title": "New Title"})
    assert result["detected"]["title"] == "New Title"
    assert result["detected"]["authors"] == ["Original Author"]  # untouched


def test_edit_candidate_rejects_a_field_outside_the_allow_list():
    """Direct unit test of the service-layer allow-list guard itself -- the
    route only ever passes fields through a Pydantic model that already
    restricts the key set, so this path isn't independently reachable via the
    HTTP layer (see test_scan_candidates_api.py's endpoint-level coverage).
    """
    cid = _seed_scan_candidate()
    with pytest.raises(ValueError, match="Unknown editable field"):
        scanner.edit_candidate(cid, {"confidence": 0.99})


def test_edit_candidate_raises_for_unknown_id():
    with pytest.raises(ValueError):
        scanner.edit_candidate("does-not-exist", {"title": "X"})


def test_ignore_candidate_sets_status():
    cid = _seed_scan_candidate(status="ambiguous")
    scanner.ignore_candidate(cid)
    assert db.one("SELECT status FROM scan_candidates WHERE id=?", (cid,))["status"] == "ignored"


def test_ignore_candidate_raises_for_unknown_id():
    with pytest.raises(ValueError):
        scanner.ignore_candidate("does-not-exist")


def test_inspect_file_contains_evidence(tmp_path):
    """inspect_file must include an 'evidence' dict derived from path parsing."""
    p = tmp_path / "Author Name" / "Book Title.epub"
    p.parent.mkdir(parents=True)
    make_epub(p)
    detected = scanner.inspect_file(p, root=tmp_path)
    assert "evidence" in detected
    ev = detected["evidence"]
    assert ev["author"] == "Author Name"
    assert ev["title"] == "Book Title"
    assert "source" in ev
    assert ev["source"]["author"] == "folder"
    assert ev["source"]["title"] == "filename"


def test_inspect_file_evidence_overridden_by_embedded(tmp_path):
    """Embedded EPUB metadata overrides path evidence for title/authors,
    but path identifiers are retained alongside embedded ones."""
    p = tmp_path / "B00ABCDEFG" / "Book Title.epub"
    p.parent.mkdir(parents=True)
    make_epub(p, isbn="9780306406157")
    detected = scanner.inspect_file(p, root=tmp_path)
    # Embedded metadata overrides title/authors
    assert detected["title"] == "Test Book"
    assert detected["authors"] == ["Jane Author"]
    # Path evidence is still present
    assert "evidence" in detected
    assert detected["evidence"]["title"] == "Book Title"
    assert detected["evidence"]["author"] is None  # ASIN folder not author
    # Path ASIN is retained
    assert detected["identifiers"].get("asin") == "B00ABCDEFG"
    # Embedded ISBN is also present
    assert detected["identifiers"].get("isbn13") == "9780306406157"


def test_inspect_file_root_level_no_author(tmp_path):
    """A root-level file gets no author from inspect_file."""
    p = tmp_path / "Dune.epub"
    make_epub(p)
    detected = scanner.inspect_file(p, root=tmp_path)
    # Embedded metadata overrides title, but evidence shows path-derived title
    assert detected["evidence"]["author"] is None
    assert detected["evidence"]["title"] == "Dune"
    # The detected title comes from embedded metadata (Test Book)
    assert detected["title"] == "Test Book"
