import os, sys, tempfile, zipfile, pathlib
root = os.environ.get("UI_SEED_ROOT") or os.path.join(tempfile.gettempdir(), "uitest", "run")
os.environ.update(
    BOOKDOE_DB_PATH=f"{root}/bookdoe.db",
    BOOKDOE_DATA_DIR=root,
    BOOKDOE_BACKUP_DIR=f"{root}/backups",
    BOOKDOE_COVER_DIR=f"{root}/covers",
    BOOKDOE_AUTH_MODE="none",
    BOOKDOE_SCHEDULER_ENABLED="0",
    BOOKDOE_SECRET_KEY="uitest-secret-key-long-enough",
)
pathlib.Path(root).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, ".")
from app import db
from app.services import migration, covers

db.init_db()

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 128

# 420 works so the library is bigger than one page, plus the reported title late in
# the alphabet and a second author.
for i in range(420):
    migration._find_or_create_stub({"title": f"Book {i:03d}", "author": "Craig Alanson", "identifiers": {}})
dead = migration._find_or_create_stub({"title": "Dead World", "author": "Craig Alanson", "identifiers": {}})
migration._find_or_create_stub({"title": "All Systems Red", "author": "Martha Wells", "identifiers": {}})

# Give Dead World an owned audiobook asset and a cached cover.
t = db.now()
aid = db.uid()
with db.tx() as conn:
    conn.execute(
        "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'audiobook','m4b','owned',1,?,?)",
        (aid, dead, t, t),
    )
covers._store(dead, PNG, "embedded")

# Downloads for the Queue page checks (ui-queue.mjs): active, queued, failed, finished;
# one ebook on a torrent client and a very long release title to prove nothing overflows.
for iname, ikind in (("NZBgeek", "newznab"), ("Torz", "torznab")):
    conn_args = (f"idx-{iname}", iname, ikind, "http://idx.invalid", t, t)
    with db.tx() as conn:
        conn.execute("INSERT INTO indexers(id,name,kind,base_url,created_at,updated_at) VALUES (?,?,?,?,?,?)", conn_args)
for cid, cname, ckind in (("cl-sab", "SABnzbd", "sabnzbd"), ("cl-qb", "qBittorrent", "qbittorrent")):
    with db.tx() as conn:
        conn.execute("INSERT INTO download_clients(id,name,kind,base_url,created_at,updated_at) VALUES (?,?,?,?,?,?)", (cid, cname, ckind, "http://dl.invalid", t, t))
LONG = "Craig.Alanson.Expeditionary.Force.Book.01.Columbus.Day.UNABRIDGED.Audiobook.M4B.64k.Retail.Narrated.By.RC.Bray." * 2
queue_rows = [
    ("q1", "Book 001", "audiobook", "m4b", "idx-NZBgeek", "cl-sab", "usenet", "downloading", 0.42, None, 1, LONG),
    ("q2", "Book 002", "ebook", "epub", "idx-Torz", "cl-qb", "torrent", "grabbed", 0.0, None, 0, "Book 002 epub"),
    ("q3", "Book 003", "audiobook", "m4b", "idx-NZBgeek", "cl-sab", "usenet", "import_failed", 1.0, "No managed root folder configured for audiobook", 1, "Book 003 m4b"),
    ("q4", "Book 004", "ebook", "epub", "idx-Torz", "cl-qb", "torrent", "imported", 1.0, None, 0, "Book 004 epub"),
]
for qid, title, kind, fmt, idx, cl, proto, state, prog, err, auto, raw in queue_rows:
    w = db.one("SELECT id FROM works WHERE title=?", (title,))["id"]
    a, sid, rid = db.uid(), db.uid(), db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,?,?,'downloading',1,?,?)", (a, w, kind, fmt, t, t))
        conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,'{}',?,?,'done')", (sid, w, kind, auto, t))
        conn.execute("INSERT INTO releases(id,search_id,work_id,asset_kind,indexer_id,protocol,raw_title,size_bytes,parsed_json,score,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, sid, w, kind, idx, proto, raw, 700_000_000, '{"format":"%s"}' % fmt, 91, t))
        conn.execute("INSERT INTO downloads(id,work_id,asset_id,release_id,client_id,client_download_id,state,protocol,source_title,progress,error,added_at,updated_at,size_bytes,downloaded_bytes,speed_bps,eta_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (qid, w, a, rid, cl, "job-" + qid, state, proto, raw, prog, err, t - 7200, t - 30, 700_000_000, int(700_000_000 * prog), 3_000_000 if state == "downloading" else None, 95 if state == "downloading" else None))
# Conversion (issue #265): a managed root holding a small MOBI (converts to EPUB with the pure-Python
# path, so no ffmpeg is needed) and an MP3 audiobook (never converted here; it only has to be a
# valid candidate for the bulk dialog). Generated with the test fixtures.
sys.path.insert(0, "tests")
import conv_fixtures as cf
lib = cf.root(pathlib.Path(root), "managed", "mixed", name="library")
mobi = cf.make_mobi(lib["path"] / "Anne Author" / "Tiny Book" / "Tiny Book.mobi", title="Tiny Book", author="Anne Author")
cf.add_book(lib, "Tiny Book", "Anne Author", "ebook", [mobi])
parts = []
for i in (1, 2):
    p = lib["path"] / "Anne Author" / "Spoken Tiny" / f"0{i}.mp3"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"ID3" + bytes([i]) * 2000)
    parts.append(p)
cf.add_book(lib, "Spoken Tiny", "Anne Author", "audiobook", parts)
# Read-only (adopt) root: an MP3 audiobook that converts INTO the managed "library" root, and a DRM-locked
# MOBI whose Convert control must show disabled with its reason (ui-convert.mjs).
legacy = cf.root(pathlib.Path(root), "adopt", "mixed", name="legacy")
ro_parts = []
for i in (1, 2):
    p = legacy["path"] / "Blake Crouch" / "Read Only Tiny" / f"0{i}.mp3"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"ID3" + bytes([i]) * 2000)
    ro_parts.append(p)
cf.add_book(legacy, "Read Only Tiny", "Blake Crouch", "audiobook", ro_parts)
locked = cf.make_mobi(legacy["path"] / "Anne Author" / "Locked Tiny" / "Locked Tiny.mobi", title="Locked Tiny", author="Anne Author", drm=True)
cf.add_book(legacy, "Locked Tiny", "Anne Author", "ebook", [locked])
# Compact file lists (feat/compact-file-lists): a 47-track MP3 audiobook (600 s a track) and an ebook
# whose folder holds BOTH an epub and a mobi (stored format mobi, as on production).
many = []
for i in range(1, 48):
    p = lib["path"] / "Anne Author" / "Many Tracks" / f"Track {i:02d}.mp3"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"ID3" + bytes([i]) * 2000)
    many.append(p)
_, many_asset = cf.add_book(lib, "Many Tracks", "Anne Author", "audiobook", many)
db.execute("UPDATE media_files SET duration_seconds=600 WHERE asset_id=?", (many_asset,))
both_mobi = cf.make_mobi(lib["path"] / "Anne Author" / "Both Formats" / "Both Formats.mobi", title="Both Formats", author="Anne Author")
both_epub = lib["path"] / "Anne Author" / "Both Formats" / "Both Formats.epub"
both_epub.write_bytes(b"PK\x03\x04" + b"\0" * 500)
cf.add_book(lib, "Both Formats", "Anne Author", "ebook", [both_mobi, both_epub], fmt="mobi")

# Duplicate works (ui-merge.mjs): the owner's 39 Clues examples (each of two books listed twice, the second copy
# with the series baked into its title) and a book whose merged-in copy left the same epub twice in a managed
# root. Nothing here is merged by the seed; the browser check merges, undoes and cleans up.
import hashlib
rick = "Rick Riordan"
clues = {}
for title, pos in (("The Maze of Bones", "1"), ("The sword thief", "3"), ("The Sword Thief (The 39 Clues, #3)", None),
                   ("The Black Circle", "5"), ("The Black Circle (The 39 Clues, #5)", None)):
    clues[title] = (migration._find_or_create_stub({"title": title, "author": rick, "identifiers": {}}), pos)
sid = db.uid()
with db.tx() as conn:
    conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, "The 39 Clues", "39 Clues", t, t))
    for wid, pos in clues.values():
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (wid, sid, pos))
    for title, kind, fmt in (("The Sword Thief (The 39 Clues, #3)", "audiobook", "m4b"), ("The Black Circle (The 39 Clues, #5)", "ebook", "epub")):
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,?,?,'owned',1,?,?)",
                     (db.uid(), clues[title][0], kind, fmt, t, t))
for title, kind, fmt in (("Twin Tale", "ebook", "epub"), ("Twin Tale (Twins, #2)", "audiobook", "m4b")):   # both owned: the library's select mode lists owned books only
    wid = migration._find_or_create_stub({"title": title, "author": "Sam Twin", "identifiers": {}})
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,?,?,'owned',1,?,?)", (db.uid(), wid, kind, fmt, t, t))
kid = db.uid()    # a kid who may open the second Twin Tale record only: merging must ask before widening that
with db.tx() as conn:
    conn.execute("INSERT INTO users(id,username,is_admin,access_scope,created_at,updated_at) VALUES (?,?,0,'kids',?,?)", (kid, "Tiny Reader", t, t))
    conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,?)",
                 (kid, db.one("SELECT id FROM works WHERE title='Twin Tale (Twins, #2)'")["id"], t))
from app.services import tagging
kids_tag = db.one("SELECT id FROM tags WHERE name='kids'")["id"]
def owned_pair(titles, author, shelf=(), grant=()):
    ids = {}
    for title in titles:
        ids[title] = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
        with db.tx() as conn:
            conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','epub','owned',1,?,?)", (db.uid(), ids[title], t, t))
        if title in shelf:
            tagging.add_tag(ids[title], kids_tag)
        if title in grant:
            with db.tx() as conn:
                conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,?)", (kid, ids[title], t))
    return ids
owned_pair(["Grant Tale", "Grant Tale (Grants, #1)"], "Gina Grant", grant=("Grant Tale",))                                  # the kid sees the clean record only
owned_pair(["Union Tale", "Union Tale, Book 1", "Union Tale (Unions, #1)"], "Una Union", shelf=("Union Tale, Book 1",))          # a kids record and two adult ones
owned_pair(["Shelf Tale", "Shelf Tale (Shelf, #1)"], "Sheldon Shelf", shelf=("Shelf Tale", "Shelf Tale (Shelf, #1)"))       # both on the Kids shelf
dune = migration._find_or_create_stub({"title": "Dune Twice", "author": "Frank Herbert", "identifiers": {}})
dune_asset = db.uid()
with db.tx() as conn:
    conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,root_folder_id,added_at,updated_at) VALUES (?,?,'ebook','epub','owned',?,?,?)",
                 (dune_asset, dune, lib["id"], t, t))
    for rel in ("Frank Herbert/Dune Twice/Dune Twice.epub", "Frank Herbert/Dune Twice/copy/Dune Twice.epub"):
        p = lib["path"] / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = b"PK dune twice " * 300
        p.write_bytes(data)
        conn.execute("INSERT INTO media_files(id,asset_id,path,relative_path,size_bytes,sha256,managed,discovered_at) VALUES (?,?,?,?,?,?,1,?)",
                     (db.uid(), dune_asset, str(p), rel, len(data), hashlib.sha256(data).hexdigest(), t))

# Bulk duplicates (ui-duplicates.mjs): file-only Works (owned files, "From file") each with a provider-backed twin (metadata_id,
# description, no files): the pair the owner merges by hand today. The browser check scans, selects all High, dry-runs, bulk merges and
# undoes the batch. Plus the State filter shapes: EPUB owned + a monitored audiobook placeholder (partly owned) and an owned
# ebook beside a redundant empty ebook placeholder (complete).
for n, (title, author) in enumerate((("Because of Winn-Dixie", "Kate DiCamillo"), ("Holes", "Louis Sachar"), ("Hatchet", "Gary Paulsen"))):
    prov = db.uid(); fw = db.uid()
    with db.tx() as conn:
        for wid, mid, src, desc in ((prov, f"ui-prov-{n}", "provider", "A description long enough to count as real provider metadata for this seeded book."), (fw, None, "local", None)):
            conn.execute("INSERT INTO works(id,title,metadata_id,metadata_source,description,added_at,updated_at) VALUES (?,?,?,?,?,?,?)", (wid, title, mid, src, desc, t, t))
            conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)", (wid, db.contributor_id_for_name(conn, author)))
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','epub','owned',0,?,?)", (db.uid(), fw, t, t))
shape = migration._find_or_create_stub({"title": "State Shape Partly", "author": "Pat Partial", "identifiers": {}})
shape2 = migration._find_or_create_stub({"title": "State Shape Redundant", "author": "Rhea Redundant", "identifiers": {}})
with db.tx() as conn:
    conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','epub','owned',1,?,?)", (db.uid(), shape, t, t))
    conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,'audiobook',NULL,'missing',1,1,?,?)", (db.uid(), shape, t, t))
    conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','epub','owned',1,?,?)", (db.uid(), shape2, t, t))
    conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,'ebook',NULL,'missing',1,1,?,?)", (db.uid(), shape2, t, t))
# Managed library build (ui-managed-build.mjs): an ADOPTED (read-only) root holding an EPUB, a two-file M4B audiobook and a PDF.
# The browser check runs a dry run, confirms with the typed count, builds into the managed "library" root (the mixed
# managed root above receives both kinds), checks the book page and Overview, then undoes it, so it can run at every width.
adopted = cf.root(pathlib.Path(root), "adopt", "mixed", name="adopted-books")
bb = adopted["path"] / "Brian Build"
(bb / "Copy Me").mkdir(parents=True, exist_ok=True)
(bb / "Copy Me" / "Copy Me.epub").write_bytes(b"PK copy me " * 400)
cf.add_book(adopted, "Copy Me", "Brian Build", "ebook", [bb / "Copy Me" / "Copy Me.epub"])
(bb / "Listen Here").mkdir(parents=True, exist_ok=True)
tracks = []
for i in (1, 2):
    tp = bb / "Listen Here" / f"Part {i}.m4b"
    tp.write_bytes(bytes([i]) * 3000)
    tracks.append(tp)
cf.add_book(adopted, "Listen Here", "Brian Build", "audiobook", tracks)
(bb / "Scanned").mkdir(parents=True, exist_ok=True)
(bb / "Scanned" / "Scanned.pdf").write_bytes(b"%PDF-1.4 " * 200)
cf.add_book(adopted, "Scanned", "Brian Build", "ebook", [bb / "Scanned" / "Scanned.pdf"])
db.execute("UPDATE media_files SET adopted=1, managed=0 WHERE path LIKE ?", (str(adopted["path"]) + "/%",))
print("SEEDED", dead)
