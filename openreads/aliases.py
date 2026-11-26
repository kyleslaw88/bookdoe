"""Duplicate-author aliases -- a workaround for Open Library data quality.

Open Library sometimes has several author records for one real person, and
nothing on the records marks them as duplicates (no redirect/merge link:
both keep resolving as independent, live authors). Readarr then sees two
authors and splits the person's books between them on every refresh.

AUTHOR_ALIASES maps a duplicate OL author key to the canonical key that
should represent the person. openreads treats every alias key as the
canonical author everywhere: lookups, search results, work->author links,
and the author's work list (the union of all records' works).

To add a case: pick the record with the most complete data as canonical and
add `"<duplicate key>": "<canonical key>"` below (name it in a comment),
then redeploy. On startup, db.init_db() re-points any rows already
stored under the duplicate key. Chains are followed (A->B, B->C), so no
need to update old entries. Extra entries can also be supplied without a
code change via env OPENREADS_AUTHOR_ALIASES='{"OLdupA": "OLcanonA"}'.
"""
import json
import os

AUTHOR_ALIASES = {
    # Richard Osman (Thursday Murder Club). OL13823712A has no bio and 14
    # works; OL7973935A has the bio, external ids and the rest of the works.
    "OL13823712A": "OL7973935A",
    # Wilkie Collins (two series). OL22463A is the full record
    # (bio, most works); the other two are stubs with a handful of works.
    "OL15002457A": "OL22463A",
    "OL7922172A": "OL22463A",  # name spelled "WILKIE COLLINS"
}


def _all():
    merged = dict(AUTHOR_ALIASES)
    extra = os.environ.get("OPENREADS_AUTHOR_ALIASES")
    if extra:
        merged.update(json.loads(extra))
    return merged


def canonical_key(ol_key: str) -> str:
    """Follow alias chains to the canonical key (cycle-safe)."""
    table = _all()
    seen = set()
    while ol_key in table and ol_key not in seen:
        seen.add(ol_key)
        ol_key = table[ol_key]
    return ol_key


def group_keys(ol_key: str) -> list:
    """The canonical key first, then every alias that resolves to it."""
    canon = canonical_key(ol_key)
    return [canon] + sorted(k for k in _all() if k != canon and canonical_key(k) == canon)
