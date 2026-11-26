# Readarr migration

The migration system is designed around the user's real-world case: one ebook Readarr instance and one audiobook Readarr instance containing overlapping bibliographic works.

## Guarantees

- Source Readarr SQLite databases are opened read-only.
- Existing media files are not moved, renamed or deleted.
- Root folders are imported in `adopt` mode.
- The migration can analyze/reconcile before execution.
- Ebook and audiobook records can converge on one bookdoe Work with separate MediaAssets.
- Unresolved records may be imported as local stubs for explicit review rather than discarded or silently guessed.

## Evidence

Reconciliation can use:

1. identifiers in Readarr records;
2. OpenReads resolver results;
3. title/author metadata;
4. existing filesystem paths;
5. embedded media metadata discovered by later scans.

## Recommended process

1. Copy neither database nor media manually; mount Readarr config directories read-only.
2. Analyze both instances.
3. Review the reconciliation report.
4. Execute database-state import.
5. Add/verify library roots in Adopt mode.
6. Scan physical files.
7. Resolve ambiguous/unmatched items from the Review queue.
8. Verify representative works that exist in both media forms.
9. Only then configure automated acquisition.

Keep both Readarr instances running until the unified catalogue and acquisition path have been validated.
