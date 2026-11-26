# Installing bookdoe

## What you need

- A Linux host (or Unraid, see `docs/UNRAID.md`) with Docker and the Compose plugin
  v2.24 or later (`docker compose version`).
- The directories holding your ebooks and audiobooks. They are mounted read-only;
  bookdoe adopts files in place and never moves them.
- Optional: a download client (SABnzbd, qBittorrent, NZBGet, Transmission), an
  indexer (Prowlarr, Newznab, Torznab), an identity provider (OIDC), Infisical.

## One-command install

```sh
git clone <repository> bookdoe
cd bookdoe
sh install.sh
```

The script asks for the port, the host paths and the user id to run as, writes the
root `.env`, generates `BOOKDOE_SECRET_KEY` into `bookdoe/.env`, copies
`openreads/.env.example` to `openreads/.env`, creates the data directories, builds
the images and waits for `/readyz`. `sh install.sh --yes` takes every default (data
under `./data`); `--no-start` only writes the files. Existing files are never
overwritten, so re-running it is safe.

Then open `http://<host>:<port>/setup`. The wizard runs once, while the install has
no accounts, and walks through:

1. the administrator account and the public URL;
2. library roots as seen inside the container (`/books`, `/audiobooks`, the writable
   `/library/ebooks` and `/library/audiobooks` for managed imports), each checked for
   existence and permissions;
3. metadata sources (OpenReads URL, optional Google Books, Hardcover and AudiobookDB
   keys);
4. the secrets backend: environment and config volume (default) or Infisical, with a
   live connectivity check;
5. review and finish.

Everything the wizard decides goes to the config volume (`/config/setup.env`, mode
0600, and `/config/setup.json` without secret values), never into the image. If a
variable needs a restart to take effect the result says so:
`docker compose restart bookdoe`.

Without a browser:

```sh
docker compose exec bookdoe bookdoe setup                 # interactive
ADMIN_PW='a long password' docker compose exec -e ADMIN_PW bookdoe \
  bookdoe setup --non-interactive --admin-user admin --admin-password-env ADMIN_PW \
  --root "Books:/books:ebook:adopt" --root "Audiobooks:/audiobooks:audiobook:adopt" \
  --public-url https://books.example.com
```

`bookdoe status` and `bookdoe secrets-check` are also available.

## By hand

```sh
cp .env.example .env                       # host paths, port, PUID/PGID
cp bookdoe/.env.example bookdoe/.env # set BOOKDOE_SECRET_KEY (openssl rand -hex 32)
cp openreads/.env.example openreads/.env
docker compose up -d --build
```

If `BOOKDOE_SECRET_KEY` is left empty the container generates one into
`/config/setup.env` on first start and logs that it did. It still refuses to run with
an empty key in `proxy` or `oidc` mode when the file is not writable.

## Volumes and paths

| Host (`.env`) | Container | Mode | Purpose |
| --- | --- | --- | --- |
| `BOOKDOE_CONFIG_DIR` | `/config` | rw | database, backups, covers, `setup.env` |
| `OPENREADS_DATA_DIR` | `/data` (openreads) | rw | OpenReads database and cache |
| `BOOKS_DIR` | `/books` | ro | existing ebooks |
| `AUDIOBOOKS_DIR` | `/audiobooks` | ro | existing audiobooks |
| `DOWNLOADS_DIR` | `/downloads` | ro | download client completed folder |
| `MIGRATIONS_DIR` | `/migrations` | ro | Readarr database snapshots (optional) |
| `LIBRARY_EBOOKS_DIR` | `/library/ebooks` | rw | managed-import destination |
| `LIBRARY_AUDIOBOOKS_DIR` | `/library/audiobooks` | rw | managed-import destination |

Run as the user that owns the media (`PUID`/`PGID`); the entrypoint drops privileges
with `gosu`. Download clients often see different paths than bookdoe; set a remote
path mapping in Settings if `/downloads/complete/x` on the client is `/data/complete/x`
here.

## Reverse proxy and HTTPS

Proxy to container port 8787 (published as `BOOKDOE_PORT`). Set
`BOOKDOE_PUBLIC_URL` to the external URL; OIDC and device pairing use it. OpenReads
is only exposed on the private Compose network. With `BOOKDOE_AUTH_MODE=proxy`,
read the trusted-proxy notes in `docs/SECURITY.md` first.

**The iOS app refuses plain `http://` to anything that is not a local-network host**
(App Transport Security; `NSAllowsArbitraryLoads` is off). `http://192.168.x.x:8795`,
`.local` names and bare hostnames work; a public name or a tunnel needs HTTPS. To use
the iOS app away from home, terminate TLS in a reverse proxy in front of port 8795:

```text
# Caddy: automatic certificates
books.example.com {
    reverse_proxy localhost:8795
}
```

Nginx Proxy Manager (Unraid), Traefik and the Synology reverse proxy work the same
way; see `docs/UNRAID.md` and `docs/SYNOLOGY.md`. Forward WebSocket and large
streaming requests unbuffered, and set `BOOKDOE_PUBLIC_URL` to the HTTPS URL. With an https public URL, bookdoe sends
`Strict-Transport-Security` and hides `/docs`, `/redoc` and `/openapi.json`; set
`BOOKDOE_API_DOCS=1` to keep them. A `Content-Security-Policy` is always sent.

## Upgrading

```sh
git pull
docker compose up -d --build
```

Migrations are additive and take a `premigrate-*` backup first. Rollback and restore:
`docs/OPERATIONS.md`. On a server checkout, `scripts/deploy.sh` does the pull, backup,
build, tag-by-sha, restart and health/OpenReads check in one step with a read-only
deploy key, and `scripts/deploy.sh --rollback [sha]` redeploys a previously built
image without rebuilding.

## Uninstall

```sh
docker compose down
rm -rf ./data           # or whatever BOOKDOE_CONFIG_DIR and OPENREADS_DATA_DIR point at
```

Your media directories are never written to except the managed-import destinations.

## Phone apps

The web Player works on any phone as an installable PWA. Native apps live in the
same repository and sign in to this server with your username and password or an
bookdoe pairing code: iOS (`ios/`, build and TestFlight in
[IOS_BUILD.md](IOS_BUILD.md)) and Android (`android/`, build, sideload and Play
internal testing in [ANDROID_BUILD.md](ANDROID_BUILD.md)). Both also connect to
Jellyfin and Audiobookshelf servers. Plain `http://` to a LAN address works in both;
for anything reachable from outside, put the server behind HTTPS first. A step by
step test and release checklist is in
[MOBILE_TESTING_AND_DEPLOYMENT.md](MOBILE_TESTING_AND_DEPLOYMENT.md).

## Unraid and Synology

Synology: `docs/SYNOLOGY.md`. Unraid: `docs/UNRAID.md`, including a Community Apps
template (`unraid/bookdoe.xml`). The Compose file works with the Compose Manager plugin; put the
host paths in `.env` and use the appdata share for `BOOKDOE_CONFIG_DIR`.
