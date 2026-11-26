"""Evidence and the deterministic gate for the kids shelf (the owner, 2026-10-12).

The LLM never decides alone. `gather_evidence` collects the text metadata already on
record (never file contents or cover images), the classifier sends it as structured
fields, and `evaluate_gate` - plain code - decides whether a "kids" verdict may be
applied without a person. The signal tables below are the ONE definition of what counts
as audience evidence and as an adult red flag; tests/test_kids_gate.py pins them.

What bookdoe actually stores (docs/METADATA.md, db.py): title, subtitle, description,
first publish date and language on the work; publisher, release date, language, pages,
duration, abridged on editions; narrators on media assets; authors and narrators as
contributors; series and position; and free-form per-field `metadata_observations`.
Audience fields come from two places, both read from the database only (the classifier
never calls a provider): `work_audience_signals`, filled by the metadata refresh and the
audience backfill from Google Books (categories, maturityRating) and Open Library
(subjects) with the provider kept per value (services/audience.py), and free-form
`metadata_observations` (field names in OBSERVED_FIELDS). On top of that the publisher is
matched against CHILDRENS_PUBLISHERS and the description is searched for explicit audience
statements ("ages 8-12", "a middle-grade novel"). Provider disagreement: any hard adult
signal wins, and strong signals count only when no provider says the book is mature.
Nothing is invented.
"""
from __future__ import annotations

import re
from typing import Any

from .. import db

# A kids verdict is only auto-applied with a blurb to justify it. 80 characters is a
# sentence or two: shorter than that, "juvenile-sounding" is a guess about a title.
MIN_DESCRIPTION_CHARS = 80
# Priors: other books by the same author (or in the same series) that a person or the
# classifier judged not_kids, outnumbering the human-confirmed kids ones. Two are needed
# before it is a HARD signal: one model verdict (which may itself be wrong, and depends on
# scan order) must not condemn an author's whole catalogue unseen. A single not_kids is
# still sent to the model in the prompt.
PRIOR_MIN_NOT_KIDS = 2
MAX_DESCRIPTION_CHARS = 2000
# A child's reading ceiling for the "stated age range" signal.
STRONG_AGE_UPPER_BOUND = 12

# metadata_observations.field_name values read as structured evidence (any entity, any
# source). Several spellings because providers disagree; none is written by bookdoe
# today (see the module docstring).
OBSERVED_FIELDS: dict[str, tuple[str, ...]] = {
    "subjects": ("subjects", "subject"),
    "categories": ("categories", "category"),
    "genres": ("genres", "genre"),
    "audience": ("audience", "target_audience", "reading_level"),
    "age_range": ("age_range", "age_ranges", "age"),
    "maturity": ("maturity", "maturity_rating", "content_rating"),
    "explicit": ("explicit", "is_explicit"),
    "imprint": ("imprint",),
}

# work_audience_signals.kind -> the evidence key it feeds. Kinds not listed (publisher,
# language, page_count, series, subject_place) are stored for provenance and shown in the UI
# but are not audience evidence on their own.
SIGNAL_KIND_TO_EVIDENCE: dict[str, str] = {
    "category": "categories", "subject": "subjects", "maturity": "maturity",
    "age_range": "age_range", "audience": "audience",
}

_ci = re.IGNORECASE

# ---------------------------------------------------------------- strong audience signals
# id -> (label, where it is looked for)
STRONG_SIGNALS: dict[str, tuple[str, str]] = {
    "juvenile_category": ("Children's / juvenile / middle-grade category, subject or audience", "categories, subjects, genres, audience"),
    "children_publisher": ("Publisher or imprint known for children's books", "edition publisher, imprint"),
    "age_range_upper_bound": (f"Stated age range with an upper bound of {STRONG_AGE_UPPER_BOUND} or younger", "age_range field, or 'ages 8-12' / 'grades 3-6' in the description"),
    "description_audience_statement": ("Description says outright it is a children's / middle-grade book", "description"),
}

# Structured fields only: "young adult" is deliberately absent (YA is not a kids book).
JUVENILE_CATEGORY_RE = re.compile(
    r"\b(juvenile\s+(fiction|nonfiction|non-fiction|literature)|children'?s|childrens|kids'?|middle[\s-]?grade|"
    r"early\s+readers?|beginning\s+readers?|picture\s+books?|chapter\s+books?|young\s+readers?|board\s+books?|"
    r"baby\s*/?\s*toddler|ages?\s+\d)", _ci)

# Substring match on the normalised publisher/imprint. Judgement list, extend by editing
# here; every entry is a name whose catalogue is overwhelmingly for children.
CHILDRENS_PUBLISHERS: tuple[str, ...] = (
    "scholastic", "puffin", "ladybird", "usborne", "candlewick", "nosy crow", "kingfisher", "golden books",
    "random house books for young readers", "random house children", "penguin young readers",
    "knopf books for young readers", "dial books for young readers", "viking children",
    "putnam books for young readers", "philomel", "harpercollins children", "greenwillow", "katherine tegen",
    "little, brown books for young readers", "little brown books for young readers", "simon & schuster books for young readers",
    "aladdin", "margaret k. mcelderry", "atheneum books for young readers", "disney press", "disney hyperion",
    "disney book group", "dk children", "abrams books for young readers", "macmillan children", "feiwel and friends",
    "feiwel & friends", "henry holt books for young readers", "bloomsbury children", "walker books", "orchard books",
    "hachette children", "capstone", "kane miller", "tundra books", "kids can press", "step into reading",
    "chronicle kids", "boyds mills", "lerner publishing", "egmont", "andersen press", "barefoot books",
    "books for young readers", "for young readers", "children's books", "childrens books", "juvenile",
)

_AGE_RANGE_TEXT_RE = re.compile(r"\b(?:ages?|aged)\s*(\d{1,2})\s*(?:-|–|—|to|through)\s*(\d{1,2})\b", _ci)
_GRADE_RANGE_TEXT_RE = re.compile(r"\bgrades?\s*(\d{1,2})\s*(?:-|–|—|to|through)\s*(\d{1,2})\b", _ci)
_DESC_AUDIENCE_RE = re.compile(
    r"\b(middle[\s-]?grade\s+(novel|book|story|series|adventure|fantasy|mystery|fiction|reader|readers)|"
    r"children'?s\s+(book|story|novel|series|classic|fantasy|adventure)|picture\s+book|early\s+reader|"
    r"chapter\s+book|for\s+young\s+readers|for\s+(?:kids|children)\b|a\s+kids'?\s+(book|story))", _ci)
_NEGATION_RE = re.compile(r"\b(not|isn'?t|aren'?t|never|no|unlike|rather than|more than just)\s+(?:\w+\s+){0,3}$", _ci)

# ---------------------------------------------------------------- hard adult signals
HARD_ADULT_SIGNALS: dict[str, str] = {
    "maturity_flag": "Mature / explicit content flag",
    "adult_audience_category": "Adult audience category or subject",
    "adult_genre": "Adult-leaning genre (erotica, romance, horror, thriller, LitRPG ...)",
    "adult_text_marker": "Description or title states adult content or an adult-only genre",
    "author_prior_not_kids": "Other books by the same author were judged not for kids",
    "series_prior_not_kids": "Other books in the same series were judged not for kids",
}
# Genre words in category/subject/genre/series fields. "suspense" and "space opera" are
# left out on purpose: kids' mysteries and Star Wars live there; those cases are held
# back by the strong-signal requirement and the author/series priors instead.
ADULT_GENRE_RE = re.compile(
    r"\b(erotica|erotic|romance|romantic\s+suspense|horror|thrillers?|litrpg|lit\s?rpg|gamelit|game\s?lit|"
    r"progression\s+fantasy|harem|true\s+crime)\b", _ci)
ADULT_AUDIENCE_RE = re.compile(r"(?<!young )(?<!young-)\badults?\b|\bmature\b|\b18\s?\+", _ci)
ADULT_TEXT_RE = re.compile(
    r"\b(litrpg|lit\s?rpg|gamelit|game\s?lit|erotica|steamy|sex\s+scenes?|sexual\s+content|graphic\s+violence|"
    r"mature\s+(readers?|audiences?|content|themes?)|adult\s+(readers?|audiences?|content|fiction|fantasy|science\s+fiction|sci-?fi|romance|themes?)|"
    r"18\s?\+|explicit\s+(content|language|scenes?))", _ci)

# "Young Adult" is neither a strong nor a hard signal: it only says "not clearly for kids".
YOUNG_ADULT_RE = re.compile(r"\b(young[\s-]?adult|teen(?:s|age|agers?)?|ya)\b", _ci)

# Human-readable gate blockers.
BLOCKERS: dict[str, str] = {
    "llm_not_kids": "The model did not say kids",
    "below_threshold": "Model confidence is below the auto-approve threshold",
    "llm_reported_adult_signals": "The model itself reported adult signals",
    "hard_adult_signals": "Hard adult signal(s) in the metadata",
    "no_strong_audience_signal": "No strong audience signal in the metadata (children's category, children's publisher, age range up to 12, or a stated children's book)",
    "description_too_short": f"No usable description (under {MIN_DESCRIPTION_CHARS} characters)",
}


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _as_list(value: Any) -> list[str]:
    """An observation value as a list of strings: JSON list, or ';' / '|' / newline separated text."""
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    if text[:1] in "[{":
        parsed = db.json_load(text, None)
        if isinstance(parsed, list):
            return [_norm(x) for x in parsed if _norm(x)]
        if isinstance(parsed, dict):
            return [f"{k}: {v}" for k, v in parsed.items() if _norm(v)]
    return [p for p in (_norm(x) for x in re.split(r"[;|\n]", text)) if p]


def normalize_term(kind: str, value: Any) -> str:
    """The class of one stored provider value, from the signal tables above (the ONE place):
    'mature' (provider maturity flag), 'adult_audience', 'adult_genre', 'juvenile',
    'young_adult' or 'neutral'. A hard adult reading wins over a juvenile one, so
    "Juvenile Fiction / Horror" is adult_genre. 'Young Adult' is deliberately its own class."""
    text = _norm(value)
    if not text:
        return "neutral"
    if kind == "maturity":
        return "mature" if _is_mature_flag(text) else "neutral"
    if kind == "age_range":
        rng = parse_age_range(text)
        return "juvenile" if rng and rng[1] is not None and rng[1] <= STRONG_AGE_UPPER_BOUND else "neutral"
    if kind not in SIGNAL_KIND_TO_EVIDENCE:
        return "neutral"
    if ADULT_AUDIENCE_RE.search(text):
        return "adult_audience"
    if ADULT_GENRE_RE.search(text):
        return "adult_genre"
    if JUVENILE_CATEGORY_RE.search(text):
        return "juvenile"
    if YOUNG_ADULT_RE.search(text):
        return "young_adult"
    return "neutral"


def parse_age_range(value: Any) -> tuple[int, int | None] | None:
    """(min, max-or-None) from '8-12', 'Ages 8 to 12', '12+', {'min':8,'max':12}; None if not an age range."""
    if isinstance(value, dict):
        lo, hi = value.get("min"), value.get("max")
        try:
            return (int(lo), int(hi) if hi is not None else None) if lo is not None else None
        except (TypeError, ValueError):
            return None
    text = _norm(value)
    m = re.search(r"(\d{1,2})\s*(?:-|–|—|to|through)\s*(\d{1,2})", text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = re.search(r"(\d{1,2})\s*(\+|and up|and over|plus)", text, _ci)
    if m:
        return (int(m.group(1)), None)
    m = re.fullmatch(r"(?:ages?\s*)?(\d{1,2})", text, _ci)
    return (int(m.group(1)), int(m.group(1))) if m else None


def _unnegated(regex: re.Pattern[str], text: str) -> str | None:
    """First match of `regex` in `text` that is not directly negated ('is not a children's book')."""
    for m in regex.finditer(text):
        if not _NEGATION_RE.search(text[max(0, m.start() - 40):m.start()]):
            return m.group(0)
    return None


def _description_age_upper(text: str) -> tuple[int, int, str] | None:
    for m in _AGE_RANGE_TEXT_RE.finditer(text):
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return lo, hi, m.group(0)
    for m in _GRADE_RANGE_TEXT_RE.finditer(text):
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return lo + 5, hi + 6, m.group(0)  # a grade-g class is about g+5 at the start, g+6 at the end
    return None


# ---------------------------------------------------------------- priors
def _prior(rows: list[dict[str, Any]]) -> dict[str, int]:
    """not_kids / kids counts over OTHER works. 'kids' counts only what a person confirmed:
    an auto-approval is the very thing being second-guessed, so it never vouches for a sibling."""
    not_kids = kids = 0
    for r in rows:
        if r["state"] == "off":
            not_kids += 1
        elif r["state"] == "on" and r["via"] != "auto_approved":
            kids += 1
        elif r["state"] is None and r["verdict"] == "not_kids":
            not_kids += 1
    return {"not_kids": not_kids, "kids": kids, "others": len(rows)}


_PRIOR_SELECT = (
    "SELECT w.id,kc.verdict,d.state,d.via FROM works w "
    "LEFT JOIN kids_classification kc ON kc.work_id=w.id LEFT JOIN kids_decisions d ON d.work_id=w.id "
)


def author_priors(work_id: str) -> list[dict[str, Any]]:
    out = []
    for a in db.all("SELECT c.id,c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id "
                    "WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (work_id,)):
        rows = db.all(_PRIOR_SELECT + "JOIN work_contributors x ON x.work_id=w.id WHERE x.contributor_id=? AND x.role='author' AND w.id!=?",
                      (a["id"], work_id))
        out.append({"name": a["name"], **_prior(rows)})
    return out


def series_priors(work_id: str) -> list[dict[str, Any]]:
    out = []
    for s in db.all("SELECT s.id,s.name,m.position FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (work_id,)):
        rows = db.all(_PRIOR_SELECT + "JOIN series_memberships x ON x.work_id=w.id WHERE x.series_id=? AND w.id!=?", (s["id"], work_id))
        out.append({"name": s["name"], "position": s["position"], **_prior(rows)})
    return out


def prior_is_adverse(prior: dict[str, Any]) -> bool:
    return prior["not_kids"] >= PRIOR_MIN_NOT_KIDS and prior["not_kids"] > prior["kids"]


# ---------------------------------------------------------------- evidence
def _observed(work_id: str, edition_ids: list[str]) -> dict[str, list[str]]:
    """Structured fields recorded as observations on the work or its editions, de-duplicated."""
    out: dict[str, list[str]] = {}
    ids = [work_id, *edition_ids]
    marks = ",".join("?" * len(ids))
    wanted = {name: key for key, names in OBSERVED_FIELDS.items() for name in names}
    rows = db.all(f"SELECT field_name,value FROM metadata_observations WHERE entity_id IN ({marks}) AND field_name IN ({','.join('?' * len(wanted))}) ORDER BY observed_at",  # nosec B608
                  (*ids, *wanted))
    for r in rows:
        bucket = out.setdefault(wanted[r["field_name"]], [])
        for item in _as_list(r["value"]):
            if item not in bucket:
                bucket.append(item)
    return out


def stored_signals(work_id: str) -> list[dict[str, Any]]:
    """Provider audience values on record for a work (normalized table; never a network call)."""
    return db.all("SELECT provider,kind,value,norm,fetched_at FROM work_audience_signals WHERE work_id=? ORDER BY provider,kind,value", (work_id,))


def gather_evidence(work_id: str) -> dict[str, Any]:
    """Everything the classifier and the gate may look at, from records only. Keys with no
    value are omitted so a prompt never says 'unknown' for what is merely absent."""
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not work:
        raise ValueError("Work not found")
    editions = db.all("SELECT * FROM editions WHERE work_id=? ORDER BY created_at", (work_id,))
    assets = db.all("SELECT narrator_json,duration_seconds,abridged FROM media_assets WHERE work_id=?", (work_id,))
    ev: dict[str, Any] = {"title": work["title"], "authors": [], "description": _norm(work.get("description"))[:MAX_DESCRIPTION_CHARS]}
    ev["authors"] = [a["name"] for a in db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (work_id,))]
    if work.get("subtitle"):
        ev["subtitle"] = _norm(work["subtitle"])
    if work.get("primary_language") or any(e.get("language") for e in editions):
        ev["language"] = work.get("primary_language") or next(e["language"] for e in editions if e.get("language"))
    release = work.get("first_publish_date") or next((e["release_date"] for e in editions if e.get("release_date")), None)
    if release:
        ev["release_date"] = str(release)
    publishers = list(dict.fromkeys(_norm(e["publisher"]) for e in editions if _norm(e.get("publisher"))))
    if publishers:
        ev["publishers"] = publishers
    pages = [e["pages"] for e in editions if e.get("pages")]
    if pages:
        ev["pages"] = max(pages)
    seconds = [x for x in [*(e.get("duration_seconds") for e in editions), *(a.get("duration_seconds") for a in assets)] if x]
    if seconds:
        ev["duration_minutes"] = round(max(seconds) / 60)
    narrators = list(dict.fromkeys(n for a in assets for n in (db.json_load(a["narrator_json"], []) or []) if n))
    if narrators:
        ev["narrators"] = narrators
    series = [{"name": s["name"], "position": s["position"]} for s in db.all(
        "SELECT s.name,m.position FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (work_id,))]
    if series:
        ev["series"] = series
    observed: dict[str, list[str]] = {}
    sources = []
    for sig in stored_signals(work_id):
        key = SIGNAL_KIND_TO_EVIDENCE.get(sig["kind"])
        if key:
            bucket = observed.setdefault(key, [])
            if sig["value"] not in bucket:
                bucket.append(sig["value"])
            sources.append({"provider": sig["provider"], "kind": sig["kind"], "value": sig["value"], "norm": sig["norm"]})
    for key, items in _observed(work_id, [e["id"] for e in editions]).items():
        bucket = observed.setdefault(key, [])
        bucket.extend(i for i in items if i not in bucket)
    if sources:
        ev["audience_sources"] = sources
    for key in ("subjects", "categories", "genres", "audience", "maturity", "imprint"):
        if observed.get(key):
            ev[key] = observed[key]
    if observed.get("age_range"):
        ev["age_range"] = observed["age_range"]
    if observed.get("explicit"):
        ev["explicit"] = observed["explicit"]
    ev["author_prior"] = author_priors(work_id)
    ev["series_prior"] = series_priors(work_id)
    return ev


def evidence_hash(ev: dict[str, Any]) -> str:
    """Stable fingerprint of the evidence the model saw (priors excluded: they move with
    other books, not with this one)."""
    import hashlib
    body = {k: v for k, v in ev.items() if k not in ("author_prior", "series_prior")}
    return hashlib.sha256(db.json_dump(body).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- signals
def _is_mature_flag(value: str) -> bool:
    v = re.sub(r"[_\s-]+", " ", value.strip().lower())
    if v.startswith("not ") or v in ("", "false", "no", "0", "none", "all ages", "everyone"):
        return False
    return bool(re.search(r"\b(mature|explicit|adult|18\+?|true|yes|1|nc 17|r rated)\b", v))


def _via(ev: dict[str, Any], value: str) -> str:
    """' [google_books]' when a provider supplied this value (provenance in every reason)."""
    providers = sorted({s["provider"] for s in ev.get("audience_sources", []) if s["value"] == value})
    return f" [{', '.join(providers)}]" if providers else ""


def provider_says_mature(ev: dict[str, Any]) -> bool:
    return any(_is_mature_flag(v) for f in ("maturity", "explicit") for v in ev.get(f, []))


def strong_signals(ev: dict[str, Any]) -> list[dict[str, str]]:
    """Strong audience signals, or none at all when any provider says the book is mature
    (provider disagreement: the cautious reading wins; see raw_strong_signals for what was set aside)."""
    return [] if provider_says_mature(ev) else raw_strong_signals(ev)


def raw_strong_signals(ev: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    structured = [(f, v) for f in ("categories", "subjects", "genres", "audience") for v in ev.get(f, [])]
    for field, value in structured:
        if JUVENILE_CATEGORY_RE.search(value):
            found.append({"id": "juvenile_category", "detail": f"{field}: {value}{_via(ev, value)}"})
    for pub in [*ev.get("publishers", []), *ev.get("imprint", [])]:
        low = pub.lower()
        if any(name in low for name in CHILDRENS_PUBLISHERS):
            found.append({"id": "children_publisher", "detail": pub})
    for raw in ev.get("age_range", []):
        rng = parse_age_range(raw)
        if rng and rng[1] is not None and rng[1] <= STRONG_AGE_UPPER_BOUND:
            found.append({"id": "age_range_upper_bound", "detail": f"age_range: {raw}{_via(ev, raw)}"})
    desc = ev.get("description") or ""
    text_age = _description_age_upper(desc)
    if text_age and text_age[1] <= STRONG_AGE_UPPER_BOUND:
        found.append({"id": "age_range_upper_bound", "detail": f"description: '{text_age[2]}'"})
    stated = _unnegated(_DESC_AUDIENCE_RE, desc)
    if stated:
        found.append({"id": "description_audience_statement", "detail": f"description: '{stated}'"})
    return _dedupe(found)


def adult_signals(ev: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for field in ("maturity", "explicit"):
        for value in ev.get(field, []):
            if _is_mature_flag(value):
                found.append({"id": "maturity_flag", "detail": f"{field}: {value}{_via(ev, value)}"})
    structured = [(f, v) for f in ("categories", "subjects", "genres", "audience") for v in ev.get(f, [])]
    structured += [("series", s["name"]) for s in ev.get("series", [])]
    for field, value in structured:
        if field != "series" and ADULT_AUDIENCE_RE.search(value):
            found.append({"id": "adult_audience_category", "detail": f"{field}: {value}{_via(ev, value)}"})
        if ADULT_GENRE_RE.search(value):
            found.append({"id": "adult_genre", "detail": f"{field}: {value}{_via(ev, value)}"})
    for field, value in (("title", ev.get("title")), ("subtitle", ev.get("subtitle")), ("description", ev.get("description"))):
        m = ADULT_TEXT_RE.search(value or "")
        if m:
            found.append({"id": "adult_text_marker", "detail": f"{field}: '{m.group(0)}'"})
    for p in ev.get("author_prior", []):
        if prior_is_adverse(p):
            found.append({"id": "author_prior_not_kids", "detail": f"{p['name']}: {p['not_kids']} of {p['others']} other book(s) not for kids, {p['kids']} confirmed for kids"})
    for p in ev.get("series_prior", []):
        if prior_is_adverse(p):
            found.append({"id": "series_prior_not_kids", "detail": f"{p['name']}: {p['not_kids']} of {p['others']} other book(s) not for kids, {p['kids']} confirmed for kids"})
    return _dedupe(found)


def _dedupe(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen, out = set(), []
    for i in items:
        key = (i["id"], i["detail"])
        if key not in seen:
            seen.add(key); out.append(i)
    return out


def evaluate_gate(ev: dict[str, Any], *, verdict: str | None, confidence: float | None, threshold: float,
                  require_strong: bool = True, llm_adult_signals: list[str] | None = None) -> dict[str, Any]:
    """May this verdict be applied without a person? Pure function of its arguments.

    passed requires ALL of: verdict kids at or above `threshold`; no hard adult signal
    (and none reported by the model); at least one strong audience signal when
    `require_strong`; a description of MIN_DESCRIPTION_CHARS. `blockers` lists every
    failed condition (not just the first) so the reviewer sees the whole picture."""
    strong, adult = strong_signals(ev), adult_signals(ev)
    set_aside = raw_strong_signals(ev) if provider_says_mature(ev) else []
    blockers: list[str] = []
    if verdict != "kids":
        blockers.append("llm_not_kids")
    elif confidence is None or confidence < threshold:
        blockers.append("below_threshold")
    if llm_adult_signals:
        blockers.append("llm_reported_adult_signals")
    if adult:
        blockers.append("hard_adult_signals")
    if require_strong and not strong:
        blockers.append("no_strong_audience_signal")
    if len(ev.get("description") or "") < MIN_DESCRIPTION_CHARS:
        blockers.append("description_too_short")
    reasons = [BLOCKERS[b] for b in blockers]
    reasons += [f"{HARD_ADULT_SIGNALS[s['id']]} ({s['detail']})" for s in adult]
    if set_aside:
        reasons.append("Providers disagree: a provider marks it mature, so these audience signals do not count: " + "; ".join(s["detail"] for s in set_aside))
    return {"passed": not blockers, "blockers": blockers, "reasons": reasons, "strong_signals": strong, "adult_signals": adult,
            "provider_conflict": set_aside, "threshold": threshold, "require_strong": require_strong}


# ---------------------------------------------------------------- prompt
KIDS_SYSTEM_PROMPT = (
    "You classify whether a book is appropriate and intended for children (roughly "
    "middle-grade readers or younger, up to about age 12). You are given structured "
    "evidence from the library's own records; use ONLY that evidence. Respond with strict "
    'JSON only: {"verdict":"kids"|"not_kids"|"uncertain","confidence":0.0-1.0,'
    '"reason":"one short sentence","audience_evidence":["field: what it says",...],'
    '"adult_signals":["field: what it says",...]}. '
    "audience_evidence lists the evidence fields that support a kids verdict, each naming "
    'the field and quoting what it says (for example "categories: Juvenile Fiction", '
    '"description: ages 8-12"); a kids verdict with an empty audience_evidence is invalid. '
    "adult_signals lists anything pointing the other way (adult genre, mature themes, an "
    "author or series whose other books are not for kids). A juvenile-sounding blurb or a "
    "science-fiction or fantasy premise is NOT audience evidence by itself: adult books "
    "have those too. Answer \"not_kids\" for anything containing sexual content, graphic "
    "violence, horror intended to frighten, substance abuse, or other mature/adult themes - "
    "including young-adult fiction that touches these themes, which is NOT the same as a "
    "children's book. If the author prior or series prior shows other books judged not for "
    "kids, treat that as a strong reason to answer not_kids or uncertain. Answer \"uncertain\" "
    "rather than guessing when the evidence does not settle it. Never explain outside the JSON object."
)


def render_prompt(ev: dict[str, Any]) -> str:
    """The user message: one labelled line per evidence field, Title first."""
    lines = [f"Title: {ev['title']}", f"Author(s): {', '.join(ev['authors']) or 'unknown'}"]
    if ev.get("subtitle"):
        lines.append(f"Subtitle: {ev['subtitle']}")
    if ev.get("series"):
        lines.append("Series: " + "; ".join(s["name"] + (f" #{s['position']}" if s.get("position") else "") for s in ev["series"]))
    for key, label in (("publishers", "Publisher"), ("imprint", "Imprint"), ("categories", "Categories"), ("subjects", "Subjects"),
                       ("genres", "Genres"), ("audience", "Audience"), ("age_range", "Age range"), ("maturity", "Maturity rating"),
                       ("explicit", "Explicit flag"), ("narrators", "Narrator(s)")):
        if ev.get(key):
            lines.append(f"{label}: {'; '.join(ev[key])}")
    for key, label in (("language", "Language"), ("release_date", "Release date"), ("pages", "Pages")):
        if ev.get(key):
            lines.append(f"{label}: {ev[key]}")
    if ev.get("duration_minutes"):
        lines.append(f"Audiobook runtime: {ev['duration_minutes']} minutes")
    for p in ev.get("author_prior", []):
        lines.append(f"Author prior ({p['name']}): {p['not_kids']} other book(s) judged not_kids, {p['kids']} confirmed kids, {p['others']} other book(s) in the library")
    for p in ev.get("series_prior", []):
        lines.append(f"Series prior ({p['name']}): {p['not_kids']} other book(s) judged not_kids, {p['kids']} confirmed kids, {p['others']} other book(s) in the series")
    if ev.get("audience_sources"):
        by_provider: dict[str, list[str]] = {}
        for src in ev["audience_sources"]:
            by_provider.setdefault(src["provider"], [])
            if src["kind"] not in by_provider[src["provider"]]:
                by_provider[src["provider"]].append(src["kind"])
        lines.append("Audience metadata from: " + "; ".join(f"{p} ({', '.join(k)})" for p, k in sorted(by_provider.items())))
    missing = [label for label, keys in (("categories/subjects/genres", ("categories", "subjects", "genres")), ("age range", ("age_range",)),
                                         ("maturity rating", ("maturity", "explicit"))) if not any(ev.get(k) for k in keys)]
    if missing:
        lines.append("Not on record (do not guess these): " + ", ".join(missing))
    lines.append(f"Description: {ev.get('description') or '(none available)'}")
    return "\n".join(lines)


def parse_answer(raw: Any) -> dict[str, Any] | None:
    """The model's answer, strictly: {verdict, confidence, reason, audience_evidence, adult_signals}
    or None. A kids verdict needs a usable 0..1 confidence AND a non-empty audience_evidence
    list of strings; the two lists must be lists of strings when present. Anything else is
    'no answer', never 'kids'."""
    if not isinstance(raw, dict):
        return None
    verdict = raw.get("verdict")
    if verdict not in ("kids", "not_kids", "uncertain"):
        return None
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        confidence = None
    else:
        confidence = float(confidence)
    if verdict == "kids" and confidence is None:
        return None
    lists: dict[str, list[str]] = {}
    for key in ("audience_evidence", "adult_signals"):
        val = raw.get(key, [])
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            return None
        lists[key] = [x.strip()[:300] for x in val if x.strip()][:12]
    if verdict == "kids" and not lists["audience_evidence"]:
        return None
    reason = raw.get("reason")
    reason = reason.strip()[:500] if isinstance(reason, str) and reason.strip() else None
    return {"verdict": verdict, "confidence": confidence, "reason": reason, **lists}
