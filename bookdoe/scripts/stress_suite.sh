#!/usr/bin/env bash
# Run the full bookdoe test suite N times (default 10) consecutively, each run from a
# clean state, and exit non-zero on the first run that fails. Proof artifact for #36,
# where about 1 in 3 full runs ended with 'sqlite3.OperationalError: disk I/O error'.
#
# Clean state per run: a fresh interpreter, a fresh TMPDIR (so pytest's tmp_path base
# and conftest's session root are new), no pytest cache (-p no:cacheprovider) and no
# .pytest_cache / __pycache__ carried over. There is no retry: a failing run stops the
# loop and its full output is printed.
#
# Usage, from the repository root or bookdoe/:
#   bookdoe/scripts/stress_suite.sh            # 10 runs
#   RUNS=3 bookdoe/scripts/stress_suite.sh     # fewer runs
# Needs the bookdoe requirements plus pytest on PATH (e.g. an activated venv).
set -u

BOOKDOE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNS="${RUNS:-10}"
PYTEST=(python -m pytest -o addopts= -q -p no:cacheprovider)

cd "$BOOKDOE_DIR" || exit 2
rm -rf .pytest_cache
find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null

LOG_DIR="$(mktemp -d)"
echo "command: (cd bookdoe && PYTHONPATH=. ${PYTEST[*]}) x ${RUNS}, fresh TMPDIR per run"

status=0
for i in $(seq 1 "$RUNS"); do
    run_tmp="$(mktemp -d)"
    log="$LOG_DIR/run-$i.log"
    start=$(date +%s)
    TMPDIR="$run_tmp" PYTHONPATH=. "${PYTEST[@]}" >"$log" 2>&1
    rc=$?
    secs=$(( $(date +%s) - start ))
    # pytest's summary is its last non-empty line, e.g. "412 passed, 15 skipped in 41.2s".
    summary="$(grep -v '^$' "$log" | tail -n 1)"
    printf 'run %2d/%d: exit %d (%ss) %s\n' "$i" "$RUNS" "$rc" "$secs" "$summary"
    rm -rf "$run_tmp"
    if [ "$rc" -ne 0 ]; then
        echo "--- run $i failed; full output:"
        cat "$log"
        status=1
        break
    fi
done

rm -rf "$LOG_DIR"
if [ "$status" -eq 0 ]; then
    echo "result: ${RUNS}/${RUNS} consecutive full-suite runs passed (exit 0)"
else
    echo "result: FAILED on run $i (exit 1)"
fi
exit "$status"
