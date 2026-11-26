# ADR 004 — Provider-independent identity and field provenance

**Decision:** Internal UUIDs define canonical metadata entities. External provider identifiers are namespaced evidence. Provider values are stored as observations; canonical decisions and manual locks are separate records.

**Reason:** No upstream provider should be able to redefine the application's identity graph or overwrite user-corrected metadata merely because a refresh occurred.
