# Contributing

Thanks for looking at bookdoe. The project is small enough that one person can
hold it in their head; please keep it that way.

## Ground rules

- Open an issue before a large change so the design can be discussed first.
- One topic per pull request. Rebase on `main`; no merge commits.
- Every behaviour change comes with a test. The suites are the specification.
- No secrets, personal hostnames, LAN addresses or machine-specific paths in the
  repository, ever. `make secret-scan` runs on every pull request and fails on them.
- Published text (README, docs) uses plain punctuation; keep sentences short.

## Setting up

```sh
python3 -m venv .venv-bookdoe && .venv-bookdoe/bin/pip install -r bookdoe/requirements.txt -r bookdoe/requirements-dev.txt
python3 -m venv .venv-openreads  && .venv-openreads/bin/pip install -r openreads/requirements.txt pytest
npm install                       # only for the browser QA gate (make uigate)
```

The two services pin different FastAPI versions, hence two environments.

```sh
make test                                                   # both suites
cd bookdoe && PYTHONPATH=. pytest -q tests/test_db.py     # one file
make check                                                  # what CI runs
```

Every test gets its own SQLite database; a test that leaks a connection or a thread
fails on teardown on purpose (see `bookdoe/tests/conftest.py`).

## Files every branch used to edit (now one file per change)

- **UI checks:** a new `scripts/ui-<name>.mjs` starts with `// ui-gate: widths=360,390,1280 order=140`
  and `make uigate` finds it. `order` is the run position on the shared seeded instance (checks sort by it, then
  by name; no `order` runs last): give a check that changes data a higher number than the read-only ones. A script that needs its own instance, or is not part of the gate,
  goes in the `OWN_INSTANCE` / `NOT_GATED` list in `scripts/ui-gate.mjs` (one entry per line);
  a `ui-*.mjs` with neither fails the gate. `node scripts/ui-gate.mjs --list` prints the plan.
- **ES modules:** `scripts/check-esm.sh` checks every `bookdoe/app/static/*.js` except the
  classic scripts named in it; nothing to register.
- **Migrations:** steps 1-41 live in `bookdoe/app/migrations.py`. A new step is its own module,
  `bookdoe/app/migration_steps/<slug>.py`, with `from ..migrations import register` and
  `@register(N)`; `migrations.py` imports every module in that package, so it is still the one
  registry. Two branches that each add a step touch different files and merge cleanly. Take the
  next free `N` at merge time, not when you start: if another branch landed first, renumber yours
  (and its tests). `python scripts/check-migrations.py` (in `make ci` and CI) fails when two steps
  share a number or a name.

## Code conventions

`CODING.md` is the short list of invariants that cause review rejections: managed
roots only, migrations in one place, the pre-push checklist. Read it before your
first pull request.

- Python 3.12, FastAPI, SQLite. No ORM, no message broker, no new runtime
  dependencies without a reason written in the pull request.
- Routes in `bookdoe/app/main.py` and `player_api.py` are thin; logic lives in
  `bookdoe/app/services/`.
- Request models reject unknown fields (`extra='forbid'`).
- Secrets are read through `bookdoe/app/secrets_provider.py` and never logged.
- The browser apps are dependency-free ES modules; `make check` syntax-checks them.

## iOS

`ios/Packages/AlexandriaKit` holds the logic and its tests (`swift test`, runs on
macOS and Linux). The app target under `ios/App` needs Xcode; generate the project
with `xcodegen generate`. Keep server-specific quirks inside the provider for that
server. See `docs/IOS_BUILD.md`.

## Android

`android/core` is a pure Kotlin/JVM module mirroring `AlexandriaKit` (same models,
providers and test scenarios); `android/app` is the Compose and Media3 app.
`./gradlew :core:test :app:testDebugUnitTest :app:assembleDebug :app:lintDebug`
is what CI runs and needs no emulator; `android/ci/build.sh` runs it in Docker
without an SDK on the machine. A change to a provider belongs in both the Swift and
the Kotlin version, with the test ported too. See `docs/ANDROID_BUILD.md`.

## Pull request checklist

- [ ] Tests added or updated, `make check` green locally.
- [ ] No new personal data or secrets (`make secret-scan`).
- [ ] Docs updated when behaviour or configuration changed (`docs/`, `.env.example`).
- [ ] Migration added in `bookdoe/app/migrations.py` for any schema change, additive
      where possible, with the upgrade note in `docs/OPERATIONS.md`.
