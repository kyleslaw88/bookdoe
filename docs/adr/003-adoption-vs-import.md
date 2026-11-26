# ADR 003 — Existing adoption and managed import are separate operations

**Decision:** Scanning/adopting existing media never renames or moves files. Managed import is a distinct pipeline for newly acquired content.

**Reason:** Existing collections are valuable state. Treating discovery as an implicit normalization operation creates unnecessary destructive risk and makes migration harder to trust.
