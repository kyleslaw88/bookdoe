# Changelog

## 1.0.0 - 2026-11-18

First public release. Release notes: [docs/RELEASE_NOTES_1.0.0.md](docs/RELEASE_NOTES_1.0.0.md).

- Server and web UI under the name bookdoe: ebook and audiobook acquisition, in-place
  library adoption, managed imports, metadata, monitoring and a one-time Readarr migration.
- Native iOS and Android apps.
- Docker image published to `ghcr.io/<owner>/bookdoe` on version tags.
- Environment variables are `BOOKDOE_*`; the earlier prototype names keep working with a
  deprecation warning (see [docs/UPGRADING.md](docs/UPGRADING.md)).

Per-change history before 1.0.0 lives in `docs/changes/`.
