#!/bin/sh
set -eu
PUID="${PUID:-99}"; PGID="${PGID:-100}"; UMASK="${UMASK:-002}"; umask "$UMASK"
if [ "$(id -u)" = "0" ]; then
  groupmod -o -g "$PGID" openreads 2>/dev/null || true
  usermod -o -u "$PUID" openreads 2>/dev/null || true
  chown -R "$PUID:$PGID" /data
  exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
