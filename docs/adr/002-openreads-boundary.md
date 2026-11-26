# ADR 002 — OpenReads is the metadata control plane

**Decision:** bookdoe consumes OpenReads through a versioned API. bookdoe does not directly embed individual bibliographic-provider clients.

**Reason:** Provider fragility must not become acquisition-system identity. OpenReads can evolve provider blending, provenance and AI reconciliation without forcing the library/download application to change.
