# Metadata and OpenReads v2

## Compatibility and canonical APIs

OpenReads preserves its original Readarr/rreading-glasses-compatible endpoints so existing Readarr clients can remain operational during migration. The new `/api/v2` interface is the canonical metadata contract for bookdoe and future native clients.

## Canonical entities

OpenReads v2 stores provider-independent UUID entities for contributors, works, editions and series. Legacy Open Library / Readarr-compatible identifiers are retained as mappings rather than primary identity.

## Provenance

Metadata is not flattened into a last-writer-wins record. OpenReads records:

- **Observation**: a provider supplied a value for a field at a point in time.
- **Decision**: the currently selected canonical value and why/source.
- **Lock**: a human-selected canonical value that provider refresh must not overwrite.

The Metadata Studio UI exposes competing observations and allows field-level selection/locking.

## Provider strategy

The existing stack uses Open Library as the primary work/edition graph, Google Books for supplementary edition data, and AudiobookDB for series enrichment. Existing throttling, caching, negative caching, matching and backoff remain in place.

Provider failures are isolated. A single provider must not make existing metadata or bookdoe's library unusable.

## Audience signals

For the kids shelf, OpenReads also records each provider's audience hints as observations: Google Books `categories` and `maturityRating` (MATURE / NOT_MATURE), language, publisher and page count, and Open Library `subjects`. bookdoe stores them per provider (`work_audience_signals`) and the kids gate reads only that stored data. Provider calls happen in the metadata refresh and the rate-limited `audience-backfill` job, never in the classifier.

## Local LLM

The optional LLM is deliberately subordinate to evidence. The model can compare observations and assist with ambiguity; it cannot originate bibliographic identifiers or silently invent facts. In metadata-selection mode the model is constrained to return an existing observation ID, which is validated before a suggestion is shown. Applying a suggestion remains explicit and auditable.

Evidence preference is approximately:

`exact identifier > verified provider mapping > normalized exact metadata > deterministic fuzzy match > LLM-assisted review > human review`
