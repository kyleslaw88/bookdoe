.PHONY: test check uigate uicheck uiqa secret-scan hygiene ci compile ios-test android-test android-build docker-build install

test:
	cd openreads && pytest -q
	cd bookdoe && PYTHONPATH=. pytest -q

# What a CI runner without a browser can do: compile, bundle syntax, both suites
# (the bookdoe suite includes the hygiene scan).
ci: compile test
	node --check bookdoe/app/static/app.js openreads/static/app.js
	node --check bookdoe/app/static/sw.js bookdoe/app/static/pwa.js
	sh scripts/check-esm.sh
	python scripts/check-migrations.py
	python scripts/changelog.py --check

compile:
	python -m compileall -q bookdoe/app openreads

# SwiftPM package tests for the iOS app (needs Swift 5.9+; works on macOS and Linux).
ios-test:
	cd ios/Packages/BookdoeKit && swift test

# Android unit tests (core JVM module and app ViewModel-level tests) and the debug
# APK plus lint. Needs JDK 17 and the Android SDK (ANDROID_HOME); without one, run
# android/ci/build.sh for the same commands inside Docker.
android-test:
	cd android && ./gradlew --no-daemon :core:test :app:testDebugUnitTest

android-build:
	cd android && ./gradlew --no-daemon :app:assembleDebug :app:lintDebug

docker-build:
	docker compose build

# One-command install (see docs/INSTALL.md).
install:
	sh install.sh

# Repository hygiene: no secrets, tokens, personal hosts or machine paths in tracked
# files. Uses gitleaks when installed (same rules, .gitleaks.toml), otherwise the
# pure-Python mirror in bookdoe/tests/test_repo_hygiene.py. CI runs the latter.
secret-scan:
	@if command -v gitleaks >/dev/null 2>&1; then gitleaks detect --source . --config .gitleaks.toml --no-banner --redact; \
	else echo "gitleaks not installed; running the Python scanner"; $(MAKE) hygiene; fi

hygiene:
	cd bookdoe && PYTHONPATH=. pytest -q tests/test_repo_hygiene.py

check: test uigate hygiene
	python -m compileall -q bookdoe/app openreads
	node --check bookdoe/app/static/app.js
	node --check bookdoe/app/static/sw.js bookdoe/app/static/pwa.js
	sh scripts/check-esm.sh
	node --check openreads/static/app.js

# Seeds a temp database, starts the app on a free port, runs the horizontal-overflow
# sweep and the mobile-nav checks headless at 360 and 390 px, then tears down. Needs
# `npm install && npx playwright install chromium` once; set PYTHON to pick the
# interpreter that has bookdoe's requirements. See scripts/README-ui-qa.md.
uigate:
	node scripts/ui-gate.mjs

# Renders the real UI against a real server and asserts the DOM. Needs jsdom@22 and
# a seeded instance on :8899 - see scripts/README-ui-render-check.md. Kept out of
# `check` because it needs a running server, not because it is optional.
uicheck:
	node scripts/ui-render-check.mjs

# Real browser, real viewports, real data. See scripts/README-ui-qa.md - needs
# playwright + chromium and a seeded instance on :8899.
uiqa:
	node scripts/ui-sweep.mjs
	node scripts/ui-flows.mjs
	node scripts/ui-mobile-nav.mjs
	node scripts/ui-metadata-picker.mjs
	node scripts/ui-search.mjs
