# Integrations

> Optional integrations; you are responsible for the content you download and for complying with the law where you live.

## Acquisition pipeline

A monitored, wanted asset is searched for, scored, grabbed, downloaded and imported into a managed root. The stages below are the lifecycle every acquisition follows, followed by how search, scoring, download clients and import behave.

## Lifecycle

```text
MONITOR
  -> DISCOVER
  -> QUERY PLAN
  -> SEARCH INDEXERS
  -> NORMALIZE RELEASES
  -> MATCH
  -> SCORE / REJECT
  -> GRAB
  -> DOWNLOAD
  -> POLL
  -> COMPLETE
  -> MANAGED IMPORT
  -> VERIFY
  -> OWNED
```

### Asset status through the lifecycle

For a wanted asset, `media_assets.status` moves `missing` -> `downloading` (set by grab) -> `owned` (set when every file of the import is in place). A cancelled download, a failed import or a download the client reports as failed returns the asset to `missing` (or `none` if it has no acquisition intent) so it can be searched again. `downloading` assets are not listed as wanted and are not searched. An owned asset is never demoted by a download.

The automatic wanted search additionally skips any asset that has a download in `grabbed`, `downloading`, `completed`, `import_pending` or `importing`, whatever the asset status says.

### Blocklist

When an import fails, or the client reports the job as failed, the release (indexer guid and title) is written to `blocklist` with a 30 day expiry and a `release.blocklisted` history event. This happens before the asset goes back to `missing`, so the next search scores that release as rejected ("Release is blocklisted") and picks another one. Expired rows are removed by the housekeeping job. A manual retry of the failed import is still possible and does not depend on the blocklist.

### Import safety

- Only one import can claim a download: the claim is `state IN ('completed','import_pending','import_failed')` -> `importing`, and a second caller is refused. A poll never overwrites `importing` or `imported`.
- Import is idempotent. File rows are upserted by path and the asset becomes owned once, after the last file, so a retry after a partial failure ends with one row per file.
- Copies are verified by content (full hash for files up to 64 MB, sampled fingerprint above), not just size. The `sha256` of small files and the fingerprint of all files are stored on the `media_files` row. A same-size file with different content at the destination is never reused.
- `move` mode is refused when the source is inside any `observe` or `adopt` root.

## Search

bookdoe supports Prowlarr as a first-class integration and generic Newznab/Torznab protocols. Query planning can use exact identifiers before title/author fallbacks. Indexer failures are isolated so one unavailable indexer does not invalidate the entire search.

## Explainable scoring

Each release stores explicit score components and rejection reasons. The manual-search UI exposes both rather than hiding acquisition decisions in an opaque quality profile.

Examples of evidence include exact identifiers, normalized title/author match, preferred format, series position, year, language, abridged/unabridged status, size sanity, swarm state, preferred markers and blocklist state.

Automatic acquisition is intentionally more conservative than manual acquisition. The scheduled wanted search only auto-grabs a non-rejected top result when its score reaches `acquisition.auto_grab_min_score` (default 80), readable and writable at `GET/PUT /api/v1/settings/acquisition` (admin).

## Download clients

Adapters are implemented for SABnzbd, qBittorrent, NZBGet and Transmission. Cancelling a download keeps the client's files by default for stuck or failed items (`remove_from_client=false`); an active job is always stopped. qBittorrent removal matches by torrent hash only, never by name. NZBGet completion and failure come from its `history`; qBittorrent URL adds store no id and the poll binds the torrent hash by name. Remote path mappings translate downloader-visible paths to bookdoe-visible paths when container mounts differ.

## Import

Managed import supports hardlink, copy and move. Copy/move paths use a temporary target and verification before final placement. Cross-filesystem hardlink requests safely fall back to copy. Existing adopted media does not go through this pipeline.

### Automatic wanted search: ordering, draining, cooldown

The `wanted-search` job (disabled by default; enable it in Settings > Jobs) searches wanted assets (monitored, acquisition intent, `missing`, work monitored, nothing in flight) in batches. Per run:

- Order: assets never searched automatically first, then least recently searched (`wanted_search_state.last_auto_search_at`). `media_assets` is never modified by a search.
- Drain: batches of `acquisition.wanted_search_batch` (default 20; the job's `config_json` `batch` overrides) follow each other until the run ends. `stopped_reason` is `drained`, `budget` (`acquisition.wanted_search_budget_minutes`, default 20, at most half the job lease, checked before every search), `daily_cap` (`acquisition.wanted_search_daily_cap`, default 300 automatic searches per rolling 24 h, counted from `release_searches`), `throttled` (an indexer answered 429 / rate limit) or `error_rate` (5 consecutive failed searches, or half of 10+). `acquisition.wanted_search_delay_seconds` (default 1) paces searches. The next 30-minute cycle continues.
- Cooldown: a search with no acceptable result (nothing found, or nothing at or above `acquisition.auto_grab_min_score`) is a miss; the asset waits `acquisition.wanted_search_cooldown_hours` (default 12) after the first miss, doubled per consecutive miss, at most 7 days. A grab clears it; so do changing the asset's monitoring or profile, editing the work, un-wanting the asset, and changing the grab score floor. A failed search (errors only) is not a miss.
- Manual searches are never delayed, blocked or counted as misses.
- `POST /api/v1/wanted/search-now` (admin) starts a run in the background under the same one-run-per-kind rule (409 while one is active). `GET /api/v1/wanted` returns `last_auto_search_at`, `auto_misses`, `next_eligible_at`, `cooling_down` per asset and accepts `not_found=1`.

## Conversion is not acquisition

Converting an owned book to the preferred format (M4B, EPUB) never touches acquisition state. The converted file is a derived asset (see DOMAIN_MODEL.md): it is `owned` but has no monitoring or acquisition intent, never appears in Wanted, is never searched for or upgraded, and does not make the original any less owned. Quality profiles (`preferred_formats`, `format_rank`) still rank only acquired releases. Importing a real file of the converted format later replaces the converted copy.
