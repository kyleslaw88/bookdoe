import contextlib
import sqlite3

from app.config import settings
from app.services import backup
from app import db


def test_online_backup_is_valid_and_retained():
    db.add_history('test', 'backup source row')
    first = backup.create_backup(retention=1)
    assert first['sha256']
    path = settings.backup_dir / first['database']
    assert path.exists()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute("SELECT COUNT(*) FROM history WHERE event_type='test'").fetchone()[0] == 1
    # A second logical backup can share the same second-level timestamp in a fast test;
    # retention still guarantees no more than one database remains.
    backup.create_backup(retention=1)
    assert len(list(settings.backup_dir.glob('bookdoe-*.db'))) <= 1


def test_foreign_prefixed_files_are_listed_but_never_rotated():
    """A1 (review 2026-10-10): the deploy automation drops `bookdoe-predeploy-*.db`
    into the same directory. Those must neither count against nor be deleted by the
    daily retention, which once left production with 5 daily backups instead of 14."""
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    foreign = settings.backup_dir / 'bookdoe-predeploy-20261006-020026.db'
    foreign.write_bytes(b'not a real backup')
    manual = settings.backup_dir / 'premigrate-20261003T182015Z.db'
    manual.write_bytes(b'another prefix')

    for _ in range(3):
        result = backup.create_backup(retention=1)
    assert foreign.exists() and manual.exists()
    assert 'bookdoe-predeploy-20261006-020026.db' not in result['removed']
    own = [p.name for p in settings.backup_dir.glob('bookdoe-*.db') if 'predeploy' not in p.name]
    assert len(own) == 1
    names = {x['database'] for x in backup.list_backups()}
    assert foreign.name in names and manual.name in names and own[0] in names
    assert next(x for x in backup.list_backups() if x['database'] == foreign.name)['sha256'] is None


def test_default_safety_jobs_exist():
    kinds = {x['kind'] for x in db.all('SELECT kind FROM jobs')}
    assert 'database-backup' in kinds
    assert 'author-discovery' in kinds
    assert 'library-health' in kinds


def _openreads_db(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE field_locks(k TEXT)')
        conn.execute("INSERT INTO field_locks VALUES ('title')")
        conn.commit()


def _with_openreads(monkeypatch, path):
    import dataclasses
    monkeypatch.setattr(backup, 'settings', dataclasses.replace(backup.settings, openreads_db_path=path))


def test_openreads_backup_skipped_when_unconfigured_or_missing(monkeypatch, tmp_path):
    assert backup.create_openreads_backup() is None
    _with_openreads(monkeypatch, tmp_path / 'nope.db')
    assert backup.create_openreads_backup() is None


def test_openreads_backup_is_valid_has_manifest_and_own_retention(monkeypatch, tmp_path):
    src = tmp_path / 'openreads.db'
    _openreads_db(src)
    _with_openreads(monkeypatch, src)
    first = backup.create_openreads_backup(retention=1)
    out = settings.backup_dir / first['database']
    assert first['database'].startswith('openreads-') and out.exists()
    assert out.with_suffix('.json').exists() and first['sha256'] == backup._sha256(out)
    with contextlib.closing(sqlite3.connect(out)) as conn:
        assert conn.execute('SELECT k FROM field_locks').fetchone()[0] == 'title'
    # Pruning is per prefix: bookdoe backups are untouched.
    keep = backup.create_backup(retention=5)
    backup.create_openreads_backup(retention=1)
    assert len(list(settings.backup_dir.glob('openreads-*.db'))) == 1
    assert (settings.backup_dir / keep['database']).exists()


def test_database_backup_job_includes_openreads_and_survives_its_failure(monkeypatch, tmp_path):
    from app.services import scheduler
    job = db.one("SELECT * FROM jobs WHERE kind='database-backup'")
    src = tmp_path / 'openreads.db'
    _openreads_db(src)
    _with_openreads(monkeypatch, src)
    ok = scheduler.run_job(job['id'])
    assert ok['status'] == 'success' and ok['result']['openreads']['database'].startswith('openreads-')

    def boom(**_):
        raise RuntimeError('disk full')
    monkeypatch.setattr(backup, 'create_openreads_backup', boom)
    bad = scheduler.run_job(job['id'])
    assert bad['status'] == 'success'
    assert bad['result']['openreads'] == {'error': 'disk full'} and bad['result']['sha256']
