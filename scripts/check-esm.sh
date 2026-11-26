#!/bin/sh
# Syntax-check the ES-module browser bundles. `node --check` only parses a .js file as
# a classic script (where `export` is a syntax error), so each module is checked
# through a temporary .mjs copy. Run from the repo root: sh scripts/check-esm.sh
set -eu
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
status=0
# Every static/*.js is an ES module except the classic scripts below (checked with plain
# `node --check` in CI), so a new module is picked up without editing this file.
for f in bookdoe/app/static/*.js; do
  case "$f" in
    bookdoe/app/static/app.js) continue ;;
    bookdoe/app/static/sw.js) continue ;;
    bookdoe/app/static/pwa.js) continue ;;
  esac
  cp "$f" "$tmp/check.mjs"
  if node --check "$tmp/check.mjs"; then echo "ok   $f"; else echo "FAIL $f"; status=1; fi
done
exit "$status"
