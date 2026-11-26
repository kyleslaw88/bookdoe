# bookdoe 1.0.0

First public release of bookdoe, a self-hosted manager for ebooks and audiobooks.

## Highlights

- Adopts existing libraries in place (read-only) and writes only to managed roots.
- Search, download-client hand-off, managed import, metadata refresh, file-health checks
  and daily backups.
- One-time, read-only migration from Readarr databases.
- Native iOS and Android apps that also speak Jellyfin and Audiobookshelf.
- OIDC sign-in, API keys and optional Infisical secrets.

## Install

```sh
git clone https://github.com/<owner>/bookdoe
cd bookdoe
cp .env.example .env
docker compose up -d
```

Then open `http://localhost:8795/setup`. Tagged releases publish the server image to
`ghcr.io/<owner>/bookdoe` (set `BOOKDOE_IMAGE` to use it). Details: [docs/INSTALL.md](INSTALL.md).

## Upgrading from earlier builds

Environment variables, databases and trash folders from earlier prototype builds keep
working; see [UPGRADING.md](UPGRADING.md).

## Known limits

Automatic wanted search is off by default (it can grab the same release twice). See the
Status section of the README.
