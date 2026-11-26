# Browser QA

Three Playwright suites that drive the real UI in a real browser at real scale. They
exist because the Python suite, `node --check` and even the jsdom harness all passed
green while the app was unusable on a phone: the nav drawer covered the screen with
no way to dismiss it, the Settings tabs pushed the page sideways, every tap target
was under the 44px floor, and Review rendered 12,239 raw JSON dumps into a
49,000px-tall page. None of that is visible without layout and a viewport.

| script | what it covers |
| --- | --- |
| `ui-sweep.mjs` | every route at mobile/tablet/desktop: horizontal overflow, console errors, failed requests, tap targets, page height, drawer state |
| `ui-flows.mjs` | end-to-end journeys: search → open a book → refresh metadata → pick a candidate; load-more; authors; discover; review; settings |
| `ui-mobile-nav.mjs` | the drawer's four dismissal paths and the Settings tab strip |
| `ui-search.mjs` | that typing in the library filter does not rebuild the page (focus and caret survive), and that "More results" on Discover and in the picker actually returns more |
| `ui-merge.mjs` | merging duplicate books: the 39 Clues banner, the merge dialog, merge and Undo, Merge selected (series table and library select mode), Find duplicates, the Duplicate files panel with trash and Restore, Settings > System recent merges and trash; leaves the data as it found it |
| `ui-managed-build.mjs` | the managed library build: Settings > Library roots banner, plain-words root modes, per-root counts and "Copy into managed library...", the build panel (dry run, typed-count confirm, the "Allow builds to run" switch: Start disabled and the API 409 while it is off, its text saying that turning it off pauses a running build after the current file, start, completion, history with the kept-history Undo warning), the Settings > Jobs link, the Overview card, the book page state with Source copies, one-book Undo and the one-book copy dialog; undoes its build at the end |
| `ui-library-filters.mjs` | Library search and filters never strand you: header and page search stay in sync, clear via the header box, the page input, the search event and Escape, nav away and back drops the query, filter chips and Clear filters, the empty-state button, link filters (`#library?q=`) apply once, plus Clear filters on Authors, Series, Queue and the Player library |
| `ui-duplicates.mjs` | the Library State filter (Complete / Partly owned / Nothing owned with counts, the grey "No audiobook" chip, old `status=missing` links) and the Duplicates page: scan, filters, side-by-side groups, dismissal, select all High, dry run, bulk merge, then one-click batch Undo; no horizontal overflow; leaves the data as it found it |
| `ui-discover-chat.mjs` | the Player's Discover chat on its own local-auth instance with a stubbed model and metadata provider (`ui-discover-stub.mjs`), at 360/390/1280 px in `ui-gate`: ask, follow-up, library and not-in-library cards, Request, thumbs, remember, taste profile, Start over, Search tab, the kid view with its transparency notice, an approver reading the kid's chat, and the chat disappearing when switched off |
| `ui-metadata-picker.mjs` | the "Which book is this?" dialog: seeded search, refining, empty results, the "none of these" exit, and that file-derived series/year/narrator reach the page |

## The gate: `make check`

`make check` (or `make uigate` on its own) runs `ui-overflow.mjs` (every route must fit
the viewport width) and `ui-mobile-nav.mjs` at **360 and 390 px**, headless. It needs
no server and no data of its own: `scripts/ui-gate.mjs` seeds a throwaway database
(`ui-render-seed.py`) under the OS temp dir, starts the app on a free port with auth
off and the scheduler disabled, runs the checks, and removes the temp dir and stops
the server afterwards. A page that scrolls sideways at 390 px fails the build.

Requires Node >=20 (Playwright 1.64's `engines` floor).

One-time setup, from the repo root:

```sh
npm install && npx playwright install chromium
```

Run it with the Python that has `bookdoe/requirements.txt` installed
(`PYTHON=/path/to/venv/bin/python make check`; defaults to `python` on PATH). If
playwright is missing the gate fails with that install hint rather than skipping.
`UI_WIDTH`, `UI_BASE` and `UI_SHOTS` let you run a single check by hand against any
server. `ui-flows.mjs`, `ui-sweep.mjs`, `ui-search.mjs` and `ui-metadata-picker.mjs`
stay in `make uiqa`: they need OpenReads, network access or a large real-data copy.

## Running the gate locally

`scripts/ui-gate.mjs` is the full gate: it seeds a throwaway database, starts the app,
runs every check at 360, 390 and 1280 px, then tears everything down. It is also the
CI job `ui-gate` (see `the CI workflow`), which runs in the
`mcr.microsoft.com/playwright` container (browsers come with the image, nothing is downloaded
per run) and uploads its log as the `ui-gate-log` artifact.

```sh
# One-time setup (if not already done):
npm install && npx playwright install chromium

# Run the full gate:
node scripts/ui-gate.mjs
```

The gate needs the Python that has `bookdoe/requirements.txt` installed. Set
`PYTHON=/path/to/venv/bin/python` to pick a specific interpreter. The gate creates
temporary directories under `/tmp` and removes them on exit (or on SIGINT/SIGTERM).
Exit code 0 means all checks passed; non-zero means one or more failed.

The gate is **non-required** in CI (branch protection, not `continue-on-error`).
After 20 consecutive green runs on main it should be promoted to required.

## Running them

```sh
npm install playwright@1.49.1 && npx playwright install chromium   # Node 18 cannot run playwright >= 1.51
```

Point them at an instance with auth disabled, ideally holding a **copy** of real data
— scale is what surfaces these bugs, and a copy means destructive actions are safe:

```sh
sqlite3 -readonly "file:/path/to/bookdoe.db?mode=ro" ".backup /tmp/qa/run/bookdoe.db"
cp -r /path/to/covers /tmp/qa/run/covers

BOOKDOE_DB_PATH=/tmp/qa/run/bookdoe.db BOOKDOE_DATA_DIR=/tmp/qa/run \
BOOKDOE_COVER_DIR=/tmp/qa/run/covers BOOKDOE_AUTH_MODE=none \
BOOKDOE_SCHEDULER_ENABLED=0 BOOKDOE_SECRET_KEY=qa-secret-key-long-enough-here \
PYTHONPATH=. python -m uvicorn app.main:app --host 127.0.0.1 --port 8899 &

node scripts/ui-sweep.mjs && node scripts/ui-flows.mjs && node scripts/ui-mobile-nav.mjs
```

Screenshots land in `/tmp/qa/shots`. `ui-flows.mjs` needs OpenReads reachable; when
the production instance is on an unpublished Docker port, tunnel to it rather than
publishing it: `ssh -N -L 18790:<container-ip>:8790 <host>` then
`OPENREADS_URL=http://127.0.0.1:18790`.

## Known coverage gap

Production is OIDC-only, so these run against `BOOKDOE_AUTH_MODE=none`. The login
screen, the Authentik round-trip and session expiry are **not** covered here — see
`tests/test_oidc_callback_e2e.py` for that flow.
