# bookdoe — Coding Guide for Builders

> This file covers only the practical conventions and pitfalls that cause review rejections.

## Project layout

```
bookdoe/
  app/
    main.py            # FastAPI routes + request/response models (thin wrappers)
    db.py              # SQLite schema, connection helpers, seed data
    auth.py            # OIDC / session / API-key auth
    config.py          # Settings (env-var backed)
    migrations.py      # Versioned migration framework — ALL schema changes go here
    services/          # Business logic (one module per domain)
    integrations/      # External adapters (indexers, downloaders)
  tests/               # pytest suite — one file per feature area
  requirements.txt     # pip install -r requirements.txt
  pytest.ini           # testpaths = tests; markers = slow
```

## Running tests

Every command below runs from the **repo root**; the ones that need another directory
`cd` into it themselves, so each is self-contained.

```bash
make test                                                          # full suite (openreads + bookdoe)
cd bookdoe && PYTHONPATH=. python -m pytest -q tests/test_db.py # one test file
python -m compileall -q bookdoe/app                             # catches SyntaxError / bad imports
```

`make test` runs `cd openreads && pytest -q` then `cd bookdoe && PYTHONPATH=. pytest -q`.
Tests marked `slow` are skipped unless you select them with `-m slow`.

CI runs the bookdoe suite in parallel: from `bookdoe/`, install `requirements.txt` plus `requirements-dev.txt`, then run
`PYTHONPATH=. pytest -q -p no:cacheprovider -n 4 --dist loadfile` (pytest-xdist, pinned in `bookdoe/requirements-dev.txt`, test-only). Each xdist worker is its own process with its own
temp session root and per-test DB, so no extra setup is needed. For a serial run, or to debug one test, drop `-n 4`
(or pass `-p no:xdist`); keep tests free of fixed ports and shared files so they stay parallel-safe.

## Invariants (repeat offenders)

1. **Managed roots only.** A library root's `management_mode` is `observe`, `adopt` or
   `managed`; only `managed` roots may be written to (adopted roots are the user's
   files and stay read-only). Every path goes through `resolve_safe` in
   `bookdoe/app/services/paths.py`, which returns `None` for anything outside the
   root (`..` or symlink escape).
2. **Migrations are numbered steps registered through `bookdoe/app/migrations.py`.** Steps
   1-41 are in that file; new ones go one per module in `bookdoe/app/migration_steps/`
   (imported by `migrations.py`). Never add schema changes inline in `db.py`.

## Naming and comments

- Module-level functions: `snake_case` with a short docstring (one line).
- Service functions called from routes: prefix with an underscore for internal
  helpers (e.g. `_ensure_assets` in `bookdoe/app/services/metadata.py`,
  `_apply_asset_monitoring` in `bookdoe/app/services/monitoring.py`).
- Comment the *why*, not the *what*. If the code is self-evident, skip the comment.
- Pydantic models: use `model_config = ConfigDict(extra='forbid')` on request models
  to reject unknown fields (see `AuthorMonitorRequest` in `bookdoe/app/main.py`).

## PRE-PUSH CHECKLIST

Before every push, verify each item against your diff:

- [ ] **Called from production code.** Every new function, check, or helper has a
      call site in `bookdoe/app/main.py` or a service module — not just a test.
- [ ] **Names defined.** No undefined names, no missing imports (`import re` before
      `re.sub`, etc.). Run `python -m compileall -q bookdoe/app` to catch this.
- [ ] **Tests drive the real entry point.** Assert on the actual return value or
      database state, not on a policy string or a tautological `"X" in c or ...`.
- [ ] **Every file the issue names changed.** Re-read the issue; if it says "update
      config.py and the call site in main.py", both must appear in your diff.
- [ ] **Diff within scope.** No unrelated renames, reformatting, or 2000-line cleanups
      for a two-line finding.
- [ ] **Rebased on main.** `git fetch && git rebase origin/main` — no merge commits.
- [ ] **No wall-clock sleeps.** Tests must use `monkeypatch` or fixtures, not `time.sleep`.
