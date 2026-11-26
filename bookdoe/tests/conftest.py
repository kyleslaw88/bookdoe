import atexit
import os
import sqlite3
import shutil
import tempfile
import threading
from contextlib import closing

# Settings reads these once, at import. They only keep that import-time default away
# from the production /config paths; every test then gets its own paths (fresh_db).
_SESSION_ROOT = tempfile.mkdtemp(prefix="bookdoe-pytest-")
atexit.register(shutil.rmtree, _SESSION_ROOT, ignore_errors=True)
os.environ["BOOKDOE_DB_PATH"] = os.path.join(_SESSION_ROOT, "unused.db")
os.environ["BOOKDOE_DATA_DIR"] = os.path.join(_SESSION_ROOT, "data")
os.environ["BOOKDOE_BACKUP_DIR"] = os.path.join(_SESSION_ROOT, "backups")
os.environ["BOOKDOE_COVER_DIR"] = os.path.join(_SESSION_ROOT, "covers")
os.environ["BOOKDOE_AUTH_MODE"] = "none"
os.environ["BOOKDOE_SCHEDULER_ENABLED"] = "0"
os.environ["BOOKDOE_SECRET_KEY"] = "pytest-secret-key-that-is-long-and-stable"
# Existing suites exercise the acquisition routes/jobs; tests/test_acquisition_flag.py clears this to test the off default.
os.environ["BOOKDOE_ACQUISITION_ENABLED"] = "1"

import pytest

from app import db
from app.config import settings
# Imported here, not inside the teardown: a first import during teardown allocates enough
# to trigger the cyclic GC, which would close a leaked connection before the leak check
# below could see it (test_db_connections_closed relies on that check being live).
from app.services import scheduler as _scheduler
from app.services import devices as _devices
from app.services import discover_chat as _discover
from app.services import request_search as _request_search

ISOLATED_PATHS = {"db_path": "bookdoe.db", "data_dir": "data", "backup_dir": "backups", "cover_dir": "covers"}


def open_database_handles(db_path: str) -> list[str]:
    """Files this process still holds open on ``db_path`` or its -wal/-shm companions.

    Linux only (reads /proc/self/fd); elsewhere returns [] and the check is skipped.
    A sqlite3.Connection that was merely dropped is NOT closed: CPython keeps it in a
    reference cycle with its statement cache, so only the cyclic GC closes it, at an
    arbitrary later moment. When that last handle on a database closes, SQLite
    checkpoints and unlinks the -wal/-shm files *by path*, which is how one test's
    leftover connection broke a later test's database under the old shared path (#36).
    """
    out = []
    try:
        fds = os.listdir("/proc/self/fd")
    except OSError:
        return out
    for fd in fds:
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(db_path):
            out.append(target)
    return sorted(out)


@pytest.fixture(autouse=True)
def no_background_request_search(monkeypatch):
    """Approving a request starts a background search in production; tests that are not about
    that must not run one (test_request_search.py swaps this hook for an inline runner)."""
    monkeypatch.setattr(_request_search, "_enqueue", lambda fn, *args: _request_search._inflight_requests.discard(args[0]))


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """Argon2 at production cost is ~50ms per hash or verify, and fixtures create several
    accounts and log in each; the minimum cost exercises the same code path."""
    from argon2 import PasswordHasher
    from app import crypto
    monkeypatch.setattr(crypto, "_ph", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture(autouse=True)
def no_fsync_in_tests(monkeypatch):
    """Profiling the serial suite (#473) showed sqlite commit/close (WAL fsync) dominating the
    per-test time. The throwaway test databases need no durability, so every connection the
    app opens turns fsync off. Production connect() is unchanged."""
    real_connect = db.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    monkeypatch.setattr(db, "connect", connect)


@pytest.fixture(autouse=True)
def fresh_search_process_state(monkeypatch):
    """The throttle pause, the request-search pacing clock and the cap reservations live in the
    process: every test starts with none of them."""
    from app.services import wanted_search as _ws
    monkeypatch.setattr(_ws, "_throttle_until", 0.0)
    monkeypatch.setattr(_ws, "_cap_reserved", 0)
    monkeypatch.setattr(_request_search, "_last_search_at", None)


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory):
    """A fully migrated and seeded database, built once per worker process. Running every
    migration step per test cost ~0.3s each, which made the serial suite exceed the
    nightly 30 minute limit; fresh_db copies this file instead."""
    root = tmp_path_factory.mktemp("template") / "_bookdoe"
    original = {k: getattr(settings, k) for k in ISOLATED_PATHS}
    for k, name in ISOLATED_PATHS.items():
        object.__setattr__(settings, k, root / name)
    try:
        db.init_db()
        path = str(settings.db_path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert not open_database_handles(path)
    finally:
        for k, v in original.items():
            object.__setattr__(settings, k, v)
    return path


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, migrated_template):
    """Every test gets its own database file and data/backup/cover dirs, so a handle
    left over from one test can never touch the WAL/shm files of the next. The
    settings object is frozen and shared by every module, so its fields are swapped
    in place; db.connect() reads settings.db_path on every call.

    On teardown the test fails if it left a connection or a thread behind: both are
    exactly the leaks that caused #36, and failing the leaking test here is what keeps
    them from surfacing as a 'disk I/O error' in an unrelated test later."""
    from app.services import writelimit
    writelimit.reset()    # the merge/cleanup write budget is per process; every test starts with a full one
    original = {k: getattr(settings, k) for k in ISOLATED_PATHS}
    root = tmp_path / "_bookdoe"  # kept apart from dirs tests create in tmp_path
    for k, name in ISOLATED_PATHS.items():
        object.__setattr__(settings, k, root / name)
    db_path = str(settings.db_path)
    threads_before = set(threading.enumerate())
    settings.ensure_dirs()
    with closing(sqlite3.connect(migrated_template)) as src, closing(sqlite3.connect(db_path)) as dst:
        src.backup(dst)
    from app.services import reader as _reader
    _reader.reset_progress_limits()
    yield
    # A thread outliving its test (scheduler, worker pool) could write to the next
    # test's database. TestClient's AnyIO workers are told to stop when its event loop
    # closes but exit asynchronously, so wait for each one to finish. The job pool's
    # idle workers are stopped explicitly (they are reused across tests otherwise).
    if _scheduler._executor is not None:
        _scheduler.shutdown_pool(wait=True)
    if _request_search._pool_obj is not None:
        _request_search.shutdown_pool(wait=True)
    if _discover._pool_obj is not None or _discover._ground_pool_obj is not None or _discover._retired_pools:
        _discover.shutdown_pool(wait=True)
    if _devices._mail_pool is not None:
        _devices.shutdown_mail_pool(wait=True)
    leftover = [t for t in threading.enumerate() if t not in threads_before]
    for t in leftover:
        t.join(timeout=5)
    # Restore the import-time paths only now: a job still running in the pool (or a thread
    # joined above) resolves settings.db_path per call, so restoring first lets it open
    # the import-time database and create it, which fails test_each_test_gets_its_own_database_and_dirs
    # in whichever later test of this worker runs it (timing dependent, so only on slow runners).
    for k, v in original.items():
        object.__setattr__(settings, k, v)
    alive = [t for t in leftover if t.is_alive()]
    assert not alive, f"test left threads running that could still write to its database: {alive}"
    handles = open_database_handles(db_path)
    assert not handles, (
        "test left sqlite connections open on its database (close them: db.session(), "
        f"db.tx() or contextlib.closing(db.connect())): {handles}"
    )


def pytest_collection_modifyitems(config, items):
    """Keep the default suite fast: `slow` tests run only when -m names the marker."""
    if "slow" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="slow: run with `pytest -m slow`")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
