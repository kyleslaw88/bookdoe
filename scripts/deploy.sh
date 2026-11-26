#!/bin/sh
# Deploy bookdoe from the git remote's main branch.
#
# Run on the server from the compose checkout:
#   sh scripts/deploy.sh               # fast-forward to origin/main, backup, build, tag by sha, restart, health+smoke check
#   sh scripts/deploy.sh --check        # show what would deploy, change nothing
#   sh scripts/deploy.sh --rollback [sha]  # redeploy the image tagged sha (default: the one the
#                                       # last deploy or rollback replaced) with no rebuild and no backup
#
# Use a READ-ONLY deploy key for the checkout (for example via core.sshCommand in
# the local .git/config; keep the key outside the repository). The script never
# pushes. Every deploy is logged to .deploy/history.log (git-excluded); the sha this
# checkout last deployed and the one it replaced are kept in .deploy/current and
# .deploy/previous (also git-excluded) so a plain `--rollback` with no argument
# knows what to go back to.
#
# Both services are built with an image tag matching the deployed git sha
# (BOOKDOE_IMAGE / OPENREADS_IMAGE, same compose variables docker-compose.yml
# already keys off); the sha is also re-tagged as the :local default so a plain
# `docker compose up -d --build` after this script still finds it. Rollback checks
# both tagged images exist locally before touching anything running.
#
# There is one long-running process to protect across a restart: the in-process job
# scheduler (services/scheduler.py) runs backups, conversions, metadata refresh and
# search on a thread pool. The app's own shutdown already waits for the pool to
# finish (app/main.py lifespan), so the deploy's job here is to (a) not start the
# restart while something expensive is running, and (b) give the container's stop
# enough time for that in-app wait to complete rather than being SIGKILLed early.
# Both docker services are single-container (no replicas to drain across), so there
# is no on/off drain toggle to flip -- just a bounded wait on /readyz's reported
# running jobs, then a restart with a generous stop timeout.
#
# bookdoe deliberately reports OpenReads as "degraded" (200, not 503) on /readyz
# day to day, so an unrelated OpenReads outage never makes deploy.sh roll back a
# healthy bookdoe. But this deploy just rebuilt OpenReads too, so a degraded
# OpenReads right after *this* restart is not an unrelated sibling outage -- it is
# this deploy failing. That is the one thing /readyz's own pass/fail does not cover,
# so it is the extra smoke check added below.
#
# Environment:
#   HEALTH_URL        readiness endpoint (default: http://127.0.0.1:$BOOKDOE_PORT/readyz)
#   DRAIN_WAIT_MAX_S  longest wait for scheduler jobs to clear before restarting anyway (default 300)
#   DRAIN_POLL_S      seconds between polls while waiting (default 5)
#   STOP_GRACE_S      stop timeout passed to `docker compose up --timeout`, giving the app's own
#                     graceful shutdown (scheduler.shutdown_pool) room to finish an in-flight job
#                     before the container is killed (default 60)
set -eu

cd "$(dirname "$0")/.."

if [ -z "${HEALTH_URL:-}" ] && [ -f .env ]; then
    PORT=$(sed -n 's/^BOOKDOE_PORT=//p' .env | head -n1)
    HEALTH_URL="http://127.0.0.1:${PORT:-8795}/readyz"
fi
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8795/readyz}"
DRAIN_WAIT_MAX_S="${DRAIN_WAIT_MAX_S:-300}"
DRAIN_POLL_S="${DRAIN_POLL_S:-5}"
STOP_GRACE_S="${STOP_GRACE_S:-60}"
mkdir -p .deploy

ROLLBACK=0
CHECK=0
TARGET=""
case "${1:-}" in
    "") ;;
    --check) CHECK=1 ;;
    --rollback) ROLLBACK=1; TARGET="${2:-}"
        case "${3:-}" in "") ;; *) echo "usage: deploy.sh [--check | --rollback [sha]]" >&2; exit 2 ;; esac ;;
    *) echo "usage: deploy.sh [--check | --rollback [sha]]" >&2; exit 2 ;;
esac

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "deploy: tracked files have local changes; refusing to deploy:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

# ---------- helpers ----------

readyz_body() {
    curl -fsS "$HEALTH_URL" 2>/dev/null || true
}

# Best-effort: true (0) only when the readyz body says no scheduler jobs are running.
# Any other shape (jobs present, scheduler disabled/skipped, unreachable) is "not idle",
# which just means the bounded wait below runs its course instead of returning early.
scheduler_idle() {
    body="$(readyz_body)"
    case "$body" in
        *'"running":[]'*) return 0 ;;
        *) return 1 ;;
    esac
}

wait_for_scheduler_idle() {
    waited=0
    while :; do
        scheduler_idle && return 0
        if [ "$waited" -ge "$DRAIN_WAIT_MAX_S" ]; then
            echo "deploy: still showing running scheduler jobs after ${DRAIN_WAIT_MAX_S}s; restarting anyway (the app's own shutdown waits for them, up to \$STOP_GRACE_S=${STOP_GRACE_S}s)" >&2
            return 0
        fi
        sleep "$DRAIN_POLL_S"
        waited=$((waited + DRAIN_POLL_S))
    done
}

# Runs the exact backup the scheduler's own daily `database-backup` job runs (and OpenReads'
# alongside it when configured), tagged so it is easy to find before this deploy. No admin
# session is needed: it is invoked in-process, the same function the job and the authenticated
# "Back up now" button call.
backup_now() {
    echo "deploy: backup"
    docker compose exec -T bookdoe python -c "
from app.services import backup
import json
result = backup.create_backup(prefix='predeploy-${NEW}')
try:
    opened = backup.create_openreads_backup()
    if opened is not None:
        result['openreads'] = opened
except Exception as exc:
    result['openreads'] = {'error': str(exc)}
print(json.dumps(result))
" || { echo "deploy: FAILED: backup. Nothing was rebuilt or restarted." >&2; exit 1; }
}

health_wait() {
    echo "deploy: waiting for $HEALTH_URL"
    i=0
    until curl -fsS -o /dev/null "$HEALTH_URL"; do
        i=$((i + 1))
        if [ "$i" -ge 60 ]; then
            return 1
        fi
        sleep 2
    done
    return 0
}

# The one check /readyz itself will not fail on: OpenReads unreachable right after a
# deploy that just rebuilt and restarted OpenReads too (day to day this is tolerated
# as a 200 "degraded" so an unrelated sibling outage never triggers a rollback here).
openreads_smoke_check() {
    body="$(readyz_body)"
    case "$body" in
        *'"openreads":{"ok":false'*) return 1 ;;
        *) return 0 ;;
    esac
}

# ---------- rollback ----------

if [ "$ROLLBACK" = 1 ]; then
    [ "$CHECK" = 0 ] || { echo "deploy: --check and --rollback are not combined" >&2; exit 2; }
    CUR=$(cat .deploy/current 2>/dev/null || true)
    SHA="${TARGET:-$(cat .deploy/previous 2>/dev/null || true)}"
    if [ -z "$SHA" ]; then
        echo "deploy: no rollback target on record (.deploy/previous is empty) and none given; usage: deploy.sh --rollback <sha>" >&2
        exit 1
    fi
    if ! docker image inspect "bookdoe:$SHA" >/dev/null 2>&1 || ! docker image inspect "openreads:$SHA" >/dev/null 2>&1; then
        echo "deploy: FAILED: bookdoe:$SHA and/or openreads:$SHA are not on this host; nothing was changed." >&2
        exit 1
    fi

    echo "deploy: rollback to $SHA (current: ${CUR:-unknown}), no rebuild, no migration, no backup"
    wait_for_scheduler_idle

    export BOOKDOE_IMAGE="bookdoe:$SHA"
    export OPENREADS_IMAGE="openreads:$SHA"
    echo "deploy: restart with $BOOKDOE_IMAGE / $OPENREADS_IMAGE"
    docker compose up -d --no-build --timeout "$STOP_GRACE_S" || { echo "deploy: FAILED: restart. Check container state by hand." >&2; exit 1; }

    if ! health_wait; then
        echo "deploy: FAILED health check after 120s on rollback to $SHA" >&2
        echo "$(date -Iseconds) FAILED_ROLLBACK ${CUR:-unknown} -> $SHA" >> .deploy/history.log
        exit 1
    fi
    if ! openreads_smoke_check; then
        echo "deploy: FAILED: OpenReads is not healthy after the rollback to $SHA (see /api/v1/system/health)." >&2
        echo "$(date -Iseconds) FAILED_ROLLBACK ${CUR:-unknown} -> $SHA" >> .deploy/history.log
        exit 1
    fi

    printf '%s\n' "${CUR:-$SHA}" > .deploy/previous
    printf '%s\n' "$SHA" > .deploy/current
    echo "$(date -Iseconds) ROLLED_BACK ${CUR:-unknown} -> $SHA" >> .deploy/history.log
    echo "deploy: rolled back to $SHA and healthy"
    exit 0
fi

# ---------- forward deploy ----------

git fetch -q origin main
PREV=$(git rev-parse HEAD)
NEW=$(git rev-parse origin/main)

if [ "$PREV" = "$NEW" ]; then
    echo "deploy: already at $(git log --oneline -1 HEAD)"
    exit 0
fi

echo "deploy: $(git log --oneline -1 "$PREV")"
echo "     -> $(git log --oneline -1 "$NEW")"
git log --oneline "$PREV..$NEW"

if [ "$CHECK" = 1 ]; then
    exit 0
fi

git merge --ff-only -q origin/main

CUR=$(cat .deploy/current 2>/dev/null || true)
backup_now

export BOOKDOE_IMAGE="bookdoe:$NEW"
export OPENREADS_IMAGE="openreads:$NEW"
# Build stamp shown in the app (Management About, Player Settings). Only git, docker and
# sh builtins, plus date guarded: the NAS shell's coreutils are intermittently broken, and a
# missing stamp must never block a deploy.
GIT_SHA=$(git rev-parse --short=12 HEAD) || GIT_SHA=dev
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%MZ 2>/dev/null) || true
export GIT_SHA BUILD_TIME
echo "deploy: build (tag $NEW)"
docker compose build || { echo "deploy: FAILED: build. Nothing running was touched." >&2; exit 1; }
docker tag "bookdoe:$NEW" bookdoe:local || true
docker tag "openreads:$NEW" openreads:local || true

wait_for_scheduler_idle

echo "deploy: restart with $BOOKDOE_IMAGE / $OPENREADS_IMAGE"
docker compose up -d --no-build --timeout "$STOP_GRACE_S" || { echo "deploy: FAILED: restart. The images are built and tagged $NEW; retry the restart by hand or roll back." >&2; exit 1; }

if ! health_wait; then
    echo "deploy: FAILED health check after 120s on $NEW" >&2
    echo "$(date -Iseconds) FAILED $PREV -> $NEW" >> .deploy/history.log
    echo "deploy: to roll back: sh scripts/deploy.sh --rollback $PREV" >&2
    echo "deploy: (check whether the new version migrated the database before rolling back)" >&2
    exit 1
fi
if ! openreads_smoke_check; then
    echo "deploy: FAILED: OpenReads is not healthy after this deploy rebuilt and restarted it (see /api/v1/system/health)." >&2
    echo "$(date -Iseconds) FAILED $PREV -> $NEW" >> .deploy/history.log
    echo "deploy: to roll back: sh scripts/deploy.sh --rollback $PREV" >&2
    exit 1
fi

printf '%s\n' "${CUR:-$PREV}" > .deploy/previous
printf '%s\n' "$NEW" > .deploy/current
echo "$(date -Iseconds) OK $PREV -> $NEW" >> .deploy/history.log
echo "deploy: healthy at $(git log --oneline -1 HEAD)"
