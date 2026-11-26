"""File-quality maintenance detectors (P5-20): invalid EPUBs, damaged audio, chapters, covers, embedded metadata."""
from __future__ import annotations

import zipfile
from pathlib import Path

from app import db
from app.services import covers, maintenance
from app.services.detectors_quality import BATCH_SETTING

DETECTORS = ("invalid-epub", "damaged-audio", "missing-chapters", "missing-cover", "missing-embedded-metadata")
CONTAINER = (b'<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             b'<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
OPF = (b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
       b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">%s</metadata></package>')
TAGS = b"<dc:title>Dune</dc:title><dc:creator>Frank Herbert</dc:creator>"


def make_epub(path: Path, *, opf: bytes = OPF % TAGS, container: bytes | None = CONTAINER, mimetype_first: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        if mimetype_first:
            zf.writestr("mimetype", "application/epub+zip")
        if container is not None:
            zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", opf)
        if not mimetype_first:
            zf.writestr("mimetype", "application/epub+zip")
    return path


def seed(path: Path, *, kind: str = "ebook", status: str = "owned", asset_id: str | None = None, cover_url: str | None = "http://c/x.jpg",
         **cols) -> tuple[str, str, str]:
    """A root-contained work/asset/file row; ``cols`` are extra media_files columns. Returns (work, asset, file) ids."""
    t = db.now()
    wid, fid = db.uid(), db.uid()
    with db.tx() as c:
        if not db.one("SELECT 1 FROM root_folders WHERE path=?", (str(path.parent),)):
            c.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)"
                      " VALUES (?,?,?,?,'adopt',1,1,?,?)", (db.uid(), path.parent.name, str(path.parent), kind, t, t))
        c.execute("INSERT INTO works(id,title,cover_url,added_at,updated_at) VALUES (?,?,?,?,?)", (wid, f"Book {path.stem}", cover_url, t, t))
        if asset_id is None:
            asset_id = db.uid()
            c.execute("INSERT INTO media_assets(id,work_id,kind,format,status,added_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                      (asset_id, wid, kind, path.suffix.lstrip("."), status, t, t))
        names = ["id", "asset_id", "path", "missing", "discovered_at", *cols]
        c.execute(f"INSERT INTO media_files({','.join(names)}) VALUES ({','.join('?' * len(names))})",  # nosec B608
                  (fid, asset_id, str(path), 0, t, *cols.values()))
    return wid, asset_id, fid


def run(*names: str) -> dict:
    return maintenance.run_all(names or DETECTORS)


def open_issues(kind: str) -> list[dict]:
    return db.all("SELECT * FROM maintenance_issues WHERE kind=? AND status='open' ORDER BY subject_id", (kind,))


def test_truncated_epub_is_invalid_and_valid_epub_is_not(tmp_path):
    good = make_epub(tmp_path / "good.epub")
    bad = make_epub(tmp_path / "bad.epub")
    bad.write_bytes(bad.read_bytes()[:60])  # cut inside the zip: no central directory
    _, _, good_id = seed(good, fingerprint="g")
    _, _, bad_id = seed(bad, fingerprint="b")
    result = run("invalid-epub")
    assert result["complete"]
    (issue,) = open_issues("invalid_epub")
    assert issue["subject_id"] == bad_id and issue["severity"] == "error"
    assert "not a readable zip" in db.json_load(issue["detail_json"], {})["problems"][0]
    assert good_id != issue["subject_id"]


def test_epub_structure_problems_are_each_reported(tmp_path):
    cases = {
        "nocontainer": make_epub(tmp_path / "nocontainer.epub", container=None),
        "latemime": make_epub(tmp_path / "latemime.epub", mimetype_first=False),
        "badopf": make_epub(tmp_path / "badopf.epub", opf=b"<package><metadata>"),
    }
    ids = {name: seed(p, fingerprint=name)[2] for name, p in cases.items()}
    run("invalid-epub")
    found = {i["subject_id"]: db.json_load(i["detail_json"], {})["problems"] for i in open_issues("invalid_epub")}
    assert set(found) == set(ids.values())
    assert "container.xml is missing" in found[ids["nocontainer"]][0]
    assert "mimetype is not the first" in found[ids["latemime"]][0]
    assert "does not parse" in found[ids["badopf"]][0]


def test_epub_result_is_cached_and_a_rerun_does_not_reopen_the_file(tmp_path):
    p = make_epub(tmp_path / "a.epub")
    _, _, fid = seed(p, fingerprint="f1")
    run("invalid-epub")
    row = db.one("SELECT integrity_detail_json FROM media_files WHERE id=?", (fid,))
    assert db.json_load(row["integrity_detail_json"], {})["epub"]["ok"] is True
    p.write_bytes(b"not a zip now")  # cached result still answers while the stored fingerprint is unchanged
    assert run("invalid-epub")["opened"] == 0
    with db.tx() as c:
        c.execute("UPDATE media_files SET fingerprint='f2' WHERE id=?", (fid,))  # file changed: checked again
    run("invalid-epub")
    assert [i["subject_id"] for i in open_issues("invalid_epub")] == [fid]


def test_fixed_epub_auto_resolves(tmp_path):
    p = make_epub(tmp_path / "a.epub")
    p.write_bytes(p.read_bytes()[:60])
    _, _, fid = seed(p, fingerprint="f1")
    run("invalid-epub")
    assert len(open_issues("invalid_epub")) == 1
    make_epub(p)
    with db.tx() as c:
        c.execute("UPDATE media_files SET fingerprint='f2' WHERE id=?", (fid,))
    run("invalid-epub")
    assert open_issues("invalid_epub") == []
    assert db.one("SELECT status FROM maintenance_issues WHERE kind='invalid_epub'")["status"] == "resolved"


def test_cursor_advances_and_successive_runs_cover_every_epub(tmp_path):
    ids = [seed(make_epub(tmp_path / f"{n}.epub"), fingerprint=str(n))[2] for n in range(5)]
    db.set_setting(BATCH_SETTING, 2)

    def checked() -> set[str]:
        return {r["id"] for r in db.all("SELECT id,integrity_detail_json j FROM media_files") if "epub" in db.json_load(r["j"], {})}

    run("invalid-epub")
    first = checked()
    run("invalid-epub")
    second = checked()
    assert len(first) == 2 and len(second) == 4 and first < second
    run("invalid-epub")
    assert checked() == set(ids)
    assert db.get_setting("maintenance.quality_cursor.epub") in ids


def test_damaged_integrity_state_yields_damaged_audio(tmp_path):
    m4b = tmp_path / "a.m4b"
    m4b.write_bytes(b"x")
    checks = [{"name": "container", "ok": False, "error": "ffprobe failed or duration is 0"}]
    _, _, fid = seed(m4b, kind="audiobook", integrity_state="damaged", integrity_detail_json=db.json_dump({"checks": checks}))
    ok = tmp_path / "b.m4b"
    ok.write_bytes(b"x")
    seed(ok, kind="audiobook", integrity_state="ok")
    run("damaged-audio")
    (issue,) = open_issues("damaged_audio")
    assert issue["subject_id"] == fid and issue["severity"] == "error"
    assert db.json_load(issue["detail_json"], {})["failed_checks"] == checks


def test_never_verified_audio_is_verified_within_the_batch_and_junk_is_damaged(tmp_path):
    junk = [tmp_path / f"{n}.mp3" for n in range(3)]
    ids = [seed(p, kind="audiobook")[2] for p in junk]
    for p in junk:
        p.write_bytes(b"this is not audio")
    db.set_setting(BATCH_SETTING, 2)
    run("damaged-audio")
    states = [db.one("SELECT integrity_state s FROM media_files WHERE id=?", (i,))["s"] for i in ids]
    assert states.count(None) == 1  # only a batch was probed
    run("damaged-audio")
    assert all(db.one("SELECT integrity_state s FROM media_files WHERE id=?", (i,))["s"] == "damaged" for i in ids)
    assert {i["subject_id"] for i in open_issues("damaged_audio")} == set(ids)


def test_missing_chapters_only_for_long_single_file_m4b(tmp_path):
    def m4b(name: str, **cols) -> str:
        p = tmp_path / name
        p.write_bytes(b"x")
        return seed(p, kind="audiobook", **cols)

    flagged = m4b("long.m4b", duration_seconds=7200, chapter_count=0)[2]
    one = m4b("one.m4b", duration_seconds=7200, chapter_count=1)[2]
    m4b("short.m4b", duration_seconds=1800, chapter_count=0)
    m4b("chapters.m4b", duration_seconds=7200, chapter_count=12)
    m4b("unprobed.m4b", duration_seconds=7200)  # chapter_count NULL: unknown
    _, asset, _ = m4b("part1.m4b", duration_seconds=7200, chapter_count=0)
    seed(tmp_path / "part2.m4b", kind="audiobook", asset_id=asset, duration_seconds=7200, chapter_count=0)
    run("missing-chapters")
    assert {i["subject_id"] for i in open_issues("missing_chapters")} == {flagged, one}
    assert {i["severity"] for i in open_issues("missing_chapters")} == {"warning"}


def test_missing_embedded_cover_is_info_and_null_is_unknown(tmp_path):
    a, b, c = (tmp_path / n for n in ("none.m4b", "has.m4b", "null.m4b"))
    for p in (a, b, c):
        p.write_bytes(b"x")
    nocover = seed(a, kind="audiobook", has_embedded_cover=0)[2]
    seed(b, kind="audiobook", has_embedded_cover=1)
    seed(c, kind="audiobook")
    epub = make_epub(tmp_path / "e.epub")
    epub_id = seed(epub, has_embedded_cover=0)[2]
    run("missing-cover")
    issues = open_issues("missing_cover")
    assert {i["subject_id"] for i in issues} == {nocover, epub_id}
    assert {(i["severity"], i["repair_action"]) for i in issues} == {("info", "embed_cover")}


def test_owned_work_with_no_cover_anywhere_is_reported_until_a_cover_is_cached(tmp_path):
    p = tmp_path / "a.epub"
    make_epub(p)
    wid, _, _ = seed(p, cover_url=None)
    seed(make_epub(tmp_path / "b.epub"), cover_url="http://c/y.jpg")  # has a cover_url
    seed(make_epub(tmp_path / "c.epub"), cover_url="", status="wanted")  # not owned
    run("missing-cover")
    (issue,) = open_issues("missing_cover")
    assert (issue["subject_type"], issue["subject_id"], issue["severity"]) == ("work", wid, "warning")
    covers.set_manual(wid, b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    run("missing-cover")
    assert open_issues("missing_cover") == []


def test_missing_embedded_metadata_for_audio_and_epub(tmp_path):
    def audio(name: str, tags: dict | None, **cols) -> str:
        p = tmp_path / name
        p.write_bytes(b"x")
        emb = db.json_dump({"embedded_tags": tags} if tags else {"title": "guessed from path"})
        return seed(p, kind="audiobook", embedded_json=emb, **cols)[2]

    full = audio("full.m4b", {"title": "T", "artist": "A"}, has_embedded_cover=1)
    no_title = audio("nt.m4b", {"artist": "A"}, has_embedded_cover=1)
    no_author = audio("na.m4b", {"title": "T", "album": "x"}, has_embedded_cover=1)
    untagged = audio("bare.m4b", None, has_embedded_cover=0)
    audio("unprobed.m4b", None)  # never probed: unknown
    good = seed(make_epub(tmp_path / "good.epub"), fingerprint="g")[2]
    notitle = seed(make_epub(tmp_path / "nt.epub", opf=OPF % b"<dc:creator>X</dc:creator>"), fingerprint="n")[2]
    run("invalid-epub", "missing-embedded-metadata")
    found = {i["subject_id"]: (db.json_load(i["detail_json"], {})["missing"], i["repair_action"]) for i in open_issues("missing_embedded_metadata")}
    assert found == {
        no_title: (["title"], "retag"), no_author: (["author"], "retag"),
        untagged: (["title", "author"], "retag"), notitle: (["title"], "retag"),
    }
    assert full not in found and good not in found


def test_unknown_files_are_counted_in_the_summary_not_reported(tmp_path):
    p = tmp_path / "a.m4b"
    p.write_bytes(b"x")
    seed(p, kind="audiobook")
    seed(make_epub(tmp_path / "e.epub"), fingerprint="e")
    run()  # the junk audio file gets verified during the run (damaged); stream columns stay unprobed
    unknown = maintenance.summary()["unknown"]
    assert unknown == {"audio_not_probed": 1, "audio_not_verified": 0, "epub_not_checked": 0}
    assert open_issues("missing_chapters") == [] and open_issues("missing_cover") == [] and open_issues("missing_embedded_metadata") == []
