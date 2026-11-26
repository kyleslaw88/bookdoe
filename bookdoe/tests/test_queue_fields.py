"""Management Queue page: the /api/v1/queue payload, the poll that fills it, and the page.

The payload is joined server side (work, series, authors, asset, release, indexer, client,
search, history). Missing joins must give nulls, never errors. The page tests run the real
app.js under node against a live server (same harness as test_download_cancel).
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, migrations
from app import main as main_module
from app.services import downloads, migration

import helpers
from test_download_cancel import FakeServer, _client_row, fake_server, UI_SETUP  # noqa: F401  (fixture)
from test_inbox_ui import needs_node, live, run_ui  # noqa: F401  (live is a fixture)

client = TestClient(main_module.app)


def _indexer(name="NZBgeek", kind="newznab") -> str:
    iid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO indexers(id,name,kind,base_url,created_at,updated_at) VALUES (?,?,?,?,?,?)", (iid, name, kind, "http://idx.test", t, t))
    return iid


def _release(work_id, indexer_id, *, automatic, kind="audiobook", fmt="m4b", size=500_000_000, score=88, title="Rel Title"):
    sid, rid, t = db.uid(), db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,'{}',?,?,'done')", (sid, work_id, kind, int(automatic), t))
        conn.execute(
            "INSERT INTO releases(id,search_id,work_id,asset_kind,indexer_id,protocol,raw_title,size_bytes,parsed_json,score,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, sid, work_id, kind, indexer_id, "usenet", title, size, json.dumps({"format": fmt}), score, t),
        )
    return rid


def _download(title, *, state="downloading", kind="audiobook", fmt="m4b", indexer="NZBgeek", client_kind="sabnzbd", automatic=True,
              progress=0.4, error=None, with_release=True, added_ago=3600, **cols):
    work_id = migration._find_or_create_stub({"title": title, "author": "Ann Author", "identifiers": {}})
    aid = helpers.want(work_id, kind)
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET format=? WHERE id=?", (fmt, aid))
    cid = _client_row(client_kind)
    rid = _release(work_id, _indexer(indexer), automatic=automatic, kind=kind, fmt=fmt, title=f"{title} [{fmt}] release") if with_release else None
    did, t = db.uid(), db.now()
    row = dict(id=did, work_id=work_id, asset_id=aid, release_id=rid, client_id=cid, client_download_id="j1", state=state,
               protocol="usenet" if client_kind in {"sabnzbd", "nzbget"} else "torrent", source_title=f"{title} source", progress=progress,
               error=error, added_at=t - added_ago, updated_at=t - 10, **cols)
    with db.tx() as conn:
        conn.execute(f"INSERT INTO downloads({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))  # nosec B608
    return did, work_id, aid, rid


def _by_id(scope="all", **params):
    r = client.get("/api/v1/queue", params={"scope": scope, **params})
    assert r.status_code == 200, r.text
    return {x["id"]: x for x in r.json()["items"]}, r.json()


def test_payload_has_every_new_field_and_keeps_the_old_ones():
    did, work_id, aid, rid = _download("Joined Book", speed_bps=2_500_000, eta_seconds=125, size_bytes=600_000_000, downloaded_bytes=240_000_000, grabbed_by="ada")
    with db.tx() as conn:
        sid = "s1"
        conn.execute("INSERT INTO series(id,name,created_at,updated_at) VALUES (?,?,?,?)", (sid, "Big Saga", 1, 1))
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (work_id, sid, "3"))
    db.add_history("release.grabbed", "Grabbed Joined Book", work_id=work_id, data={"download_id": did})
    x = _by_id()[0][did]
    for old in ("id", "work_id", "asset_id", "release_id", "client_id", "client_download_id", "state", "protocol", "source_title", "download_path", "progress", "error", "added_at", "updated_at", "title"):
        assert old in x
    assert x["title"] == "Joined Book" and x["work_title"] == "Joined Book"
    assert x["authors"] == ["Ann Author"] and x["series"] == {"name": "Big Saga", "position": "3"}
    assert (x["media_kind"], x["format"]) == ("audiobook", "m4b")
    assert x["release_title"] == "Joined Book [m4b] release" and x["release_score"] == 88
    assert x["indexer"] == "NZBgeek" and x["client"] == "sabnzbd-1" and x["client_kind"] == "sabnzbd" and x["protocol"] == "usenet"
    assert (x["size_bytes"], x["downloaded_bytes"], x["speed_bps"], x["eta_seconds"]) == (600_000_000, 240_000_000, 2_500_000, 125)
    assert x["progress_percent"] == 40.0 and x["state_label"] == "Downloading" and x["state_group"] == "active"
    assert x["started_at"] == x["added_at"] and x["finished_at"] is None and x["duration_seconds"] >= 3599
    assert x["automatic"] is True and x["grab_source"] == "automatic" and x["grabbed_by"] == "ada"
    assert [h["event_type"] for h in x["history"]] == ["release.grabbed"]


def test_ebook_torrent_manual_and_counts_per_state():
    a, *_ = _download("Torrent Ebook", kind="ebook", fmt="epub", indexer="Torz", client_kind="qbittorrent", automatic=False, state="grabbed")
    b, *_ = _download("Done One", state="completed", completed_at=db.now() - 5)
    c, *_ = _download("Bad One", state="import_failed", error="no managed root")
    d, *_ = _download("Imported One", state="imported", imported_at=db.now() - 1)
    e_, *_ = _download("Dead One", state="failed", error="Download failed in the client")
    items, body = _by_id()
    x = items[a]
    assert (x["media_kind"], x["format"], x["indexer"], x["client_kind"], x["protocol"]) == ("ebook", "epub", "Torz", "qbittorrent", "torrent")
    assert x["automatic"] is False and x["grab_source"] == "manual" and x["state_label"] == "Sent to client"
    assert items[c]["failure_reason"] == "no managed root" and items[c]["state_label"] == "Import failed"
    assert items[d]["progress_percent"] == 100.0 and items[d]["finished_at"] is not None and items[d]["state_group"] == "completed"
    assert items[e_]["state_group"] == "failed"
    assert body["counts"] == {"queued": 1, "active": 0, "completed": 2, "failed": 2}
    # Default scope is the old contents: nothing finished, failed or cancelled.
    open_ids = {r["id"] for r in client.get("/api/v1/queue").json()["items"]}
    assert {a, b, c} <= open_ids and not {d, e_} & open_ids
    hist = {r["id"] for r in client.get("/api/v1/queue", params={"scope": "history"}).json()["items"]}
    assert {d, e_} <= hist and not {a, b, c} & hist


def test_history_is_limited_and_never_deletes_rows():
    ids = [_download(f"H{i}", state="imported", imported_at=db.now())[0] for i in range(5)]
    r = client.get("/api/v1/queue", params={"scope": "history", "history_limit": 3}).json()["items"]
    assert len(r) == 3
    assert db.one("SELECT COUNT(*) n FROM downloads WHERE id IN (%s)" % ",".join("?" * 5), ids)["n"] == 5  # nosec B608


def test_missing_joins_give_nulls_not_errors():
    did, work_id, aid, rid = _download("Orphan Book")
    # Release, indexer, client, work and asset are all gone (ON DELETE SET NULL / cascade).
    with db.tx() as conn:
        conn.execute("DELETE FROM indexers")
        conn.execute("DELETE FROM releases WHERE id=?", (rid,))
        conn.execute("DELETE FROM download_clients")
        conn.execute("DELETE FROM works WHERE id=?", (work_id,))
    x = _by_id()[0][did]
    assert x["work_title"] is None and x["title"] is None and x["authors"] == [] and x["series"] is None
    assert x["media_kind"] is None and x["format"] is None and x["indexer"] is None and x["client"] is None
    assert x["automatic"] is None and x["grab_source"] is None and x["release_score"] is None
    assert x["release_title"] == "Orphan Book source" and x["size_bytes"] is None and x["downloaded_bytes"] is None
    assert x["destination"] is None and x["state_label"] == "Downloading"


def test_download_without_a_release_row_still_renders():
    did, *_ = _download("Hand Added", with_release=False)
    x = _by_id()[0][did]
    assert x["indexer"] is None and x["automatic"] is None and x["media_kind"] == "audiobook" and x["format"] == "m4b"


def test_destination_is_the_managed_root_or_the_imported_folder(tmp_path):
    did, work_id, aid, _ = _download("Dest Book")
    root = tmp_path / "lib"
    root.mkdir()
    rf, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'managed',?,?)", (rf, "Lib", str(root), "audiobook", t, t))
        conn.execute("UPDATE media_assets SET root_folder_id=? WHERE id=?", (rf, aid))
    assert _by_id()[0][did]["destination"] == str(root)
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET state='imported',imported_at=? WHERE id=?", (t, did))
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,discovered_at) VALUES (?,?,?,1,?)", (db.uid(), aid, str(root / "Ann" / "Dest Book" / "a.m4b"), t))
    assert _by_id()[0][did]["destination"] == str(root / "Ann" / "Dest Book")


def test_poll_stores_what_the_client_reports(fake_server):
    did, *_ = _download("Polled Torrent", client_kind="qbittorrent", state="downloading")
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET client_download_id='abc' WHERE id=?", (did,))
    fake_server(FakeServer(torrents=[{"hash": "abc", "name": "Polled Torrent source", "progress": 0.5, "state": "downloading", "save_path": "/dl", "size": 1000, "downloaded": 500, "dlspeed": 250, "eta": 2}]))
    downloads.poll_all()
    x = _by_id()[0][did]
    assert (x["size_bytes"], x["downloaded_bytes"], x["speed_bps"], x["eta_seconds"]) == (1000, 500, 250, 2)
    fake_server(FakeServer(torrents=[{"hash": "abc", "name": "Polled Torrent source", "progress": 0.1, "state": "stalledDL", "save_path": "/dl", "size": 1000, "downloaded": 100, "dlspeed": 0, "eta": 8640000}]))
    downloads.poll_all()
    assert _by_id()[0][did]["eta_seconds"] is None  # qBittorrent's "infinity" is not an ETA


def test_manual_grab_records_who_and_automatic_flag_follows_the_search(fake_server):
    fake_server(FakeServer())
    work_id = migration._find_or_create_stub({"title": "Grab Me", "author": "Ann Author", "identifiers": {}})
    helpers.want(work_id, "audiobook")
    _client_row("sabnzbd")
    rid = _release(work_id, _indexer(), automatic=False)
    res = downloads.grab_release(rid, grabbed_by="ada")
    x = _by_id()[0][res["download_id"]]
    assert x["grabbed_by"] == "ada" and x["grab_source"] == "manual" and x["size_bytes"] == 500_000_000


def test_migration_33_is_idempotent_and_additive():
    cols = {"size_bytes", "downloaded_bytes", "speed_bps", "eta_seconds", "grabbed_by"}
    with db.session() as conn:
        assert cols <= migrations._columns(conn, "downloads")
    with db.tx() as conn:
        again = migrations._migrate_downloads_client_stats(conn)
        assert again == {"columns_added": 0, "already_applied": 1}
        for c in cols:
            conn.execute(f"ALTER TABLE downloads DROP COLUMN {c}")
        first = migrations._migrate_downloads_client_stats(conn)
        assert first == {"columns_added": 5, "already_applied": 0}
        assert migrations._migrate_downloads_client_stats(conn)["already_applied"] == 1


# ---------- the page (real app.js against a live server) ----------
def _seed_page():
    _download("Alpha Audio", indexer="NZBgeek", state="downloading", progress=0.25, speed_bps=1_000_000, eta_seconds=300, size_bytes=400_000_000, automatic=True)
    _download("Bravo Ebook", kind="ebook", fmt="epub", indexer="Torz", client_kind="qbittorrent", state="grabbed", progress=0, automatic=False, grabbed_by="ada")
    _download("Charlie Broken", state="import_failed", error="no managed root", progress=1.0)
    _download("Delta Done", state="imported", imported_at=db.now(), progress=1.0)


@needs_node
def test_page_shows_the_new_columns_filters_and_details(live):
    _seed_page()
    out = run_ui(UI_SETUP + """
      await renderQueue();
      const html = els['#view'].innerHTML;
      const all = queueState.items.length;
      const pick = o => { Object.assign(queueState, {group: 'all', kind: '', indexer: '', client: '', q: ''}, o); return queueFiltered().map(x => x.work_title); };
      return {
        html,
        heads: ['Title', 'Type', 'Format', 'Indexer', 'Client', 'Size', 'Progress', 'State', 'Started', 'Updated', 'Source'].map(h => html.includes('<th>' + h + '</th>') || html.includes('">' + h + '</th>')),
        all,
        ebook: pick({kind: 'ebook'}), audio: pick({kind: 'audiobook'}).sort(), torz: pick({indexer: 'Torz'}), failed: pick({group: 'failed'}),
        active: pick({group: 'active'}), search: pick({q: 'charlie'}), authorSearch: pick({q: 'ann author'}).length,
        sortTitle: (Object.assign(queueState, {group: 'all', kind: '', indexer: '', client: '', q: '', sort: 'title', dir: 'asc'}), queueFiltered().map(x => x.work_title)),
        counts: queueState.counts,
      };
    """, live)
    html = out["html"]
    assert all(out["heads"]) and out["all"] == 4
    for text in ("Alpha Audio", "audiobook", "M4B", "NZBgeek", "sabnzbd-1", "25%", "ETA 5m 00s", "Automatic", "Manual", "Recently completed", "Downloading", "Import failed", "no managed root"):
        assert text in html, text
    assert 'class="queue-detail"' in html and "hidden" in html  # expander rows exist, collapsed
    assert "Bravo Ebook" in out["ebook"][0] and len(out["ebook"]) == 1
    assert out["audio"] == ["Alpha Audio", "Charlie Broken", "Delta Done"]
    assert out["torz"] == ["Bravo Ebook"] and out["failed"] == ["Charlie Broken"] and out["active"] == ["Alpha Audio"]
    assert out["search"] == ["Charlie Broken"] and out["authorSearch"] == 4
    assert out["sortTitle"] == ["Alpha Audio", "Bravo Ebook", "Charlie Broken", "Delta Done"]
    assert out["counts"] == {"queued": 1, "active": 1, "completed": 1, "failed": 1}


@needs_node
def test_page_remembers_the_selection_in_localstorage(live):
    _seed_page()
    out = run_ui(UI_SETUP + """
      const saved = {}; localStorage.setItem = (k, v) => saved[k] = v;
      await renderQueue();
      queueState.kind = 'ebook'; queueState.sort = 'size'; rememberQueueFilter();
      return saved['bookdoe-queue-filter'];
    """, live)
    assert json.loads(out)["kind"] == "ebook" and json.loads(out)["sort"] == "size"
