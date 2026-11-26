#!/bin/sh
set -eu
PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"
umask "$UMASK"
if [ "$(id -u)" = "0" ]; then
  groupmod -o -g "$PGID" bookdoe 2>/dev/null || true
  usermod -o -u "$PUID" bookdoe 2>/dev/null || true
  mkdir -p /config
  chown -R "$PUID:$PGID" /config
  # First start without BOOKDOE_SECRET_KEY: generate one into the config volume
  # (never into the image) as the application user, so the file is owned by it.
  gosu "$PUID:$PGID" python -m app.cli ensure-secret || true
  exec gosu "$PUID:$PGID" "$@"
fi
python -m app.cli ensure-secret || true
exec "$@"
