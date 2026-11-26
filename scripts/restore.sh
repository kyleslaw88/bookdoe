#!/bin/sh
# Restore bookdoe's database from a verified backup.
#
#   sh scripts/restore.sh <backup.db> --yes
#
# Run on the server from the compose checkout. The backup may be an bookdoe-*.db or
# a premigrate-*.db from the backups directory. If the sidecar <backup>.json exists the
# SHA-256 must match or nothing is touched. The live database (and its -wal/-shm) is
# moved aside to bookdoe.db.bak-<timestamp>, never deleted.
set -eu

cd "$(dirname "$0")/.."

BACKUP=""
YES=0
for arg in "$@"; do
    case "$arg" in
        --yes) YES=1 ;;
        -*) echo "restore: unknown option $arg" >&2; exit 2 ;;
        *) BACKUP="$arg" ;;
    esac
done

if [ -z "$BACKUP" ]; then
    echo "usage: sh scripts/restore.sh <backup.db> --yes" >&2
    exit 2
fi
if [ ! -f "$BACKUP" ]; then
    echo "restore: backup file not found: $BACKUP" >&2
    exit 1
fi
if [ "$YES" != 1 ]; then
    echo "restore: this stops bookdoe and replaces its live database with $BACKUP." >&2
    echo "restore: re-run with --yes to proceed." >&2
    exit 2
fi

# Live database directory (host side of the /config mount). Read from the root .env
# (BOOKDOE_CONFIG_DIR, what docker-compose.yml mounts) or override with LIVE_DIR.
if [ -z "${LIVE_DIR:-}" ] && [ -f .env ]; then
    LIVE_DIR=$(sed -n 's/^BOOKDOE_CONFIG_DIR=//p' .env | head -n1)
fi
LIVE_DIR="${LIVE_DIR:-./data/bookdoe}"
LIVE="$LIVE_DIR/bookdoe.db"
if [ -z "${HEALTH_URL:-}" ] && [ -f .env ]; then
    PORT=$(sed -n 's/^BOOKDOE_PORT=//p' .env | head -n1)
    HEALTH_URL="http://127.0.0.1:${PORT:-8795}/readyz"
fi
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8795/readyz}"
SIDECAR="${BACKUP%.db}.json"

if [ -f "$SIDECAR" ]; then
    WANT=$(sed -n 's/.*"sha256": *"\([0-9a-f]*\)".*/\1/p' "$SIDECAR" | head -n1)
    GOT=$(sha256sum "$BACKUP" | cut -d' ' -f1)
    if [ -z "$WANT" ] || [ "$WANT" != "$GOT" ]; then
        echo "restore: SHA-256 mismatch for $BACKUP (manifest $WANT, file $GOT); aborting, nothing changed" >&2
        exit 1
    fi
    echo "restore: checksum verified against $SIDECAR"
else
    echo "restore: WARNING no sidecar $SIDECAR; checksum not verified" >&2
fi

TS=$(date +%Y%m%dT%H%M%S)

echo "restore: stopping bookdoe"
docker compose stop bookdoe

if [ -f "$LIVE" ]; then
    mv "$LIVE" "$LIVE.bak-$TS"
    [ -f "$LIVE-wal" ] && mv "$LIVE-wal" "$LIVE.bak-$TS-wal"
    [ -f "$LIVE-shm" ] && mv "$LIVE-shm" "$LIVE.bak-$TS-shm"
    echo "restore: previous database kept as $LIVE.bak-$TS"
fi

cp "$BACKUP" "$LIVE"
# Match the ownership of the directory (PUID/PGID), not root.
chown "$(stat -c '%u:%g' "$LIVE_DIR")" "$LIVE" 2>/dev/null || true

echo "restore: starting bookdoe"
docker compose start bookdoe

i=0
until curl -fsS -o /dev/null "$HEALTH_URL"; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "restore: FAILED, $HEALTH_URL not ready after 120s; the previous database is at $LIVE.bak-$TS" >&2
        exit 1
    fi
    sleep 2
done
echo "restore: OK, restored $BACKUP and $HEALTH_URL is ready (previous database: $LIVE.bak-$TS)"
