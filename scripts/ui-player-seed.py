"""Seed a throwaway instance for scripts/ui-player-e2e.mjs: local auth with an admin,
an adult and a kid; three owned works with real cover art (one on the kids shelf, one
tagged 'classics'), and a real minimal EPUB the reader can open, and two real audiobooks (one on the kids shelf). Prints JSON the e2e
script reads (credentials are test-only). Environment is set by ui-gate.mjs."""
import io
import json
import os
import pathlib
import sys
import wave
import zipfile

root = os.environ["UI_SEED_ROOT"]
pathlib.Path(root).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, ".")
from app import auth, db  # noqa: E402
from app.services import covers, migration, tagging  # noqa: E402

import helpers_seed  # noqa: E402  (same dir as this script when run from bookdoe/, see ui-gate)

db.init_db()
PW = "ui-e2e-password-long-enough"
admin = auth.create_local_user("ada", PW, is_admin=True)
adult = auth.create_local_user("jane", PW, is_admin=False, access_scope="full")
kid = auth.create_local_user("owen", PW, is_admin=False, access_scope="kids")

# A real EPUB (mimetype, container, package, one chapter) so epub.js renders text.
books = pathlib.Path(root) / "books"
books.mkdir(exist_ok=True)
epub = books / "Readable.epub"
# Absolute publisher sizes, as Calibre conversions often emit: the reader's text-size
# control must still win over them.
chapter = "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>One</title><style>body{font-size:12pt} p{font-size:12pt;margin:0 0 1em}</style></head><body>" + "".join(
    f"<p id='p{i}'>Paragraph {i}. The quick brown fox jumps over the lazy dog, again and again.</p>" for i in range(60)) + "</body></html>"
with zipfile.ZipFile(epub, "w") as z:
    z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml", "<?xml version='1.0'?><container version='1.0' xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/></rootfiles></container>")
    z.writestr("OEBPS/content.opf", "<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='id'><metadata xmlns:dc='http://purl.org/dc/elements/1.1/'><dc:identifier id='id'>urn:uuid:e2e</dc:identifier><dc:title>Readable</dc:title><dc:language>en</dc:language></metadata><manifest><item id='c1' href='c1.xhtml' media-type='application/xhtml+xml'/><item id='nav' href='nav.xhtml' media-type='application/xhtml+xml' properties='nav'/></manifest><spine><itemref idref='c1'/></spine></package>")
    z.writestr("OEBPS/nav.xhtml", "<html xmlns='http://www.w3.org/1999/xhtml' xmlns:epub='http://www.idpf.org/2007/ops'><body><nav epub:type='toc'><ol><li><a href='c1.xhtml'>One</a></li></ol></nav></body></html>")
    z.writestr("OEBPS/c1.xhtml", chapter)

t = db.now()
with db.tx() as conn:
    conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES ('r1','Books',?,'ebook','adopt',?,?)", (str(books), t, t))

classics = tagging.create_tag("classics")
ids = {}
for title, shelf, tag in (("Readable Kids Book", True, None), ("Grown-up Classic", False, classics), ("Plain Novel", False, None)):
    wid = migration._find_or_create_stub({"title": title, "author": "E2E Author", "identifiers": {}})
    aid = helpers_seed.own(wid)
    covers._store(wid, helpers_seed.png(title), "embedded")
    if shelf:
        tagging.add_tag(wid, tagging.kids_tag_id())
    if tag:
        tagging.add_tag(wid, tag)
    ids[title] = wid
    if title == "Readable Kids Book":
        with db.tx() as conn:
            conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES ('f1',?,?,?,'{}',1,0,0,?)", (aid, str(epub), epub.stat().st_size, t))
        ids["asset"] = aid
# Audiobook fixtures (real, playable WAVs, 8 kHz 8-bit mono so they stay small): one on
# the kids shelf for the removal/re-add lifecycle test, one for admin screenshots of the
# audio controls. A short tone is enough for <audio> to report a duration. They have covers
# (the lifecycle test checks the worker re-stores covers after a re-add), so the cover
# counts in ui-player-e2e.mjs include them.
def _wav(path, seconds):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(1); w.setframerate(8000)
        w.writeframes(bytes(128 + (12 if (i // 40) % 2 else -12) for i in range(8000)) * seconds)

audio = {}
for title, shelf, seconds in (("Bedtime Audiobook", True, 240), ("Grown-up Audiobook", False, 200)):
    wid = migration._find_or_create_stub({"title": title, "author": "E2E Narrator Author", "identifiers": {}})
    aid = helpers_seed.own(wid, kind="audiobook", fmt="wav")
    path = books / f"{title.replace(' ', '_')}.wav"
    _wav(path, seconds)
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,duration_seconds,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,?,'{}',1,0,0,?)",
                     (db.uid(), aid, str(path), path.stat().st_size, seconds, t))
    covers._store(wid, helpers_seed.png(title), "embedded")
    if shelf:
        tagging.add_tag(wid, tagging.kids_tag_id())
    audio[title] = {"work": wid, "asset": aid}
ids["audio"] = audio
# Kids-shelf cleanup fixtures: one book the classifier auto-approved that is not for kids
# ("Backyard Starship") and three more for the bulk removal. No covers on purpose, so the
# cover counts in ui-player-e2e stay about the three books above.
tagging.set_auto_approve(enabled=False)
cleanup = {}
for title in ("Backyard Starship", "Bulk Kid 1", "Bulk Kid 2", "Bulk Kid 3"):
    wid = migration._find_or_create_stub({"title": title, "author": "E2E Author", "identifiers": {}})
    helpers_seed.own(wid)
    tagging.add_tag(wid, tagging.kids_tag_id(), source="llm", confidence=0.9)
    tagging.set_kids_decision(wid, "on", via="auto_approved", by="classifier")
    cleanup[title] = wid
ids["cleanup"] = cleanup
# Re-check fixtures (docs: Management > Settings > Tags > "Re-check auto-approved books"): three old
# auto-approvals with a blurb but no audience evidence, and one that passes the gate.
recheck = {}
for title, publisher, conf in (("Recheck Adult 1", None, 0.9), ("Recheck Adult 2", None, 0.9), ("Recheck Adult 3", None, 0.9), ("Recheck Pass", "Scholastic", 0.97)):
    wid = migration._find_or_create_stub({"title": title, "author": f"Author of {title}", "identifiers": {}})
    helpers_seed.own(wid)
    t0 = db.now()
    with db.tx() as conn:
        conn.execute("UPDATE works SET description=? WHERE id=?", ("A boy inherits a battered starship and an AI companion and sets out on a sci-fi adventure across the nebula.", wid))
        if publisher:
            conn.execute("INSERT INTO editions(id,work_id,publisher,created_at,updated_at) VALUES (?,?,?,?,?)", (db.uid(), wid, publisher, t0, t0))
        conn.execute("INSERT INTO kids_classification(work_id,verdict,confidence,reason,checked_at) VALUES (?,'kids',?,'aligns with middle-grade fiction',?)", (wid, conf, t0))
    tagging.add_tag(wid, tagging.kids_tag_id(), source="llm", confidence=conf)
    tagging.set_kids_decision(wid, "on", via="auto_approved", by=None, note=json.dumps({"confidence": conf, "model": "fixture"}))
    recheck[title] = wid
ids["recheck"] = recheck
# Inbox fixtures: three kids-shelf proposals waiting (auto-approval off so they stay)
# and one unresolved-metadata task that a kids bulk action must never touch.
tagging.set_auto_approve(enabled=False)
proposals = []
for n in range(3):
    wid = migration._find_or_create_stub({"title": f"Proposed Kids {n}", "author": "E2E Author", "identifiers": {}})
    helpers_seed.own(wid)
    tid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,'kids_tag','work',?,?,?,'open',40,?)",
                     (tid, wid, f"Proposed Kids {n}", json.dumps({"verdict": "kids", "confidence": 0.9, "reason": "fixture", "model": "fixture"}), t))
    proposals.append(wid)
meta_wid = migration._find_or_create_stub({"title": "Unmatched Import", "author": "E2E Author", "identifiers": {}})
with db.tx() as conn:
    conn.execute("INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,'metadata_match','work',?,?,'{}','open',0,?)",
                 (db.uid(), meta_wid, "Unmatched Import", t))
# Send-to-Kindle against the e2e SMTP sink (scripts/ui-smtp-sink.py) when the gate runs one.
if os.environ.get("UI_SMTP_PORT"):
    from app.services import devices as devices_svc
    devices_svc.set_smtp_config(host="127.0.0.1", port=int(os.environ["UI_SMTP_PORT"]), username="", password=None, from_email="bookdoe@e2e.test", use_tls=False)
# Series fixtures (series navigation / series-level actions): a three-book series nobody
# has put on the kids shelf yet (positions 1, 2, 4: one numbering gap) and a separate series
# whose only book is hidden from kids. No covers, so cover counts elsewhere are unchanged.
def _series(name, members):
    sid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,NULL,?,?,?,?)", (sid, name, name, t, t))
        for wid, pos in members:
            conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (wid, sid, pos))
    return sid
sw = {}
for title in ("Series Kid 1", "Series Kid 2", "Series Kid 4", "Series Hidden 1"):
    wid = migration._find_or_create_stub({"title": title, "author": "E2E Author", "identifiers": {}})
    helpers_seed.own(wid)
    sw[title] = wid
sw["series_id"] = _series("E2E Daniel X", [(sw["Series Kid 1"], "1"), (sw["Series Kid 2"], "2"), (sw["Series Kid 4"], "4")])
sw["other_series_id"] = _series("E2E Hidden Saga", [(sw["Series Hidden 1"], "1")])
ids["series"] = sw
# A book with many files per asset (regression: one "Keep offline" per asset, not per file):
# an audiobook asset of 12 tracks and an ebook asset of 2 files (.epub and .mobi). No cover.
mwid = migration._find_or_create_stub({"title": "Many Files Book", "author": "E2E Author", "identifiers": {}})
maid = helpers_seed.own(mwid, kind="audiobook", fmt="mp3")
meaid = helpers_seed.own(mwid, kind="ebook", fmt="epub")
for n in range(12):
    tp = books / f"Many_Files_{n:02d}.mp3"; _wav(tp, 2)
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,duration_seconds,part_number,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,2,?,'{}',1,0,0,?)", (db.uid(), maid, str(tp), tp.stat().st_size, n + 1, t))
for ext in ("epub", "mobi"):
    ep = books / f"Many_Files.{ext}"; ep.write_bytes(epub.read_bytes() if ext == "epub" else b"BOOKMOBI" + b"x" * 200)
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,'{}',1,0,0,?)", (db.uid(), meaid, str(ep), ep.stat().st_size, t))
ids["multi"] = {"work": mwid, "audio_asset": maid, "ebook_asset": meaid}
ids["proposals"] = proposals
ids["meta"] = meta_wid
print("E2E " + json.dumps({"password": PW, "works": ids}))
