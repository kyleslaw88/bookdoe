import sqlite3
from pathlib import Path
import zipfile

import pytest

from app import db
from app.services import migration


def make_readarr(path: Path, root: Path, media_file: Path):
    c=sqlite3.connect(path)
    c.executescript('''
      CREATE TABLE Authors(Id INTEGER PRIMARY KEY, Name TEXT);
      CREATE TABLE Books(Id INTEGER PRIMARY KEY, AuthorId INTEGER, Title TEXT, Monitored INTEGER, ForeignBookId INTEGER);
      CREATE TABLE Editions(Id INTEGER PRIMARY KEY, BookId INTEGER, Isbn13 TEXT, ASIN TEXT);
      CREATE TABLE BookFiles(Id INTEGER PRIMARY KEY, BookId INTEGER, Path TEXT, Size INTEGER);
      CREATE TABLE RootFolders(Id INTEGER PRIMARY KEY, Path TEXT, Name TEXT);
    ''')
    c.execute("INSERT INTO Authors VALUES (1,'Jane Author')")
    c.execute("INSERT INTO Books VALUES (10,1,'Test Book',1,999)")
    c.execute("INSERT INTO Editions VALUES (20,10,'9781234567897',NULL)")
    c.execute("INSERT INTO BookFiles VALUES (30,10,?,?)",(str(media_file),media_file.stat().st_size))
    c.execute("INSERT INTO RootFolders VALUES (40,?,'Books')",(str(root),))
    c.commit(); c.close()


def seed_local_work():
    wid,eid,cid,t=db.uid(),db.uid(),db.uid(),db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)",(wid,'Test Book','Test Book',t,t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",(cid,'Jane Author','Author, Jane',t,t))
        c.execute("INSERT INTO work_contributors VALUES (?,?, 'author',0)",(wid,cid))
        c.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",(eid,wid,'Test Book',t,t))
        c.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('edition',?,'isbn13','9781234567897','test',1,1)",(eid,))
    return wid


def test_readarr_migration_is_read_only_and_adopts_in_place(tmp_path, monkeypatch):
    media=tmp_path/'book.epub'; media.write_bytes(b'not-a-real-epub-but-stable')
    source=tmp_path/'readarr.db'; make_readarr(source,tmp_path,media)
    before_db=source.read_bytes(); before_media=media.read_bytes(); wid=seed_local_work()
    # Seed a managed root so the free-space check has a target.
    t = db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,enabled,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (db.uid(), 'Test Root', str(tmp_path), 'ebook', 'managed', 1, t, t),
        )
    a=migration.analyze_readarr(str(source),'ebook','Readarr Ebook')
    r=migration.reconcile_run(a['run_id'],use_network=False)
    assert r['resolved']==1
    monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: 10 * 1024**3)
    x=migration.execute_run(a['run_id'])
    assert x['files']==1
    assert source.read_bytes()==before_db
    assert media.read_bytes()==before_media and media.exists()
    mf=db.one("SELECT * FROM media_files WHERE path=?",(str(media),))
    assert mf and mf['adopted']==1 and mf['managed']==0
    asset=db.one("SELECT * FROM media_assets WHERE id=?",(mf['asset_id'],))
    assert asset['work_id']==wid


def seed_managed_root(path, media_kind='ebook', mode='managed'):
    t = db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,enabled,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (db.uid(), Path(path).name, str(path), media_kind, mode, 1, t, t),
        )


def make_run(tmp_path, size=1_000_000):
    media = tmp_path / 'book.epub'
    media.write_bytes(b'x' * size)
    source = tmp_path / 'readarr.db'
    make_readarr(source, tmp_path, media)
    seed_local_work()
    a = migration.analyze_readarr(str(source), 'ebook', 'Readarr Ebook')
    migration.reconcile_run(a['run_id'], use_network=False)
    return a['run_id']


PLANNED = 1_000_000
REQUIRED = 1_100_000  # PLANNED * 1.10


class TestCheckFreeSpace:
    def test_rejects_when_no_root(self):
        with pytest.raises(ValueError, match="No managed destination root"):
            migration.check_free_space(None, PLANNED, 0.10)

    def test_exact_boundary(self, monkeypatch):
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED)
        migration.check_free_space("/dest", PLANNED, 0.10)  # free == required passes
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED - 1)
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.check_free_space("/dest", PLANNED, 0.10)

    def test_rejects_between_planned_and_margin(self, monkeypatch):
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: PLANNED + 50_000)
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.check_free_space("/dest", PLANNED, 0.10)

    def test_passes_above_margin(self, monkeypatch):
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED + 1)
        migration.check_free_space("/dest", PLANNED, 0.10)

    def test_custom_margin(self, monkeypatch):
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: 1_400_000)
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.check_free_space("/dest", PLANNED, 0.50)
        migration.check_free_space("/dest", PLANNED, 0.30)

    def test_real_filesystem(self, tmp_path):
        migration.check_free_space(str(tmp_path), 1, 0.10)
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.check_free_space(str(tmp_path), 1 << 62, 0.10)


class TestPlanMigration:
    def test_returns_space_ok_when_sufficient(self, tmp_path, monkeypatch):
        seed_managed_root(tmp_path)
        run_id = make_run(tmp_path)
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED)
        plan = migration.plan_migration(run_id)
        assert plan['space_ok'] is True
        assert plan['total_planned_bytes'] == PLANNED
        assert plan['required_bytes'] == REQUIRED
        assert plan['safety_margin'] == 0.10

    def test_rejects_when_free_space_in_margin(self, tmp_path, monkeypatch):
        seed_managed_root(tmp_path)
        run_id = make_run(tmp_path)
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED - 1)
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.plan_migration(run_id)

    def test_safety_margin_parameter_changes_outcome(self, tmp_path, monkeypatch):
        seed_managed_root(tmp_path)
        run_id = make_run(tmp_path)
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: 1_200_000)
        assert migration.plan_migration(run_id)['space_ok'] is True
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.plan_migration(run_id, safety_margin=0.50)

    def test_settings_margin_changes_outcome(self, tmp_path, monkeypatch):
        import dataclasses
        from app import config
        seed_managed_root(tmp_path)
        run_id = make_run(tmp_path)
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: 1_200_000)
        monkeypatch.setattr(config, 'settings', dataclasses.replace(config.settings, migration_safety_margin=0.50))
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.plan_migration(run_id)
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.execute_run(run_id)

    def test_no_managed_root_is_rejected_even_with_adopted_root(self, tmp_path):
        seed_managed_root(tmp_path, mode='adopt')
        run_id = make_run(tmp_path)
        with pytest.raises(ValueError, match="No managed destination root"):
            migration.plan_migration(run_id)
        with pytest.raises(ValueError, match="No managed destination root"):
            migration.execute_run(run_id)

    def test_empty_plan_without_managed_root_is_rejected(self, tmp_path):
        run_id = make_run(tmp_path, size=0)
        with pytest.raises(ValueError, match="No managed destination root"):
            migration.plan_migration(run_id)

    def test_checks_the_destination_share_not_any_root(self, tmp_path, monkeypatch):
        full, roomy = tmp_path / 'a_full', tmp_path / 'b_roomy'
        full.mkdir(); roomy.mkdir()
        seed_managed_root(full)
        seed_managed_root(roomy)
        run_id = make_run(tmp_path)
        free = {str(full): 0, str(roomy): 10 * 1024**3}
        monkeypatch.setattr(migration, '_free_space_bytes', lambda p: free[p])
        with pytest.raises(ValueError, match="a_full"):
            migration.plan_migration(run_id)

    def test_other_media_kind_root_is_not_a_destination(self, tmp_path):
        seed_managed_root(tmp_path, media_kind='audiobook')
        run_id = make_run(tmp_path)
        with pytest.raises(ValueError, match="No managed destination root"):
            migration.plan_migration(run_id)


class TestExecuteRunFreeSpace:
    def test_rejects_when_below_safety_margin(self, tmp_path, monkeypatch):
        seed_managed_root(tmp_path)
        run_id = make_run(tmp_path)
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED - 1)
        with pytest.raises(ValueError, match="Insufficient free space"):
            migration.execute_run(run_id)

    def test_passes_at_required(self, tmp_path, monkeypatch):
        seed_managed_root(tmp_path)
        run_id = make_run(tmp_path)
        monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED)
        assert migration.execute_run(run_id)['files'] == 1


def test_plan_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    client_admin = TestClient(app)
    seed_managed_root(tmp_path)
    run_id = make_run(tmp_path)
    monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED - 1)
    r = client_admin.post(f"/api/v1/migrations/{run_id}/plan")
    assert r.status_code == 400 and "Insufficient free space" in r.json()["detail"]
    monkeypatch.setattr(migration, '_free_space_bytes', lambda _p: REQUIRED)
    r = client_admin.post(f"/api/v1/migrations/{run_id}/plan")
    assert r.status_code == 200 and r.json()["space_ok"] is True
