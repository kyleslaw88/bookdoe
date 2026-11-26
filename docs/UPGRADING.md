# Upgrading from alexandria to bookdoe

The product, the Python service directory (`alexandria/` is now `bookdoe/`), the container,
the image and the environment variables were renamed from alexandria to bookdoe. An existing
install keeps working without edits; do these steps when you are ready, in any order.

## Environment variables

Every `ALEXANDRIA_X` is now `BOOKDOE_X`. At start-up a `BOOKDOE_X` that is not set falls back to
`ALEXANDRIA_X`, and the log carries one `DEPRECATED` line per old variable that was used. This
covers the process environment, the compose `env_file` and `<config dir>/setup.env`. The
fallback will be removed in a later release, so rename the variables (`sed -i 's/^ALEXANDRIA_/BOOKDOE_/'`
on your env files). If both names are set, `BOOKDOE_X` wins.

`docker-compose.yml` now reads `BOOKDOE_IMAGE`, `BOOKDOE_PORT`, `BOOKDOE_CONFIG_DIR`,
`BOOKDOE_PUBLIC_URL` and `BOOKDOE_CONTAINER_NAME` from the root `.env`, and the service env file
is `bookdoe/.env` (it was `alexandria/.env`). The compose file does not fall back to the old
names, so rename those keys in the root `.env`, and move the service env file, before you
`docker compose up` with the new file.

## Database and data directory

A new install creates `<config dir>/bookdoe.db`. If `<config dir>/alexandria.db` exists and
`bookdoe.db` does not, the old file keeps being used (with a warning), and nothing is moved.
To switch: stop the container, `mv alexandria.db bookdoe.db` (and the `-wal` / `-shm` files, if
any), start it. If `BOOKDOE_DB_PATH` (or `ALEXANDRIA_DB_PATH`) is set, that path is used as is.

Backups and pre-migration copies are now named `bookdoe-*.db` / `bookdoe.db.bak-*`. Older
`alexandria-*` files are left where they are and are no longer rotated; delete them when you no
longer need them.

## Trash folders

Each managed root's trash folder is now `.bookdoe-trash`. Files already in a `.alexandria-trash`
folder can still be listed, restored and purged, and scans skip it. New removals go to the new folder.

## Sign-in

The session, device and OIDC cookies are now `bookdoe_session`, `bookdoe_device` and
`bookdoe_oidc` (`__Host-` prefixed over https). Browsers sign in once after the upgrade and
re-trust their device. API keys and device tokens are unaffected.

## OIDC groups

The default group names are `bookdoe-admin`, `bookdoe-adult` and `bookdoe-kid`. While a group
variable is left at its default, the old default (`alexandria-admin` and so on) is still accepted.
A variable you set yourself is used exactly as written.

## Container and CLI

The in-container user, group and CLI are `bookdoe` (`docker compose exec bookdoe bookdoe status`).
The compose service is `bookdoe`; a deploy that targets the old service name needs the new one.
File ownership in `/config` follows `PUID` / `PGID`, so volumes need no change.
