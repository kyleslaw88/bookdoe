"""Catalog duplicate detectors: duplicate Works, split duplicate Works, duplicate Editions (P5-21a, Part XXV).

Read-only: they only SELECT, so adopted and observe roots are covered like managed ones.
Candidates come from a normalized-title index (a dict of title key -> works), never from
comparing every Work with every other, and pair generation stops at the run's budget
(``MAX_PAIRS``), so a library full of one repeated title cannot blow up the run.

Part XIX: identifiers outrank fuzzy text. A pair that shares an identifier value (or where
both sides are actively used) is ``high`` confidence; a pair matched on title and author
alone is ``probable``. "Actively used" means each side has at least one owned or monitored
asset -- catalog-only rows (status missing, unmonitored) do not count toward the high
confidence threshold. Part XXVI evidence-before-merge: each issue stores the signals it
was found on, not only the verdict.

Fingerprints (see maintenance.py): the subject is the pair, ids sorted so the pair has one
identity whichever side was found first.
"""
from __future__ import annotations

import itertools
import re
import unicodedata
from typing import Any, Iterable, Iterator

from .. import db
from .maintenance import DetectorContext, Finding, register_detector

MAX_PAIRS = 500
_TRAILING_ARTICLE = re.compile(r",\s*(?:the|a|an)\s*$")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")
_APOSTROPHES = re.compile(r"[’'`]")
_NON_WORD = re.compile(r"[\W_]+")


def normalize_title(title: str | None) -> str:
    """Lowercase, subtitle after ':' dropped, article and punctuation stripped.

    'The Hobbit', 'Hobbit, The' and 'The Hobbit: There and Back Again' all give 'hobbit'.
    """
    s = unicodedata.normalize("NFKC", str(title or "")).casefold()
    s = s.split(":", 1)[0].strip()
    s = _TRAILING_ARTICLE.sub("", s)
    s = _LEADING_ARTICLE.sub("", s)
    s = _APOSTROPHES.sub("", s)
    return _NON_WORD.sub(" ", s).strip()


# Series decorations a catalog or a file tag bolts onto a title: '(The 39 Clues, #5)', '(Book 5 of X)',
# ': Book 5', ', #5', ' - Vol. 2'. They name the book's place in a series, not a different book.
_NUM = r"(\d+(?:\.\d+)?)"
_MARKER = r"(?:#|\b(?:book|bk|vol|volume|no|part|number)\b\.?)"
_SUFFIX_PAREN = re.compile(r"\s*[(\[][^()\[\]]*?" + _MARKER + r"\s*" + _NUM + r"[^()\[\]]*[)\]]\s*$", re.I)
_SUFFIX_TAIL = re.compile(r"\s*(?:[:,;\u2013\u2014-]\s*)?" + _MARKER + r"\s*" + _NUM + r"\s*$", re.I)


def strip_series_suffix(title: str | None, series_name: str | None = None) -> tuple[str, str | None]:
    """(title without series decoration, the position the decoration named or None).

    'The Black Circle (The 39 Clues, #5)' -> ('The Black Circle', '5'); 'Dune: Book 1' -> ('Dune', '1').
    With ``series_name`` a '(The 39 Clues)' bracket and a 'The 39 Clues: ' / 'The 39 Clues #5: ' prefix
    go too. A title that would be left empty is returned unchanged."""
    original = unicodedata.normalize("NFKC", str(title or "")).strip()
    s, pos = original, None
    for _ in range(2):
        for rx in (_SUFFIX_PAREN, _SUFFIX_TAIL):
            m = rx.search(s)
            if m and s[:m.start()].strip():
                pos = pos or m.group(1)
                s = s[:m.start()].strip()
                break
    if series_name:
        name = re.escape(unicodedata.normalize("NFKC", series_name).strip())
        name = re.sub(r"\\ ", r"\\s+", name)
        m = re.search(r"\s*\(\s*(?:the\s+)?" + name + r"(?:\s+series)?\s*\)\s*$", s, re.I)
        if m and s[:m.start()].strip():
            s = s[:m.start()].strip()
        m = re.match(r"^(?:the\s+)?" + name + r"\s*(?:" + _MARKER + r"\s*(?P<n>\d+(?:\.\d+)?))?\s*[:\u2013\u2014-]\s*(?=\S)", s, re.I)
        if m and s[m.end():].strip():
            pos = pos or m.group("n")
            s = s[m.end():].strip()
    return (s or original), pos


def title_key(title: str | None, series_name: str | None = None) -> str:
    """normalize_title of the title with its series decoration removed: the identity of the book itself."""
    stem, _ = strip_series_suffix(title, series_name)
    return normalize_title(stem) or normalize_title(title)


def full_title_key(title: str | None, series_name: str | None = None) -> str:
    """Like title_key but a subtitle is kept: 'Dune: Messiah' and 'Dune: Children of Dune' stay different books."""
    stem, _ = strip_series_suffix(title, series_name)
    t = unicodedata.normalize("NFKC", stem or str(title or "")).casefold().strip()
    t = _TRAILING_ARTICLE.sub("", t)
    t = _LEADING_ARTICLE.sub("", t)
    return _NON_WORD.sub(" ", _APOSTROPHES.sub("", t)).strip()


def pair_id(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _pair_subject(a: str, b: str) -> str:
    return ":".join(pair_id(a, b))


# ---------------------------------------------------------------------------
# Works
# ---------------------------------------------------------------------------

def _load_works() -> dict[str, dict[str, Any]]:
    works: dict[str, dict[str, Any]] = {}
    for r in db.all("SELECT id,title,metadata_id,added_at FROM works"):
        works[r["id"]] = {"id": r["id"], "title": r["title"], "provider_linked": bool(r["metadata_id"]), "added_at": r["added_at"],
                          "authors": {}, "positions": {}, "suffix_position": strip_series_suffix(r["title"])[1], "owned": 0, "assets": 0, "monitored_assets": 0, "kinds": set(), "identifiers": set()}
    for r in db.all(
        "SELECT wc.work_id,c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id "
        "WHERE wc.role='author' ORDER BY wc.work_id,wc.ord"
    ):
        w = works.get(r["work_id"])
        if w is not None:
            w["authors"].setdefault(db.contributor_key(r["name"]), r["name"])
    for r in db.all("SELECT work_id,series_id,position FROM series_memberships WHERE position IS NOT NULL AND TRIM(position)<>''"):
        w = works.get(r["work_id"])
        if w is not None:
            w["positions"][r["series_id"]] = str(r["position"]).strip()
    for r in db.all("SELECT work_id,kind,status,monitored FROM media_assets WHERE derived_from_asset_id IS NULL"):
        w = works.get(r["work_id"])
        if w is None:
            continue
        w["assets"] += 1
        w["monitored_assets"] += int(bool(r["monitored"]))
        if r["status"] == "owned":
            w["owned"] += 1
            w["kinds"].add(r["kind"])
    # Work-level and edition-level identifiers both describe the book: the identifiers
    # key is per entity_type, so the same ISBN can sit on a work and on another's edition.
    for r in db.all(
        "SELECT i.namespace,i.value,CASE i.entity_type WHEN 'work' THEN i.entity_id ELSE e.work_id END work_id "
        "FROM identifiers i LEFT JOIN editions e ON i.entity_type='edition' AND e.id=i.entity_id "
        "WHERE i.entity_type IN ('work','edition')"
    ):
        w = works.get(r["work_id"])
        if w is not None:
            w["identifiers"].add((r["namespace"], str(r["value"]).casefold()))
    return works


def _canonical(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The Work that looks like the one to keep, and why: owned, then provider-linked, then identifiers."""
    def score(w):
        return (w["owned"], w["provider_linked"], len(w["identifiers"]))
    if score(a) == score(b):
        first = min((a, b), key=lambda w: (w["added_at"], w["id"]))
        return first, ["tie on owned/provider/identifiers; older Work preferred"]
    win, lose = (a, b) if score(a) > score(b) else (b, a)
    why = []
    if win["owned"] > lose["owned"]:
        why.append("owned")
    if win["provider_linked"] and not lose["provider_linked"]:
        why.append("provider-linked")
    if len(win["identifiers"]) > len(lose["identifiers"]):
        why.append("more identifiers")
    return win, why


def _side(w: dict[str, Any]) -> dict[str, Any]:
    return {"work_id": w["id"], "title": w["title"], "authors": list(w["authors"].values()), "assets": w["assets"],
            "owned_assets": w["owned"], "owned_kinds": sorted(w["kinds"]), "monitored_assets": w["monitored_assets"],
            "identifiers": len(w["identifiers"]), "provider_linked": w["provider_linked"]}


def different_positions(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Both Works say where they sit in a series (or in their title decoration) and the places differ:
    two volumes of one series, not one book twice."""
    for sid, pos in a["positions"].items():
        if sid in b["positions"] and _num_or_text(pos) != _num_or_text(b["positions"][sid]):
            return True
    pa, pb = a.get("suffix_position"), b.get("suffix_position")
    return bool(pa and pb and _num_or_text(pa) != _num_or_text(pb))


def _num_or_text(v: str) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v).casefold()


def _work_pairs(works: dict[str, dict[str, Any]], ctx: DetectorContext) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Candidate pairs from the title index, in a stable order, at most ctx.limit + 1 of them.

    Within a title group works are indexed by author key, so two works are only ever
    compared when they share an author: a title held by thousands of distinct authors
    costs O(n) to index and yields nothing. The budget is one counter across every title
    group, and a pair sharing several authors is yielded once.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for w in works.values():
        key = title_key(w["title"])
        if key and w["authors"]:
            index.setdefault(key, []).append(w)
    budget = ctx.limit + 1  # one past the cap tells _collect the run was truncated
    seen: set[tuple[str, str]] = set()
    for key in sorted(index):
        group = sorted(index[key], key=lambda w: w["id"])
        by_author: dict[str, list[dict[str, Any]]] = {}
        for w in group:  # group is id-sorted, so each author's list is too
            for ak in w["authors"]:
                by_author.setdefault(ak, []).append(w)
        for ak in sorted(by_author):
            for a, b in itertools.combinations(by_author[ak], 2):
                pair = pair_id(a["id"], b["id"])
                if pair in seen:
                    continue
                seen.add(pair)
                if different_positions(a, b):
                    continue
                yield a, b
                budget -= 1
                if budget <= 0:
                    return


def _duplicate_works(ctx: DetectorContext) -> Iterable[Finding]:
    works = _load_works()
    for a, b in _work_pairs(works, ctx):
        same_authors = a["authors"].keys() == b["authors"].keys()
        shared_ids = sorted(a["identifiers"] & b["identifiers"])
        a_active = bool(a["owned"] or a["monitored_assets"])
        b_active = bool(b["owned"] or b["monitored_assets"])
        signals = ["title_equal_normalized"]
        if normalize_title(a["title"]) != normalize_title(b["title"]):
            signals.append("series_suffix_variant")
        signals.append("authors_equal" if same_authors else "authors_overlap")
        if shared_ids:
            signals.append("shared_identifier")
        if a_active and b_active:
            signals.append("both_monitored_or_owned")
        high = bool(shared_ids) or (a_active and b_active)

        owned_vs_catalog = (a["owned"] > 0) != (b["owned"] > 0)
        different_kinds = bool(a["kinds"]) and bool(b["kinds"]) and a["kinds"] != b["kinds"]
        split = same_authors and (owned_vs_catalog or different_kinds)
        if split:
            signals.append("owned_vs_catalog_only" if owned_vs_catalog else "different_kinds")
        canon, why = _canonical(a, b)
        detail = {
            "works": [_side(a), _side(b)],
            "signals": signals,
            "shared_identifiers": [{"namespace": ns, "value": v} for ns, v in shared_ids],
            "normalized_title": title_key(a["title"]),
            "confidence": "high" if high else "probable",
            "canonical_work_id": canon["id"],
            "canonical_reason": why,
        }
        yield Finding("split_duplicate_work" if split else "duplicate_work", "warning" if high else "info", "work_pair",
                      _pair_subject(a["id"], b["id"]), detail=detail, repair_action="merge_works")


register_detector("duplicate-works", ("duplicate_work", "split_duplicate_work"), _duplicate_works, limit=MAX_PAIRS)


# ---------------------------------------------------------------------------
# Editions
# ---------------------------------------------------------------------------

def _clean(v: Any) -> str:
    return " ".join(str(v or "").split()).casefold()


def _duplicate_editions(ctx: DetectorContext) -> Iterable[Finding]:
    editions = {r["id"]: r for r in db.all(
        "SELECT e.id,e.work_id,e.title,e.publisher,e.release_date,e.language,w.title work_title FROM editions e JOIN works w ON w.id=e.work_id")}
    assets: dict[str, list[int]] = {}
    for r in db.all("SELECT edition_id,status FROM media_assets WHERE edition_id IS NOT NULL AND derived_from_asset_id IS NULL"):
        counts = assets.setdefault(r["edition_id"], [0, 0])
        counts[0] += 1
        counts[1] += int(r["status"] == "owned")

    # pair -> signals. Only editions of one Work are indexed together, so no all-pairs scan.
    # Identifier overlap between two editions cannot occur: identifiers' primary key is
    # (entity_type, namespace, value), so one edition value belongs to one edition row, and
    # editions.metadata_id is unique-indexed. Field equality is therefore the only signal.
    pairs: dict[tuple[str, str], list[str]] = {}
    by_fields: dict[tuple, list[str]] = {}
    for eid, e in editions.items():
        # An edition with no publisher or no release date has nothing to compare: skip it,
        # or every sparse edition of a Work would match every other.
        if _clean(e["publisher"]) and _clean(e["release_date"]):
            by_fields.setdefault((e["work_id"], normalize_title(e["title"]), _clean(e["publisher"]), _clean(e["language"]),
                                  _clean(e["release_date"])), []).append(eid)

    budget = ctx.limit + 1  # one past the cap tells _collect the run was truncated
    for key in sorted(by_fields, key=repr):
        for a, b in itertools.islice(itertools.combinations(sorted(by_fields[key]), 2), budget):
            pairs[(a, b)] = ["title_publisher_language_date_equal"]
        if len(pairs) >= budget:
            break

    for (a, b), sigs in sorted(pairs.items()):
        ea, eb = editions[a], editions[b]

        def side(eid, e):
            n, owned = assets.get(eid, (0, 0))
            return {"edition_id": eid, "title": e["title"], "publisher": e["publisher"], "release_date": e["release_date"],
                    "language": e["language"], "assets": n, "owned_assets": owned}
        detail = {"work_id": ea["work_id"], "work_title": ea["work_title"], "editions": [side(a, ea), side(b, eb)], "signals": sigs,
                  "confidence": "probable"}
        yield Finding("duplicate_edition", "info", "edition_pair",
                      _pair_subject(a, b), detail=detail)


register_detector("duplicate-editions", ("duplicate_edition",), _duplicate_editions, limit=MAX_PAIRS)
