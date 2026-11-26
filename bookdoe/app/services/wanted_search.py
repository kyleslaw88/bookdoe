"""The automatic wanted-search job: drain the eligible backlog politely.

Ownership, wanted and acquisition intent keep their meaning (docs/INTEGRATIONS.md): this
module only decides WHICH wanted assets (monitored, acquisition intent, status 'missing',
work monitored, nothing in flight) to search next and when to stop. A run

  * orders by the asset's last automatic search (never-searched first), so assets that
    never match cannot starve the rest (the old updated_at ordering never moved);
  * works through the whole backlog in batches of `batch`, starting the next batch at
    once, until drained, out of time budget, out of daily searches, or the indexers push
    back (HTTP 429 / repeated errors);
  * leaves a cooldown after a search that found nothing acceptable (12 h, doubling per
    consecutive miss, capped at 7 days); a grab, a monitoring change or a work edit clears it;
  * never changes media_assets: its only state is wanted_search_state (migration 32), written
    one asset at a time, so a crash mid-run leaves nothing half-done (the asset is just
    searched again next cycle).

Manual searches (releases.search_releases(automatic=False)) are not counted, not delayed and
not blocked by any of this.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from typing import Any, Callable

from .. import db
from ..config import settings
from . import releases
from . import acquisition

log = logging.getLogger(__name__)

# Hooks the tests replace so budgets and pacing run on a fake clock (no real sleeps).
monotonic: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep

DEFAULTS = {
    "auto_grab_min_score": 80,
    "wanted_search_daily_cap": 300,
    "wanted_search_batch": 20,
    "wanted_search_budget_minutes": 20,
    "wanted_search_cooldown_hours": 12,
    "wanted_search_delay_seconds": 1.0,
}
# (min, max) accepted by the settings API and the clamp applied when reading.
BOUNDS = {
    "auto_grab_min_score": (0, 1000),
    "wanted_search_daily_cap": (1, 10000),
    "wanted_search_batch": (1, 100),
    "wanted_search_budget_minutes": (1, 60),
    "wanted_search_cooldown_hours": (1, 168),
    "wanted_search_delay_seconds": (0, 60),
}
MAX_COOLDOWN_SECONDS = 7 * 86400
ERROR_STREAK_STOP = 5
THROTTLE_MARKERS = ("429", "too many requests", "rate limit", "rate-limit", "ratelimit", "retry-after")
MAX_ERRORS_LISTED = 50
THROTTLE_PAUSE_SECONDS = 600
KEY_PREFIX = "acquisition."


def _setting(name: str):
    default = DEFAULTS[name]
    raw = db.get_setting(KEY_PREFIX + name, default)
    lo, hi = BOUNDS[name]
    try:
        value = float(raw) if isinstance(default, float) else int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def auto_grab_min_score() -> int:
    """Score an automatic search result must reach before it is grabbed unattended."""
    return _setting("auto_grab_min_score")


def max_budget_minutes() -> int:
    """A run must stay well inside the job lease (half of it), or the reaper would kill it."""
    return max(1, int(settings.job_lease_seconds // 120))


def get_settings() -> dict[str, Any]:
    out = {name: _setting(name) for name in DEFAULTS}
    out["wanted_search_budget_minutes"] = min(out["wanted_search_budget_minutes"], max_budget_minutes())
    return out


def validate_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """Range-check a partial settings update; ValueError names the offending field."""
    clean: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in DEFAULTS:
            raise ValueError(f"Unknown setting {name}")
        lo, hi = BOUNDS[name]
        if name == "wanted_search_budget_minutes":
            hi = min(hi, max_budget_minutes())
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not lo <= value <= hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        clean[name] = float(value) if isinstance(DEFAULTS[name], float) else int(value)
    return clean


def save_settings(changes: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
    """Persist validated changes and write one audit entry listing old -> new values."""
    clean = validate_changes(changes)
    before = get_settings()
    for name, value in clean.items():
        db.set_setting(KEY_PREFIX + name, value)
    after = get_settings()
    changed = {k: [before[k], after[k]] for k in clean if before[k] != after[k]}
    if "auto_grab_min_score" in changed:
        # A different floor can turn yesterday's "nothing good enough" into a grab: search again.
        with db.tx() as conn:
            conn.execute("DELETE FROM wanted_search_state WHERE last_outcome IN ('no_result','no_match')")
    if changed:
        db.add_history(
            "settings.acquisition",
            "Acquisition settings changed: " + ", ".join(f"{k} {v[0]} -> {v[1]}" for k, v in changed.items()) + f" (by {actor or 'unknown'})",
            data=changed,
        )
    return after


_throttle_until = 0.0
_asset_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_cap_lock = threading.Lock()
_cap_reserved = 0


def note_throttle(seconds: float = THROTTLE_PAUSE_SECONDS) -> None:
    """The indexers pushed back (429): new request searches pause for a while."""
    global _throttle_until
    _throttle_until = max(_throttle_until, time.time() + seconds)


def throttle_remaining() -> float:
    return max(0.0, _throttle_until - time.time())


def asset_lock(asset_id: str) -> threading.Lock:
    """One lock per asset, shared by the scheduled job and request searches, so the two can
    never search-and-grab the same asset at the same time."""
    with _locks_guard:
        return _asset_locks.setdefault(asset_id, threading.Lock())


def _reserve_search(cap: int) -> bool:
    """Check the daily cap and claim a slot as one step, so two workers cannot both take the
    last one. The slot is released once the search has written its release_searches row."""
    global _cap_reserved
    with _cap_lock:
        if searches_last_24h() + _cap_reserved >= cap:
            return False
        _cap_reserved += 1
        return True


def _release_search() -> None:
    global _cap_reserved
    with _cap_lock:
        _cap_reserved = max(0, _cap_reserved - 1)


def cooldown_seconds(misses: int, base_hours: float) -> float:
    """12 h after the first miss, doubling per consecutive miss, never past 7 days."""
    if misses <= 0:
        return 0.0
    return min(base_hours * 3600 * (2 ** (misses - 1)), MAX_COOLDOWN_SECONDS)


def reset_cooldown(*, work_id: str | None = None, asset_id: str | None = None) -> None:
    """Forget the search history of an asset (or all assets of a work): called when a user
    changes monitoring or edits the work, so it is tried again at once, ahead of the queue."""
    with db.tx() as conn:
        if asset_id:
            conn.execute("DELETE FROM wanted_search_state WHERE asset_id=?", (asset_id,))
        if work_id:
            conn.execute("DELETE FROM wanted_search_state WHERE asset_id IN (SELECT id FROM media_assets WHERE work_id=?)", (work_id,))


ELIGIBLE = """a.monitored=1 AND a.has_acquisition_intent=1 AND w.monitored=1 AND a.status='missing'
  AND NOT EXISTS (SELECT 1 FROM downloads d WHERE d.asset_id=a.id
                  AND d.state IN ('grabbed','downloading','completed','import_pending','importing'))"""


def _config(job: dict[str, Any] | None) -> dict[str, Any]:
    """Job config_json (like cover-fetch's `batch`) wins over the settings page."""
    cfg = get_settings()
    raw = db.json_load((job or {}).get("config_json"), {}) or {}
    out = {
        "batch": cfg["wanted_search_batch"], "budget": cfg["wanted_search_budget_minutes"] * 60.0,
        "delay": float(cfg["wanted_search_delay_seconds"]), "cooldown_hours": cfg["wanted_search_cooldown_hours"],
        "daily_cap": cfg["wanted_search_daily_cap"], "min_score": cfg["auto_grab_min_score"],
    }
    try:
        if raw.get("batch") is not None:
            out["batch"] = max(1, min(int(raw["batch"]), 100))
        if raw.get("budget_seconds") is not None:
            out["budget"] = max(10.0, min(float(raw["budget_seconds"]), float(settings.job_lease_seconds) / 2))
        if raw.get("delay_seconds") is not None:
            out["delay"] = max(0.0, min(float(raw["delay_seconds"]), 60.0))
    except (TypeError, ValueError):
        pass
    return out


def searches_last_24h() -> int:
    return db.one("SELECT COUNT(*) n FROM release_searches WHERE automatic=1 AND started_at>?", (db.now() - 86400,))["n"]


def _is_throttle(errors: list[dict[str, Any]]) -> bool:
    return any(any(m in str(e.get("error", "")).lower() for m in THROTTLE_MARKERS) for e in errors)


def _record(asset_id: str, outcome: str, cooldown_hours: float) -> None:
    """Upsert the asset's search summary in one small transaction."""
    now = db.now()
    with db.tx() as conn:
        row = conn.execute("SELECT misses FROM wanted_search_state WHERE asset_id=?", (asset_id,)).fetchone()
        misses = row["misses"] if row else 0
        if outcome in ("no_result", "no_match"):
            misses += 1
            nxt = now + cooldown_seconds(misses, cooldown_hours)
        elif outcome == "grabbed":
            misses, nxt = 0, None
        else:  # an error says nothing about the book: no miss, no cooldown, but it goes to the back
            nxt = None
        conn.execute(
            "INSERT INTO wanted_search_state(asset_id,last_auto_search_at,misses,next_eligible_at,last_outcome,updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(asset_id) DO UPDATE SET last_auto_search_at=excluded.last_auto_search_at,misses=excluded.misses,"
            "next_eligible_at=excluded.next_eligible_at,last_outcome=excluded.last_outcome,updated_at=excluded.updated_at",
            (asset_id, now, misses, nxt, outcome, now),
        )


# ---------- does a release name the wanted book? ----------
# The test is an exact-residue comparison: take the release name, remove the work's author(s) and
# format/noise tokens, and what is left must BE the work's title (or its main title, optionally
# followed by its own subtitle). "Dune" therefore never matches "Dune Messiah" or "Children of Dune".
_ARTICLES = ("the", "a", "an")
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd", "dr", "mr", "mrs", "ms"}
MAX_NEAR_MISSES = 5
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "ß": "ss", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
                           "þ": "th", "Þ": "Th", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i",
                           "‘": "", "’": "", "'": "", "`": "", "´": ""})
_NOISE = {"epub", "epub3", "mobi", "azw", "azw3", "pdf", "mp3", "m4b", "m4a", "flac", "ogg", "opus", "docx", "txt", "djvu", "rtf",
          "retail", "unabridged", "abridged", "audiobook", "audiobooks", "ebook", "ebooks", "audible", "kindle", "eng", "english"}
_NOISE_RE = re.compile(r"v\d+|(?:19|20)\d\d|\d+kbps")
# Words that mark a different product (a summary, a box set, a sequel note, a translation, an adaptation...):
# never an acceptable extra.
_NOT_THE_BOOK = {"summary", "analysis", "workbook", "study", "discussion", "teachers", "graphic", "box", "boxed", "set", "collection", "companion", "sequel", "trilogy",
                 "omnibus", "review", "quicklet", "cliffnotes", "sparknotes", "illustrated", "complete", "anthology",
                 "adapted", "young", "readers", "edition", "editions", "edicion", "espanol", "spanish", "francais", "french", "deutsch", "german",
                 "italiano", "translation", "translated", "radio", "dramatisation", "dramatization", "dramatised", "dramatized", "bbc", "losers"}
# plus words that make a capitalised phrase a product/series note rather than a person's name
_NOT_NAME = _NOT_THE_BOOK | {"series", "book", "books", "volume", "vol", "part", "classics", "library", "press"}
_PARTICLES = {"du", "de", "la", "van", "von", "der", "el", "al"}   # surname particles: "Daphne du Maurier"
_EDITION_ADJ = {"deluxe", "anniversary", "special", "collectors", "collector", "revised", "updated", "expanded", "centennial", "limited", "signature",
                "reissue", "commemorative", "definitive"}
_ORDINAL_RE = re.compile(r"\d+(?:st|nd|rd|th)")
_PUBLISHER_END = {"classics", "library", "press", "books"}   # "(Penguin Classics)", "(Folger Shakespeare Library)"
_AUDIO_WORDS = {"m4b", "mp3", "m4a", "audiobook", "audiobooks", "unabridged", "audible"}
_NARR_RE = re.compile(r"\b(?:(?:read|narrated|performed)\s+by|narrator)\b[\s:]*[^\[\]\(\)\|]*?(?=\s[-\u2013\u2014|]\s|[\[\]\(\)\|]|$)", re.I)
_NAME_WORD_RE = re.compile(r"(?:[^\W\d_]\.){1,4}|[^\W\d_]+(?:['\u2019-][^\W\d_]+)*\.?")
_BRACKET_RE = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")


def _tok(value: str | None) -> list[str]:
    """Normalized tokens: lower case, no accents (o-slash, ae, l-stroke, eszett transliterated), apostrophes
    dropped, every other punctuation (dots, underscores, hyphens...) a space. A colon stays as its own ':' token."""
    s = unicodedata.normalize("NFKD", (value or "").translate(_TRANSLIT))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold().replace("&", " and ").replace(":", " : ")
    return re.sub(r"[^a-z0-9:]+", " ", s).split()


def _is_noise(tok: str) -> bool:
    return tok in _NOISE or bool(_NOISE_RE.fullmatch(tok))


def _strip_article(toks: list[str]) -> list[str]:
    return toks[1:] if len(toks) > 1 and toks[0] in _ARTICLES else toks


def _work_title_parts(title: str) -> tuple[list[str], list[str], list[str]]:
    """(full, main, subtitle) token lists of a work title, leading articles dropped, without a trailing
    "(Series, #3)" parenthetical. main == full when there is no subtitle."""
    base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title or "").strip() or (title or "")
    parts = re.split(r"[:–—]| - ", base, maxsplit=1)
    full = _strip_article([t for t in _tok(base) if t != ":"])
    main = _strip_article([t for t in _tok(parts[0]) if t != ":"])
    sub = _strip_article([t for t in _tok(parts[1]) if t != ":"]) if len(parts) > 1 else []
    return full, main, sub


def _people(authors: list[str]) -> list[tuple[list[str], list[str]]]:
    """(given tokens, surname tokens) per person. Handles "First Last", "Last, First", several authors in
    one string ("A B, C D" / "A and B"), suffixes (Jr.) and compound surnames ("El-Sayed")."""
    people: list[tuple[list[str], list[str]]] = []
    for raw in authors:
        for chunk in re.split(r"\s+(?:and|&)\s+|;|\s+/\s+", raw or ""):
            parts = [p.strip() for p in chunk.split(",") if p.strip()]
            if len(parts) == 2 and len(parts[0].split()) == 1 and (_tok(parts[1]) or [""])[0] not in _NAME_SUFFIXES:
                people.append(([t for t in _tok(parts[1])], _tok(parts[0])))   # "Last, First"
                continue
            for part in parts:
                words = [w for w in part.split() if " ".join(_tok(w)) not in _NAME_SUFFIXES]
                if words:
                    given, sur = [t for w in words[:-1] for t in _tok(w)], _tok(words[-1])
                    while len(given) > 1 and given[-1] in _PARTICLES:   # "Daphne du Maurier": du belongs to the surname
                        sur = [given.pop()] + sur
                    people.append((given, sur))
    return [(g, s) for g, s in people if s]


def _author_form_sets(authors: list[str]) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    """(full forms, surname-only forms). A full form names the person: given name(s) or initials plus the
    surname, in either order ("First Last", "Last First"); a one-name author's name is a full form. A
    surname-only form ("Last", or the joined hyphenated "elsayed") is weaker evidence. A hyphenated or
    particle surname is one unit (its tokens together or joined), never split."""
    full: set[tuple[str, ...]] = set()
    only: set[tuple[str, ...]] = set()
    for given, sur in _people(authors):
        for sv in {tuple(sur), ("".join(sur),)}:
            (only if given else full).add(sv)   # a very short whole surname ("Ng", "Poe") is allowed; only pieces of one are not
            for g in (given, given[:1], [t[0] for t in given], ["".join(t[0] for t in given)], [given[0][0]] if given else []):
                if g:
                    full.add(tuple(g) + sv)
                    full.add(sv + tuple(g))
    return full, only - full


def _author_forms(authors: list[str]) -> list[tuple[str, ...]]:
    """Every token sequence that names an author in a release, longest first."""
    full, only = _author_form_sets(authors)
    return sorted(full | only, key=lambda f: (-len(f), f))


def _remove_seq(toks: list[str], form: tuple[str, ...], mark: str) -> list[str]:
    out, i, n = [], 0, len(form)
    while i < len(toks):
        if tuple(toks[i:i + n]) == form:
            out.append(mark)
            i += n
        else:
            out.append(toks[i])
            i += 1
    return out


def _num_matches(tok: str, position: Any) -> bool:
    try:
        return position is None or int(float(position)) == int(tok)
    except (TypeError, ValueError):
        return True


def _series_list(series: Any) -> list[tuple[list[str], Any]]:
    out = []
    for s in series or []:
        name, pos = (s.get("name"), s.get("position")) if isinstance(s, dict) else (s, None)
        toks = _strip_article([t for t in _tok(name) if t != ":"])
        if toks:
            out.append((toks, pos))
    return out


def _contains(seq: list[str], part: list[str]) -> bool:
    n = len(part)
    return bool(n) and any(seq[i:i + n] == part for i in range(len(seq) - n + 1))


def _eq(a: list[str], b: list[str]) -> bool:
    return _strip_article(a) == _strip_article(b)


def _descriptive_subtitle(work_title: str) -> bool:
    """A stored subtitle that describes the book ("A Memoir", "A Brief History of Humankind") rather than
    naming an instalment ("Messiah", "The Final Empire"): starts with a/an or is five or more words."""
    base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", work_title or "").strip()
    parts = re.split(r"[:–—]| - ", base, maxsplit=1)
    toks = [t for t in _tok(parts[1]) if t != ":"] if len(parts) > 1 else []
    return bool(toks) and (toks[0] in ("a", "an") or len(toks) >= 5)


def _name_like(seg: str) -> bool:
    """2 to 4 capitalised name words ("Perdita Weeks", "R.C. Bray") and nothing else."""
    s = seg.strip()
    if not s or re.search(r"[,;&/:\d]", s):
        return False
    words = s.split()
    if not 2 <= len(words) <= 4:
        return False
    for w in words:
        if not w[0].isupper() or not _NAME_WORD_RE.fullmatch(w) or (set(_tok(w)) & (_NOT_NAME | _NOISE)):
            return False
    return True


def _strip_narrators(text: str, forms: list[tuple[str, ...]], protected: set[str], series_toks: list[list[str]]) -> str:
    """Audio releases carry the narrator ("read by X", "- X", "[X]"). Remove an explicit credit and any
    separate name-like segment that is neither an author, nor part of the title, nor the series."""
    text = _NARR_RE.sub(" ", text)
    avoid = protected | {t for n in series_toks for t in n}
    parts = re.split(r"(\s[-–—|]\s|[\[\]\(\)])", text)
    for k in range(0, len(parts), 2):
        if not _name_like(parts[k]):
            continue
        toks = _tok(parts[k])
        if set(toks) & avoid or any(_remove_seq(toks, f, "@") != toks for f in forms):
            continue
        parts[k] = " "
    return "".join(parts)


def _extra_person(text: str, authors: list[str], protected: set[str]) -> bool:
    """The release names its author by surname alone: is there another person around that surname (a capitalised
    given name before it or after it, or an "and / & / ," list entry) who is not one of the work's authors?
    "Dune: The Butlerian Jihad - Brian Herbert" has one; "Dune by Herbert" does not."""
    people = _people(authors)
    own = {t for g, s in people for t in g + s}
    surs = {tuple(s) for _, s in people} | {("".join(s),) for _, s in people}
    ents: list[tuple[str, bool, str]] = []   # (token, capitalised, separator characters before it)
    sep = ""
    for m in re.finditer(r"([^\W_]+(?:['’-][^\W_]+)*)|([^\w\s]|_)", text):
        if m.group(1):
            for k, t in enumerate(_tok(m.group(1))):
                ents.append((t, m.group(1)[0].isupper(), sep if k == 0 else ""))
            sep = ""
        else:
            sep += m.group(2)

    def other(e: tuple[str, bool, str]) -> bool:
        t = e[0]
        return e[1] and not (t in own or t in protected or _is_noise(t) or t in _ARTICLES or t in ("by", "and"))

    listy = set(",;& ")
    for i in range(len(ents)):
        for sv in surs:
            n = len(sv)
            if tuple(e[0] for e in ents[i:i + n]) != sv:
                continue
            gap = ents[i][2]
            if i and (not gap.strip() or set(gap) <= listy):
                p = ents[i - 1]
                if p[0] == "and" and i > 1:
                    p = ents[i - 2]
                if other(p):
                    return True
            j = i + n
            if j < len(ents):
                nx = ents[j]
                if nx[0] == "and" and j + 1 < len(ents):
                    nx = ents[j + 1]
                if set(ents[j][2]) <= listy and other(nx):
                    return True
    return False


def release_match(work_title: str, authors: list[str], release_title: str, series: Any = None, kind: str | None = None) -> tuple[str, tuple[str, ...]] | None:
    """Classify a release against the wanted work: None (not this book), ("exact", ()) (the release's text,
    minus the work's author(s) and noise, IS the work's title) or ("free", subtitle tokens) (the work's main
    title plus a subtitle the work has no record of: "Healing Politics: A Doctor's Note on ..."). The free
    form is the risky one; search_and_grab only takes it when no other free result has a different subtitle.

    The free form is allowed ONLY when all hold: the work has no subtitle on file; it is not in a series whose
    name equals or contains its main title; the main title has 2 or more words; a full author form (given name
    + surname, either order) is in the release, never a surname alone; and the subtitle is plausible (3+ words
    or starting with an article, no summary/box set/sequel/translation/adaptation words)."""
    full, main, sub = _work_title_parts(work_title)
    if not full:
        return None
    protected = set(full) | set(sub)
    sers = _series_list(series)
    series_blocks_main = any(_contains(n, main) for n, _ in sers)
    people = _people(authors)
    full_forms, only_forms = _author_form_sets(authors)
    all_forms = sorted(full_forms | only_forms, key=lambda f: (-len(f), f))
    tagged: list[list[str]] = []

    def bracket(m: re.Match) -> str:
        inner = [t for t in _tok(m.group(1)) if t != ":"]
        if inner and all(_is_noise(t) for t in inner):
            return " "
        if inner and len(inner) <= 4 and inner[-1] in _PUBLISHER_END and not (set(inner) & protected):
            return " "   # "(Penguin Classics)", "(Folger Shakespeare Library)"
        stripped = _strip_article(inner)
        names = [(n, p) for n, p in sers] + ([] if any(n == main for n, _ in sers) else [(main, None)])
        for name, pos in names:
            k = len(name)
            if stripped == name or (len(stripped) == k + 1 and stripped[:k] == name and stripped[k].isdigit() and _num_matches(stripped[k], pos)) \
                    or (len(stripped) == k + 2 and stripped[:k] == name and stripped[k] in ("book", "vol", "volume", "part", "no", "number")
                        and stripped[k + 1].isdigit() and _num_matches(stripped[k + 1], pos)):
                if any(name == n and pos == p for n, p in sers):
                    tagged.append(name)
                return " "
        return " " + m.group(1) + " "

    text = release_title or ""
    if kind == "audiobook" or (set(_tok(text)) & _AUDIO_WORDS) or _NARR_RE.search(text):
        text = _strip_narrators(text, all_forms, protected, [n for n, _ in sers])
    for _ in range(4):
        new = _BRACKET_RE.sub(bracket, text)
        if new == text:
            break
        text = new
    toks = _tok(text)
    strong = False
    if people:
        for form in sorted(full_forms, key=lambda f: (-len(f), f)):
            toks = _remove_seq(toks, form, "@")
        for form in sorted(only_forms, key=lambda f: (-len(f), f)):
            toks = _remove_seq(toks, form, "#")
        strong = "@" in toks
        if not strong and "#" not in toks:
            return None   # no author named at all: left for a human (a near miss)
        if not strong and _extra_person(text, authors, protected):
            return None   # a surname alone, with another person beside it ("Brian Herbert" for Frank Herbert)
        changed = True
        while changed:   # "by" / "and" glue next to an author name is not title text
            changed = False
            for i, t in enumerate(toks):
                if t in ("by", "and") and ((i > 0 and toks[i - 1] in ("@", "#")) or (i + 1 < len(toks) and toks[i + 1] in ("@", "#"))):
                    toks = toks[:i] + toks[i + 1:]
                    changed = True
                    break
    res: list[str] = []
    for t in toks:
        if t in ("@", "#") or (_is_noise(t) and t not in protected):
            continue
        if t == "edition" and "edition" not in protected:   # "Deluxe Edition", "25th Anniversary Edition" are noise; "Spanish Edition" is not
            k = len(res)
            while k and (res[k - 1] in _EDITION_ADJ or _ORDINAL_RE.fullmatch(res[k - 1])):
                k -= 1
            if k < len(res):
                del res[k:]
                continue
        res.append(t)
    # a colon left dangling at an edge (from a removed author or tag) carries no meaning
    while res and res[0] == ":":
        res = res[1:]
    while res and res[-1] == ":":
        res = res[:-1]
    res = _strip_article(res)
    plain = [t for t in res if t != ":"]
    if not plain:
        return None
    exact = ("exact", ())
    if plain == full or plain == main:
        # The bare main title of a work stored WITH a subtitle ("Dune: Messiah") may be a different book of the
        # same name. Trusted only when the main title is 2+ words and not a series name; a one-word main title
        # is also trusted when the stored subtitle just describes the book ("Educated: A Memoir").
        if plain == main and plain != full and (series_blocks_main or not (len(main) >= 2 or _descriptive_subtitle(work_title))):
            return None
        return exact
    if sub and (plain == main + sub or (plain[:len(main)] == main and sub[:len(plain) - len(main)] == plain[len(main):] and len(plain) > len(main))):
        return exact   # main title and the work's own subtitle, or a prefix of it
    if sub and plain == sub and any(t == main for t in tagged):
        return exact   # "The Final Empire (Mistborn 1)" for "Mistborn: The Final Empire"
    # "Title 01 - Title" or "Series 3 - Title": a series number in front
    for i, t in enumerate(plain):
        if t.isdigit() and i:
            pre, post = plain[:i], plain[i + 1:]
            if _eq(post, full) or _eq(post, main):
                if _eq(pre, full) or _eq(pre, main) or any(_eq(pre, n) and _num_matches(t, p) for n, p in sers):
                    return exact
            if sub and _eq(post, sub) and any(_eq(pre, n) and _num_matches(t, p) for n, p in sers):
                return exact   # "Mistborn 1 - The Final Empire", the series on file
    # "Title: any subtitle", where the work has none on file and the subtitle is plausibly one
    k = len(main)
    if not sub and strong and k >= 2 and not series_blocks_main and res[:k] == main and len(res) > k + 1 and res[k] == ":":
        rest = res[k + 1:]
        if ":" not in rest and not (set(rest) & _NOT_THE_BOOK) and "series book" not in " ".join(rest) \
                and (len(rest) >= 3 or rest[0] in _ARTICLES):
            return ("free", tuple(_strip_article(rest)))
    return None


def release_matches_work(work_title: str, authors: list[str], release_title: str, series: Any = None, kind: str | None = None) -> bool:
    """True when the release names this work (see release_match). Judged alone: a free "Title: subtitle"
    release passes here; search_and_grab additionally refuses it when other results carry other subtitles.
    Strict on purpose: a wrong book auto-grabbed is worse than a book left for a human to pick."""
    return release_match(work_title, authors, release_title, series, kind) is not None



def _still_wanted(asset_id: str) -> bool:
    """Eligibility right now: monitored and wanted, still missing, nothing downloading or importing."""
    return bool(db.one(f"SELECT 1 x FROM media_assets a JOIN works w ON w.id=a.work_id WHERE a.id=? AND {ELIGIBLE}", (asset_id,)))  # nosec B608


def search_and_grab(asset: dict[str, Any], min_score: int | None = None, detail: dict[str, Any] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Search once (automatic, so counted against the daily cap) and auto-grab the best
    acceptable release that scores >= min_score AND is plausibly the wanted work.

    Returns (outcome, errors): 'grabbed' | 'no_result' | 'no_match' (acceptable releases above
    the floor exist but none names the book: nothing is grabbed, the near misses go into
    detail['near_misses'] for a human) | 'error' (every indexer failed, or the search raised) |
    'skipped_active' (no longer wanted, owned, or a download is already running: nothing searched,
    no cooldown) | 'skipped_cap' (daily cap reached). Holds the asset's lock for the whole search
    and grab, shared by the job and request searches. Never raises. The caller records
    grabbed / no_result / no_match / error with _record."""
    from . import downloads  # late: downloads imports scheduler-adjacent modules
    floor = auto_grab_min_score() if min_score is None else min_score
    with asset_lock(asset["id"]):
        reserved = False
        try:
            if not _still_wanted(asset["id"]):
                return "skipped_active", []
            if not _reserve_search(_setting("wanted_search_daily_cap")):
                return "skipped_cap", []
            reserved = True
            result = releases.search_releases(asset["work_id"], asset["kind"], automatic=True)
            errs = list(result.get("errors") or [])
            if not result["items"] and errs:
                return "error", errs   # every indexer failed: that is not "not found"
            candidates = [r for r in result["items"] if not r["rejected"] and r["score"] >= floor]
            if not candidates:
                return "no_result", errs
            ctx = releases._work_context(asset["work_id"])
            near: list[dict[str, Any]] = []
            exact: list[dict[str, Any]] = []
            free: list[tuple[dict[str, Any], tuple[str, ...]]] = []
            for r in candidates:
                m = release_match(ctx["work"]["title"], ctx["authors"], r.get("title") or "", ctx.get("series"), asset.get("kind"))
                if m is None:
                    near.append({"release_id": r["id"], "title": r.get("title"), "score": r["score"]})
                elif m[0] == "exact":
                    exact.append(r)
                else:
                    free.append((r, m[1]))
            pick = exact[0] if exact else (free[0][0] if free and len({sub for _, sub in free}) == 1 else None)
            if pick is not None:
                if not _still_wanted(asset["id"]):   # the search took a while: look once more before grabbing
                    return "skipped_active", errs
                downloads.grab_release(pick["id"])
                if detail is not None:
                    detail["release"] = {"release_id": pick["id"], "title": pick.get("title"), "score": pick["score"]}
                return "grabbed", errs
            # no exact match, and the free "Title: subtitle" results disagree about the subtitle (or none exist):
            # grab nothing, list them all for a human
            near.extend({"release_id": r["id"], "title": r.get("title"), "score": r["score"]} for r, _ in free)
            if detail is not None:
                detail["near_misses"] = near[:MAX_NEAR_MISSES]
            return "no_match", errs
        except Exception as exc:
            return "error", [{"error": str(exc)}]
        finally:
            if reserved:
                _release_search()


def run(job: dict[str, Any] | None = None, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    acquisition.ensure_enabled()
    cfg = _config(job)
    t0 = monotonic()
    now = db.now()
    # Assets that stopped being wanted since their last search (monitoring switched off,
    # owned, deleted-and-recreated) must not carry an old cooldown back in when re-wanted.
    with db.tx() as conn:
        conn.execute(
            f"DELETE FROM wanted_search_state WHERE asset_id NOT IN (SELECT a.id FROM media_assets a JOIN works w ON w.id=a.work_id WHERE {ELIGIBLE})"  # nosec B608
        )
    eligible = db.one(f"SELECT COUNT(*) n FROM media_assets a JOIN works w ON w.id=a.work_id WHERE {ELIGIBLE}")["n"]  # nosec B608
    cooling = db.one(
        f"SELECT COUNT(*) n FROM media_assets a JOIN works w ON w.id=a.work_id JOIN wanted_search_state s ON s.asset_id=a.id "  # nosec B608
        f"WHERE {ELIGIBLE} AND s.next_eligible_at>?", (now,))["n"]
    res: dict[str, Any] = {
        "eligible": eligible, "searched": 0, "grabbed": 0, "skipped_on_cooldown": cooling, "no_result": 0, "no_match": 0, "skipped_active": 0, "near_misses": [],
        "errors": [], "error_count": 0, "stopped_reason": None, "batches": 0, "elapsed": 0.0, "asset_ids": [],
    }
    seen: list[str] = []
    streak = 0

    def progress() -> None:
        res["elapsed"] = round(monotonic() - t0, 1)
        if on_progress:
            try:
                on_progress(dict(res))
            except Exception:
                log.exception("wanted-search progress update failed")

    progress()
    reason = None
    while reason is None:
        rows = db.all(
            f"""SELECT a.* FROM media_assets a JOIN works w ON w.id=a.work_id
                LEFT JOIN wanted_search_state s ON s.asset_id=a.id
                WHERE {ELIGIBLE} AND (s.next_eligible_at IS NULL OR s.next_eligible_at<=?)
                  AND a.id NOT IN (SELECT value FROM json_each(?))
                ORDER BY s.last_auto_search_at IS NOT NULL, s.last_auto_search_at ASC, a.added_at ASC, a.id ASC LIMIT ?""",  # nosec B608
            (now, db.json_dump(seen), cfg["batch"]),
        )
        if not rows:
            reason = "drained"
            break
        res["batches"] += 1
        for a in rows:
            if monotonic() - t0 >= cfg["budget"]:
                reason = "budget"
                break
            if searches_last_24h() >= cfg["daily_cap"]:
                reason = "daily_cap"
                break
            if res["searched"] and cfg["delay"] > 0:
                sleep(min(cfg["delay"], max(0.0, cfg["budget"] - (monotonic() - t0))))
                if monotonic() - t0 >= cfg["budget"]:
                    reason = "budget"
                    break
            seen.append(a["id"])
            detail: dict[str, Any] = {}
            outcome, errs = search_and_grab(a, cfg["min_score"], detail)
            if outcome == "skipped_cap":
                reason = "daily_cap"
                break
            if outcome == "skipped_active":   # handled elsewhere meanwhile: not a search, no cooldown
                res["skipped_active"] += 1
                continue
            res["searched"] += 1
            res["asset_ids"].append(a["id"])
            if outcome == "no_match" and len(res["near_misses"]) < MAX_ERRORS_LISTED:
                res["near_misses"].append({"asset_id": a["id"], "work_id": a["work_id"], "kind": a["kind"], "releases": detail.get("near_misses", [])})
            if outcome == "grabbed":
                res["grabbed"] += 1
            elif outcome == "no_result":
                res["no_result"] += 1
            elif outcome == "no_match":
                res["no_match"] += 1
            if outcome == "error":
                res["error_count"] += 1
                if len(res["errors"]) < MAX_ERRORS_LISTED:
                    res["errors"].append({"asset_id": a["id"], "work_id": a["work_id"], "error": str(errs[0].get("error", "")) if errs else "search failed"})
                streak += 1
            else:
                streak = 0
            try:
                _record(a["id"], outcome, cfg["cooldown_hours"])
            except Exception:
                log.exception("wanted-search: could not record state for %s", a["id"])
            if _is_throttle(errs):
                note_throttle()
                reason = "throttled"
                break
            if streak >= ERROR_STREAK_STOP or (res["searched"] >= 10 and res["error_count"] * 2 >= res["searched"]):
                reason = "error_rate"
                break
        progress()
    res["stopped_reason"] = reason
    res["elapsed"] = round(monotonic() - t0, 1)
    return res
