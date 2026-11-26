# Architecture

## Design goals

bookdoe is designed as a self-hosted appliance rather than a distributed SaaS system. The architecture therefore optimizes for correctness, recoverability, comprehensibility, and low operational burden before horizontal scale.

```text
Browser
  |
  v
Nginx Proxy Manager
  |
  v
bookdoe ------------------------------------------------------+
  |                                                             |
  | native OIDC                                                 |
  +------------------> Authentik                                |
  |                                                             |
  | metadata                                                    |
  +------------------> OpenReads Metadata Studio                |
  |                       |                                      |
  |                       +--> Open Library                      |
  |                       +--> Google Books                      |
  |                       +--> AudiobookDB                       |
  |                       +--> optional local OpenAI-compatible LLM
  |                                                             |
  | search                                                      |
  +------------------> Prowlarr / Newznab / Torznab             |
  |                                                             |
  | acquisition                                                 |
  +------------------> SABnzbd / qBittorrent / NZBGet / Transmission
  |                                                             |
  +------------------> ebook and audiobook library mounts <-----+
```

## Service boundary

The separation between bookdoe and OpenReads is intentional.

### bookdoe owns

- desired/owned media state;
- monitoring policy;
- library roots and physical files;
- release searches and scores;
- indexer/download-client integrations;
- download/import lifecycle;
- existing-library adoption;
- migration;
- OPDS publication;
- users/authentication;
- jobs/history/review queues.

### OpenReads owns

- contributor/work/edition/series identity;
- provider identifiers;
- provider observations;
- canonical metadata decisions;
- metadata field locks;
- provider cache/rate limits;
- metadata reconciliation;
- optional LLM review/audit.

bookdoe persists sufficient metadata snapshots to remain useful when OpenReads is temporarily unavailable. Metadata outages must not prevent library browsing, download tracking, import, or history access.

## Application architecture

Both services are modular monoliths. That choice is deliberate: there is no message broker, Redis cluster, or distributed transaction system to operate. External systems are isolated behind adapters and internal modules remain independently testable.

bookdoe uses FastAPI, Python and SQLite/WAL. Its frontend is two dependency-free browser applications shipped with the backend (the Player at `/app`, Management at `/manage`; the reader and audio player are one shared module), avoiding a Node runtime or separate frontend container in production. Devices use the versioned Player API (`/api/player/v1`) with their own revocable tokens; see `PLAYER_API.md`.

OpenReads remains Python/FastAPI because its tested provider/cache/matching code is valuable and rewriting it solely for language uniformity would add risk without product value.

## Persistence

SQLite is configured with WAL, foreign keys and busy timeouts. bookdoe provides an online backup job using SQLite's backup API. Backups undergo `PRAGMA integrity_check`, receive a SHA-256 sidecar manifest, and rotate according to configured retention.

## Deployment

The supplied Docker topology uses one private `books` network. OpenReads is not published to the host by default. bookdoe can be published to the LAN or attached to an Nginx Proxy Manager network for HTTPS/OIDC access.
