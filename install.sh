#!/bin/sh
# One-command install for bookdoe + OpenReads with Docker Compose.
#
#   sh install.sh                 # interactive: asks for paths and port, then starts
#   sh install.sh --yes           # accept every default (data under ./data)
#   sh install.sh --no-start      # write configuration only
#
# Safe to re-run: existing .env files are never overwritten (missing keys are not
# added either; edit them by hand or delete the file to regenerate). Nothing here
# touches the image; every value lands in .env, bookdoe/.env, openreads/.env and
# the config volume. Afterwards open http://<host>:<port>/setup for the wizard, or
# run `docker compose exec bookdoe bookdoe setup` for the CLI version.
set -eu

cd "$(dirname "$0")"

YES=0
START=1
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --no-start) START=0 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "install: unknown option $arg" >&2; exit 2 ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "install: docker is not installed; see https://docs.docker.com/engine/install/" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "install: the 'docker compose' plugin (v2.24 or later) is required" >&2
    exit 1
fi

ask() {
    # ask VAR "prompt" default
    var="$1"; prompt="$2"; default="$3"
    if [ "$YES" = 1 ] || [ ! -t 0 ]; then
        eval "$var=\"\$default\""
        return
    fi
    printf '%s [%s]: ' "$prompt" "$default"
    read -r answer || answer=""
    [ -n "$answer" ] || answer="$default"
    eval "$var=\"\$answer\""
}

# Indexers and download clients are optional: say nothing about them unless the flag is on.
acquisition_on() {
    case "$(printf '%s' "${BOOKDOE_ACQUISITION_ENABLED:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

random_hex() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_hex(32))'
    else
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

if [ -f .env ]; then
    echo "install: .env exists, keeping it"
else
    echo "bookdoe install. Paths are on this host; press Enter to accept a default."
    ask BOOKDOE_PORT "Port to publish bookdoe on" "8795"
    ask BOOKDOE_CONFIG_DIR "Config/database directory" "./data/bookdoe"
    ask OPENREADS_DATA_DIR "OpenReads data directory" "./data/openreads"
    ask BOOKS_DIR "Existing ebook library (read-only)" "./data/books"
    ask AUDIOBOOKS_DIR "Existing audiobook library (read-only)" "./data/audiobooks"
    if acquisition_on; then
        ask DOWNLOADS_DIR "Download client completed folder (read-only)" "./data/downloads"
    else
        DOWNLOADS_DIR="./data/downloads"
    fi
    ask LIBRARY_EBOOKS_DIR "Managed ebook destination (writable)" "./data/library/ebooks"
    ask LIBRARY_AUDIOBOOKS_DIR "Managed audiobook destination (writable)" "./data/library/audiobooks"
    ask MIGRATIONS_DIR "Readarr database snapshots for migration (optional)" "./data/migrations"
    ask PUID "User id the containers run as" "$(id -u)"
    ask PGID "Group id the containers run as" "$(id -g)"
    ask BOOKDOE_PUBLIC_URL "Public URL (how browsers and the app reach it)" "http://localhost:$BOOKDOE_PORT"
    {
        echo "# Written by install.sh. Host paths and ports for docker-compose.yml."
        echo "BOOKDOE_PORT=$BOOKDOE_PORT"
        echo "BOOKDOE_PUBLIC_URL=$BOOKDOE_PUBLIC_URL"
        echo "BOOKDOE_CONFIG_DIR=$BOOKDOE_CONFIG_DIR"
        echo "OPENREADS_DATA_DIR=$OPENREADS_DATA_DIR"
        echo "BOOKS_DIR=$BOOKS_DIR"
        echo "AUDIOBOOKS_DIR=$AUDIOBOOKS_DIR"
        echo "DOWNLOADS_DIR=$DOWNLOADS_DIR"
        echo "LIBRARY_EBOOKS_DIR=$LIBRARY_EBOOKS_DIR"
        echo "LIBRARY_AUDIOBOOKS_DIR=$LIBRARY_AUDIOBOOKS_DIR"
        echo "MIGRATIONS_DIR=$MIGRATIONS_DIR"
        echo "PUID=$PUID"
        echo "PGID=$PGID"
        echo "UMASK=002"
    } > .env
    echo "install: wrote .env"
fi

# Read back whatever .env holds (first run or a kept file).
getvar() { sed -n "s/^$1=//p" .env | head -n1; }
for d in "$(getvar BOOKDOE_CONFIG_DIR)" "$(getvar OPENREADS_DATA_DIR)" "$(getvar BOOKS_DIR)" "$(getvar AUDIOBOOKS_DIR)" \
         "$(getvar DOWNLOADS_DIR)" "$(getvar LIBRARY_EBOOKS_DIR)" "$(getvar LIBRARY_AUDIOBOOKS_DIR)" "$(getvar MIGRATIONS_DIR)"; do
    [ -n "$d" ] && mkdir -p "$d"
done

if [ -f bookdoe/.env ]; then
    echo "install: bookdoe/.env exists, keeping it"
else
    umask 077
    {
        echo "# Written by install.sh. Application settings for the bookdoe container."
        echo "# Generated locally; never committed. See .env.example for every option."
        echo "BOOKDOE_SECRET_KEY=$(random_hex)"
        echo "BOOKDOE_AUTH_MODE=local"
        echo "BOOKDOE_LOG_LEVEL=INFO"
        echo "BOOKDOE_SECRETS_BACKEND=env"
    } > bookdoe/.env
    umask 022
    echo "install: wrote bookdoe/.env (with a generated secret key)"
fi

if [ -f openreads/.env ]; then
    echo "install: openreads/.env exists, keeping it"
else
    cp openreads/.env.example openreads/.env
    echo "install: wrote openreads/.env from the example"
fi

if [ "$START" = 0 ]; then
    echo "install: configuration written; start with: docker compose up -d --build"
    exit 0
fi

echo "install: pulling images and starting"
if ! docker compose pull; then
    echo "install: pull failed; building from source instead (a few minutes)"
    docker compose build
fi
docker compose up -d

PORT="$(getvar BOOKDOE_PORT)"
URL="http://127.0.0.1:${PORT:-8795}"
echo "install: waiting for $URL/readyz"
i=0
until curl -fsS -o /dev/null "$URL/readyz" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 90 ]; then
        echo "install: not ready after 180s; check: docker compose logs bookdoe" >&2
        exit 1
    fi
    sleep 2
done
echo
echo "bookdoe is running. Finish setup in your browser: $(getvar BOOKDOE_PUBLIC_URL)/setup"
echo "(or without a browser: docker compose exec bookdoe bookdoe setup)"
