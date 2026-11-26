from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def create_backup(*, retention: int | None = None, prefix: str = 'bookdoe') -> dict[str, Any]:
    """Create a transactionally consistent SQLite backup and verify it before publishing.

    The source database remains online. A temporary database is written via SQLite's
    backup API, integrity-checked, fsynced by SQLite/OS close semantics, then atomically
    renamed into the backup directory. A sidecar manifest contains checksum and metadata.
    """
    retention = settings.backup_retention if retention is None else max(1, int(retention))
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final = settings.backup_dir / f'{prefix}-{stamp}.db'
    temp = settings.backup_dir / f'.{final.name}.tmp'
    manifest = final.with_suffix('.json')

    for p in (temp,):
        p.unlink(missing_ok=True)

    src = sqlite3.connect(str(settings.db_path), timeout=30)
    dst = sqlite3.connect(str(temp), timeout=30)
    try:
        src.execute('PRAGMA busy_timeout=30000')
        src.backup(dst, pages=256, sleep=0.05)
        check = dst.execute('PRAGMA integrity_check').fetchone()
        if not check or check[0] != 'ok':
            raise RuntimeError(f'Backup integrity check failed: {check[0] if check else "no result"}')
        dst.commit()
    finally:
        dst.close()
        src.close()

    temp.replace(final)
    payload = {
        'version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'database': final.name,
        'size_bytes': final.stat().st_size,
        'sha256': _sha256(final),
        'source': str(settings.db_path),
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')

    # Only this prefix's own timestamped files take part in rotation. `bookdoe-*.db`
    # also matched `bookdoe-predeploy-*.db` written by the deploy automation, which
    # crowded real daily backups out of the retention window (review 2026-10-10, A1).
    backups = sorted(_own_backups(prefix), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for old in backups[retention:]:
        removed.append(old.name)
        old.unlink(missing_ok=True)
        old.with_suffix('.json').unlink(missing_ok=True)

    payload['retention'] = retention
    payload['removed'] = removed
    return payload


_STAMP = re.compile(r'^(?P<prefix>.+)-(?P<stamp>\d{8}T\d{6}Z)\.db$')


def _own_backups(prefix: str) -> list[Path]:
    """Backups created by create_backup() under this prefix: `<prefix>-<UTC stamp>.db`."""
    out = []
    for p in settings.backup_dir.glob(f'{prefix}-*.db'):
        m = _STAMP.match(p.name)
        if m and m.group('prefix') == prefix:
            out.append(p)
    return out


def create_openreads_backup(*, retention: int | None = None) -> dict[str, Any] | None:
    """Back up OpenReads' database (field locks, manual mappings) when it is mounted.

    Returns None when BOOKDOE_OPENREADS_DB_PATH is unset or the file is absent.
    Same online backup + integrity check + manifest as create_backup, prefix
    ``openreads`` with its own retention so it never prunes (or is pruned by)
    bookdoe's backups. The source is opened read-only, so a read-only mount works.
    """
    source = settings.openreads_db_path
    if not source or not Path(source).is_file():
        return None
    retention = settings.backup_retention if retention is None else max(1, int(retention))
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final = settings.backup_dir / f'openreads-{stamp}.db'
    temp = settings.backup_dir / f'.{final.name}.tmp'
    temp.unlink(missing_ok=True)

    uri = f'file:{Path(source).resolve()}?mode=ro'
    src = sqlite3.connect(uri, uri=True, timeout=30)
    dst = sqlite3.connect(str(temp), timeout=30)
    try:
        src.execute('PRAGMA busy_timeout=30000')
        src.backup(dst, pages=256, sleep=0.05)
        check = dst.execute('PRAGMA integrity_check').fetchone()
        if not check or check[0] != 'ok':
            raise RuntimeError(f'OpenReads backup integrity check failed: {check[0] if check else "no result"}')
        dst.commit()
    except Exception:
        dst.close()
        src.close()
        temp.unlink(missing_ok=True)
        raise
    dst.close()
    src.close()

    temp.replace(final)
    payload = {
        'version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'database': final.name,
        'size_bytes': final.stat().st_size,
        'sha256': _sha256(final),
        'source': str(source),
    }
    final.with_suffix('.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')

    backups = sorted(settings.backup_dir.glob('openreads-*.db'), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for old in backups[retention:]:
        removed.append(old.name)
        old.unlink(missing_ok=True)
        old.with_suffix('.json').unlink(missing_ok=True)
    payload['retention'] = retention
    payload['removed'] = removed
    return payload


def list_backups() -> list[dict[str, Any]]:
    """Every .db in the backup directory, whoever wrote it, newest first.

    Files without a manifest (foreign copies such as the deploy automation's) are
    listed with sha256 None so an operator can see them, but they are never rotated."""
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for dbfile in sorted(settings.backup_dir.glob('*.db'), key=lambda p: p.stat().st_mtime, reverse=True):
        sidecar = dbfile.with_suffix('.json')
        data: dict[str, Any] = {}
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text())
            except Exception:
                data = {}
        items.append({
            'database': dbfile.name,
            'size_bytes': dbfile.stat().st_size,
            'mtime': dbfile.stat().st_mtime,
            'sha256': data.get('sha256'),
            'created_at': data.get('created_at'),
        })
    return items
