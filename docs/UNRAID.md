# Unraid deployment

## Suggested mounts

The root Compose file contains example paths; replace them with your actual Unraid shares/appdata locations.

```text
/mnt/cache/appdata/bookdoe  -> /config
/mnt/cache/appdata/openreads   -> /data
/mnt/user/ebooks               -> /books
/mnt/user/audiobooks           -> /audiobooks
/mnt/user/downloads            -> /downloads
Readarr ebook appdata          -> /migrations/readarr-ebooks:ro
Readarr audio appdata          -> /migrations/readarr-audio:ro
```

Set PUID/PGID to an identity that can read existing media and write only where managed import should be allowed. The supplied images use `gosu` to drop privileges before starting the application.

## Community Apps template

`unraid/bookdoe.xml` is a Docker template for the bookdoe container: copy it to
`/boot/config/plugins/dockerMan/templates-user/` and add the container from the Docker
tab. bookdoe needs the OpenReads container as well (set its address in the OpenReads
URL field), so the Compose route above is simpler. The template's image is
`bookdoe:local` until an image is published; build it with
`docker compose build` on the server. Listing in Community Apps itself is deferred until
the project is public and an image is published; until then the template is for manual use.

## Nginx Proxy Manager

bookdoe only needs a normal HTTP reverse proxy to container port 8787. HTTPS should terminate at NPM. Set `BOOKDOE_PUBLIC_URL` to the external HTTPS URL when using OIDC.

OpenReads normally remains private on the Docker network. bookdoe reaches it as `http://openreads:8790`.

## Authentik

Native OIDC is preferred to proxy-header authentication because bookdoe can validate the authorization flow itself. Configure the Authentik redirect URI to bookdoe's callback endpoint as shown by the application's login flow/provider setup.

## Path semantics

Do not assume the downloader and bookdoe see the same path. If SABnzbd reports `/downloads/complete/book` but bookdoe sees `/data/complete/book`, configure a remote path mapping in Settings.

## Initial deployment checklist

- Run `sh install.sh` from the checkout (it writes the root `.env` with your host
  paths and port, generates `BOOKDOE_SECRET_KEY` into `bookdoe/.env`, creates the
  data directories and starts the stack), or copy `.env.example` to `.env` and
  `bookdoe/.env.example` to `bookdoe/.env` by hand. See `docs/INSTALL.md`.
- Finish in the browser at `http://UNRAID-IP:8795/setup` (administrator, library
  roots, metadata keys, secrets backend) or with
  `docker compose exec bookdoe bookdoe setup`.
- Replace sample host paths in the root `.env` (`BOOKS_DIR`, `AUDIOBOOKS_DIR`, ...).
- Verify container UID/GID access before enabling imports.
- Mount Readarr configs read-only.
- Start with scheduler enabled but automatic wanted search can be disabled from Jobs until search scoring is verified.
- Run a manual database backup after first successful configuration.
