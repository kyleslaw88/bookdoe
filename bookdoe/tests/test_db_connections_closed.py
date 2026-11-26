"""#36: 'disk I/O error' in an unrelated test.

Root cause: CPython keeps every sqlite3.Connection in a reference cycle with its
statement cache, so ``with db.connect() as conn`` (which only ends the transaction)
left each connection open until the cyclic GC ran at some arbitrary later point.
Under the old shared per-process database path, that late close of one test's last
handle made SQLite checkpoint and unlink the -wal/-shm files *by path*, pulling them
out from under the test that was running by then, which failed with 'disk I/O error'.

Guards: db helpers close their connections; every test has its own database path;
conftest fails any test that leaves a handle (or thread) behind.

The originally flaky test, tests/test_scan_candidates_api.py::test_list_is_paginated_like_library_and_wanted,
is unchanged: it was the victim, not the leaker. It is now covered by the teardown guard
in tests/conftest.py (fresh_db), which gives it a private database and fails whichever
test leaves a connection or thread behind, and its request path is exercised for leaks by
test_scan_candidates_api_leaves_no_open_handles_or_threads below.

Acceptance criteria for #36 and what demonstrates each:

    Criterion                                   Demonstrated by
    ------------------------------------------  --------------------------------------------------------------
    Shared state identified (root cause)        This docstring; conftest.open_database_handles docstring;
                                                test_open_database_handles_sees_an_unclosed_connection
                                                (a dropped connection stays open until closed)
    Per-test isolation of db/data/backup/cover  conftest.fresh_db (ISOLATED_PATHS swapped per test);
                                                test_each_test_gets_its_own_database_and_dirs
    No lingering connections (handles)          db.session(); test_helpers_leave_no_open_handles;
                                                test_session_closes_and_rolls_back_on_error;
                                                test_teardown_fails_a_test_that_leaks_a_connection
                                                (the conftest guard fails the leaking test itself)
    No lingering threads / background writers   conftest.fresh_db teardown joins and asserts on new threads;
                                                test_scan_candidates_api_leaves_no_open_handles_or_threads;
                                                test_scheduler_is_off_in_tests_and_lifespan_does_not_start_it
    10 consecutive full-suite passes            scripts/stress_suite.sh (exits non-zero on the first failing
                                                run); committed output: tests/evidence/issue-36-stress-run.txt
    No retry wrappers                           No pytest-rerunfailures/flaky in requirements.txt or
                                                pytest.ini; stress_suite.sh stops on the first failure
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from conftest import open_database_handles

needs_proc = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
BOOKDOE_ROOT = Path(__file__).resolve().parents[1]


def _handles() -> list[str]:
    return open_database_handles(str(settings.db_path))


@needs_proc
def test_open_database_handles_sees_an_unclosed_connection():
    """The detector the teardown guard relies on: an open connection shows up, a closed
    one does not."""
    before = _handles()
    conn = db.connect()
    conn.execute("SELECT 1")
    assert len(_handles()) > len(before)
    conn.close()
    assert _handles() == before


@needs_proc
def test_helpers_leave_no_open_handles():
    before = _handles()
    db.one("SELECT 1 AS x")
    db.all("SELECT 1 AS x")
    with db.tx() as c:
        c.execute("SELECT 1")
    with db.session() as c:
        c.execute("SELECT 1")
    db.init_db()
    assert _handles() == before


@needs_proc
def test_scan_candidates_api_leaves_no_open_handles_or_threads():
    """The path that flaked in #36: app startup (lifespan) + the paginated list endpoint."""
    rid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO scan_runs(id,status,started_at,finished_at) VALUES (?,?,?,?)", (rid, "done", t, t))
    before, threads = _handles(), set(threading.enumerate())
    with TestClient(app) as client:
        assert client.get("/api/v1/scan-candidates?limit=1&offset=0").status_code == 200
    assert _handles() == before
    for x in threading.enumerate():
        if x not in threads:
            x.join(timeout=5)
            assert not x.is_alive(), x


@needs_proc
def test_teardown_fails_a_test_that_leaks_a_connection(tmp_path):
    """The conftest guard is live: a test that drops an unclosed connection fails at its
    own teardown (instead of breaking a later test), while a clean test passes. Run in a
    subprocess with tests/conftest.py loaded as a plugin so the probe file can live in
    tmp_path rather than in the suite."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent("""
        from app import db

        def test_leaks_a_connection():
            db.connect().execute("SELECT 1")  # dropped, never closed

        def test_closes_its_connection():
            with db.session() as c:
                c.execute("SELECT 1")
    """))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "tests.conftest", "-o", "addopts=", "-W", "ignore", "-q", str(probe)],
        cwd=BOOKDOE_ROOT, env={**os.environ, "PYTHONPATH": str(BOOKDOE_ROOT)}, capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    # The probe's body passes; its fresh_db teardown is what fails, which pytest reports
    # as an error on that test, and only on that test.
    assert "2 passed, 1 error" in out, out
    assert "test left sqlite connections open on its database" in out, out
    assert "ERROR " in out and "test_probe.py::test_leaks_a_connection" in out.split("short test summary")[-1], out
    assert "test_closes_its_connection" not in out.split("short test summary")[-1], out


def test_session_closes_and_rolls_back_on_error():
    holder = {}
    with pytest.raises(RuntimeError):
        with db.session() as c:
            holder["c"] = c
            c.execute("INSERT INTO scan_runs(id,status,started_at) VALUES ('x','done',0)")
            raise RuntimeError
    assert db.one("SELECT 1 AS x FROM scan_runs WHERE id='x'") is None
    with pytest.raises(sqlite3.ProgrammingError):
        holder["c"].execute("SELECT 1")


def test_each_test_gets_its_own_database_and_dirs(tmp_path):
    """connect() resolves settings.db_path per call, so writes land in this test's file,
    never in the import-time BOOKDOE_DB_PATH default."""
    for field in ("db_path", "data_dir", "backup_dir", "cover_dir"):
        assert getattr(settings, field).parent == tmp_path / "_bookdoe", field
    assert str(settings.db_path) != os.environ["BOOKDOE_DB_PATH"]
    db.add_history("test", "isolation marker")
    with contextlib.closing(sqlite3.connect(tmp_path / "_bookdoe" / "bookdoe.db")) as raw:
        assert raw.execute("SELECT COUNT(*) FROM history WHERE event_type='test'").fetchone()[0] == 1
    assert not os.path.exists(os.environ["BOOKDOE_DB_PATH"])


def test_scheduler_is_off_in_tests_and_lifespan_does_not_start_it(caplog):
    """No background writer: the scheduler loop (asyncio task + worker threads) must
    not start under TestClient, or it could write to a database after its test ended."""
    assert settings.scheduler_enabled is False
    caplog.set_level(logging.INFO, logger="app.services.scheduler")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert "scheduler started" not in caplog.text
