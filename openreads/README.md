# openreads

A drop-in metadata backend for Readarr, API-compatible with
[rreading-glasses](https://github.com/blampe/rreading-glasses)'s contract, backed by
[Open Library](https://openlibrary.org) and [Google Books](https://developers.google.com/books)
instead of Goodreads' Amazon-owned AppSync API — which has been unreliable/broken for
third-party clients.

Open Library is the source of truth for the author → work → edition graph. Google Books
fills in gaps (cover, description, page count) at the edition level only, and is entirely
optional — the service works fine without a Google Books API key, just with thinner
metadata on some editions.

## Why

Readarr's built-in metadata source and popular self-hosted replacements like
rreading-glasses depend on scraping or reverse-engineered access to Goodreads, which
Amazon has progressively locked down. openreads avoids that dependency entirely by using
Open Library, a free and structured bibliographic API with no key required.

## Endpoints

Mirrors the `internal.*` schemas from rreading-glasses' own OpenAPI spec:

- `GET /search?q=`
- `GET /author/{id}`, `DELETE /author/{id}` (force refresh)
- `GET /work/{id}`
- `GET /book/{id}`, `GET /book/isbn/{isbn}`, `GET /book/asin/{asin}`
- `GET|POST /bulk`, `GET|POST /book/bulk`
- `GET /series/{id}`
- `GET /recommended`
- `GET /health`

## Running it

```bash
cp .env.example .env   # optionally set GOOGLE_BOOKS_API_KEY
docker compose up -d --build
```

Point Readarr's metadata source at `http://<host>:8790` (or a reverse-proxied hostname).
For forks that only expose a single `READARR_METADATA_URL`-style override (e.g.
[readarr-rresurrected](https://github.com/ricetim/readarr-rresurrected)), point that
directly at this service.

Note: some Readarr builds route free-text search through a separate, hardcoded search
path unrelated to the configured metadata source. See the upstream project's docs for
whether your build's "Add New" search actually calls out to a custom metadata source.

## Series enrichment from AudiobookDB

Open Library often has no series data (or no position) for a book. `audiobookdb.py`
looks the book up on [AudiobookDB](https://audiobookdb.org) (a free, documented audiobook
catalog API) by title + author and fills the gap. It is a per-book enrichment source, not a
catalog: it never adds works or editions. Series names are matched through `seriesnorm.py`
so "Molly Murphy Mysteries" (AudiobookDB) and "Molly Murphy" (Open Library) share one row.

Priority: AudiobookDB wins series name/position where it is consulted (only for works with
no series or a series lacking a position); everything else stays with Open Library / Google
Books. Books AudiobookDB doesn't know are left exactly as Open Library reported them.

Rate limiting: requests are spaced >= 0.25 s apart, counted per UTC day, and paused when the
server's `x-ratelimit-budget` header runs low or on 429/5xx/timeouts. Every answer (including
"not found") is cached in SQLite -- 30 days for hits, 7 for misses -- and an author build
makes at most `AUDIOBOOKDB_MAX_LOOKUPS_PER_BUILD` new lookups, so large authors fill in over
successive refreshes. `/bulk` only reads the cache. Every path that can call out -- author builds
and individual `/work/{id}` refreshes -- also draws from one shared token bucket
(`AUDIOBOOKDB_BURST`, default 25, refilling one per `AUDIOBOOKDB_REFILL_SECONDS`, default 10);
when it is empty a request is served from cache and the lookup happens on a later refresh.
Identical in-flight lookups (same title + author) are coalesced into one outbound request.

Environment (all optional): `AUDIOBOOKDB_ENABLED` (default `1`), `AUDIOBOOKDB_API_KEY`
(raises the limits; also raise `AUDIOBOOKDB_DAILY_CAP`), `AUDIOBOOKDB_DAILY_CAP` (default 400
anonymous / 9000 with a key), `AUDIOBOOKDB_MAX_LOOKUPS_PER_BUILD` (default 25),
`AUDIOBOOKDB_MIN_INTERVAL` (seconds, default 0.25).

## Data model

SQLite (`db.py`) is the ID-mapping authority: Open Library's string keys
(`OL123456W`, `OL123456A`, ...) are mapped to stable integer IDs, since Readarr's
`ForeignId` fields are typed as integers and must persist across restarts. Authors,
works, and editions each have their own auto-incrementing ID space — they are not a
unified namespace the way real Goodreads IDs are, so callers should not assume a
"book ID" and "work ID" are interchangeable without going through this service's own
`/search` or `/bulk` responses.

## License

MIT
