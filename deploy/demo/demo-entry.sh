#!/bin/sh
# Demo wrapper: fresh state from the seed at every start; exits at the next 11:00 UTC so Docker restarts it.
# Env overrides (used by tests): SEED_DIR, CONFIG_DIR, ENTRYPOINT, DEMO_DRY_RUN=1 (stop before exec).
SEED_DIR=${SEED_DIR:-/seed}; CONFIG_DIR=${CONFIG_DIR:-/config}; ENTRYPOINT=${ENTRYPOINT:-/entrypoint.sh}
if [ -z "$SEED_MODE" ]; then
  n=$(find "$SEED_DIR" -maxdepth 1 -type f -name '*.db' | wc -l)
  if [ "$n" -ne 1 ]; then echo "demo: $SEED_DIR must hold exactly one .db file, found $n" >&2; exit 1; fi
  seed_db=$(find "$SEED_DIR" -maxdepth 1 -type f -name '*.db')
  find "$CONFIG_DIR" -mindepth 1 -delete || { echo "demo: cannot wipe $CONFIG_DIR" >&2; exit 1; }
  cp "$seed_db" "$CONFIG_DIR/bookdoe.db" || exit 1
  [ -d "$SEED_DIR/covers" ] && cp -a "$SEED_DIR/covers" "$CONFIG_DIR/covers"
  [ -n "$DEMO_DRY_RUN" ] || chown -R "${PUID:-1000}:${PGID:-100}" "$CONFIG_DIR"
  now=$(date -u +%s); next=$(( (now/86400)*86400 + 11*3600 )); [ "$next" -le "$((now+600))" ] && next=$((next+86400))
  secs=$((next-now)); echo "demo: fresh state loaded, auto-reset in ${secs}s"
  [ -n "$DEMO_DRY_RUN" ] && exit 0
  exec timeout "$secs" "$ENTRYPOINT" "$@"
fi
exec "$ENTRYPOINT" "$@"
