# UI render check

`node --check` proves the UI file parses. It does not prove the UI *works*: a
correct API behind a broken client is still a broken feature, and the four defects
fixed on 2026-09-24 (empty cover frames, one author row per book, a refresh button
that only explained itself, a library that could only see its first page) were all
invisible to both the Python suite and the syntax check.

This harness loads the real `index.html` and `app.js` into jsdom, points them at a
real running bookdoe, drives the actual DOM, and asserts what a person would see.

```sh
npm install jsdom@22                     # Node 18 cannot load jsdom >= 23
python scripts/ui-render-seed.py         # seeds /tmp/uitest/run with 422 works
# then, from bookdoe/:
BOOKDOE_DB_PATH=/tmp/uitest/run/bookdoe.db BOOKDOE_DATA_DIR=/tmp/uitest/run \
BOOKDOE_BACKUP_DIR=/tmp/uitest/run/backups BOOKDOE_COVER_DIR=/tmp/uitest/run/covers \
BOOKDOE_AUTH_MODE=none BOOKDOE_SCHEDULER_ENABLED=0 \
BOOKDOE_SECRET_KEY=uitest-secret-key-long-enough \
PYTHONPATH=. python -m uvicorn app.main:app --host 127.0.0.1 --port 8899 &

node scripts/ui-render-check.mjs         # exits non-zero on any failed assertion
```

Two jsdom limitations the harness works around deliberately: it has no `fetch`, and
it does not execute `type="module"` scripts. `app.js` is therefore injected as a
classic script over a `fetch` that reaches the real server — the code under test is
the shipped file, unmodified.
