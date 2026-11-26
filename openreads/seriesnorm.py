"""Series-name cleanup for Open Library's free-text series strings.

Open Library series data is inconsistent: one real series shows up as
"A Molly Murphy mystery", "The Molly Murphy mysteries", "Molly Murphy
Mystery", ... and publisher imprints ("Thorndike Press large print core")
get entered in the series field. Two heuristics, applied wherever series
are assembled (openlibrary._series_from_raw, db.get_or_create_series):

1. norm_key(): a case/punctuation/article/generic-suffix-insensitive key.
   Names with the same key -- or a very close key (see similar()) -- are
   the same series.
2. is_imprint(): a denylist of patterns that look like publisher/imprint
   or edition labels, not series. To extend it, add a regex to
   IMPRINT_PATTERNS or a word to IMPRINT_WORDS.

Both are heuristics. Known limits: a real series whose name really
contains e.g. "Press" or "Large Print" would be dropped; two distinct
series differing only by a trailing generic word ("Foo Mysteries" vs
"Foo") would be merged.
"""
import difflib
import re

_LEADING_ARTICLES = {"a", "an", "the"}
# Trailing words that describe the kind of series, not which series.
GENERIC_SUFFIXES = {"mystery", "mysteries", "series", "novel", "novels", "book", "books", "saga"}

# Words that mark a publisher/imprint, matched as whole words.
IMPRINT_WORDS = {
    "thorndike", "beeler", "wheeler", "ulverscroft", "chivers", "isis",
    "cengage", "gale", "harlequin", "berkley", "kensington", "audible",
}
IMPRINT_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\blarge[- ]?print\b",
        r"\bbook[- ]?club\b",
        r"\bpress\b",
        r"\bpublish(?:ing|ers?)\b",
        r"\bprime crime\b",
        r"\bcenter point\b",
        r"\b(?:library|special|collector'?s|kindle|paperback|hardcover|anniversary|"
        r"reprint|movie tie-?in|premium|deluxe|mass[- ]market)\s+editions?\b",
        r"\bcore series\b",
    )
]


def is_imprint(name: str) -> bool:
    if not name:
        return False
    words = set(re.findall(r"[a-z]+", name.lower()))
    if words & IMPRINT_WORDS:
        return True
    return any(p.search(name) for p in IMPRINT_PATTERNS)


# A generic suffix is part of a meaningful compound, not strippable, when
# it follows one of these ("Graphic Novel", "Light Novel", ...).
_COMPOUND_MODIFIERS = {"graphic", "light", "visual", "illustrated", "picture", "chapter", "comic", "short"}


def _strippable(tokens) -> bool:
    """Can the last token be stripped as a generic suffix?"""
    if len(tokens) < 2 or not _is_generic(tokens[-1].lower()):
        return False
    return tokens[-2].lower() not in _COMPOUND_MODIFIERS


def _is_generic(token: str) -> bool:
    if token in GENERIC_SUFFIXES:
        return True
    # tolerate typos ("msytery") for reasonably long words
    return len(token) >= 5 and any(
        difflib.SequenceMatcher(None, token, g).ratio() >= 0.85
        for g in GENERIC_SUFFIXES
        if len(g) >= 5
    )


def norm_key(name: str) -> str:
    text = re.sub(r"[^\w\s]", " ", (name or "").lower().replace("&", " and "))
    tokens = text.split()
    while len(tokens) > 1 and tokens[0] in _LEADING_ARTICLES:
        tokens.pop(0)
    while _strippable(tokens):
        tokens.pop()
    return " ".join(tokens)


def title_key(title: str) -> str:
    """Key for 'same book' detection across duplicate Open Library works:
    case, punctuation (apostrophes dropped, so "Murphy's" == "Murphys") and
    leading article insensitive. Unlike norm_key it keeps every word."""
    text = re.sub(r"['\u2019]", "", (title or "").lower().replace("&", " and "))
    tokens = re.sub(r"[^\w\s]", " ", text).split()
    while len(tokens) > 1 and tokens[0] in _LEADING_ARTICLES:
        tokens.pop(0)
    return " ".join(tokens)


def bare_form(name: str) -> str:
    """The name with leading article and trailing generic words removed,
    original casing kept ("A Royal Spyness mystery" -> "Royal Spyness").
    Only used when several spellings of one series were merged and none is
    already bare; a lone name is never rewritten."""
    tokens = re.sub(r"[^\w\s'&-]", " ", name).split()
    while len(tokens) > 1 and tokens[0].lower() in _LEADING_ARTICLES:
        tokens.pop(0)
    while len(tokens) > 1 and _strippable([t.lower() for t in tokens]):
        tokens.pop()
    return " ".join(tokens)


def is_bare(name: str) -> bool:
    """True if the name has no leading article / generic suffix, i.e. it
    is already in canonical form."""
    return norm_key(name) == re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", name.lower().replace("&", " and "))).strip()


def similar(key_a: str, key_b: str) -> bool:
    """Exact key match, or a near miss (typo) between longer names. Never
    merges names whose digits differ ("Book 1" vs "Book 2")."""
    if key_a == key_b:
        return True
    if re.findall(r"\d+", key_a) != re.findall(r"\d+", key_b):
        return False
    if _one_word_gap(key_a.split(), key_b.split()):
        return True
    if min(len(key_a), len(key_b)) < 8:
        return False
    return difflib.SequenceMatcher(None, key_a, key_b).ratio() >= 0.9


def _one_word_gap(ta, tb) -> bool:
    """True if one name is the other with exactly one word missing from the
    *middle* ("Constable Evans" vs "Constable Evan Evans": how Audible and
    Open Library name the same series). Both ends must match, so a name that
    merely extends another ("Harry Potter" / "Harry Potter and the Cursed
    Child", "Wings of Fire" / "Wings of Fire Legends") is never merged."""
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) < 2 or len(long_) - len(short) != 1:
        return False
    if short[0] != long_[0] or short[-1] != long_[-1]:
        return False
    return any(long_[:i] + long_[i + 1:] == short for i in range(1, len(long_) - 1))


def pick_canonical(variants):
    """Choose the display name for a group of variants (list of names, one
    per occurrence): prefer names already in bare form, then the most
    frequent, then the shortest, then alphabetical (deterministic)."""
    counts = {}
    for v in variants:
        counts[v] = counts.get(v, 0) + 1
    return min(counts, key=lambda v: (not is_bare(v), -counts[v], len(v), v))


# Trailing position markers in stored/raw series strings: "X -- [16]",
# "X -- bk. 6", "X (6)", "X #6", "X, Book 6", "X ; 12".
_POSITION = r"(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)"
_POSITION_PATTERNS = [
    re.compile(r"^(?P<name>.+?)\s*(?:--|;|:|,|-)\s*(?:bk\.?|book|vol\.?|volume|no\.?|#|part|pt\.?)?\s*\[?\s*" + _POSITION + r"\s*\]?\s*$", re.I),
    re.compile(r"^(?P<name>.+?)\s*\(\s*(?:(?:bk\.?|book|vol\.?|volume|no\.?|#|part|pt\.?)\s*)?" + _POSITION + r"\s*\)\s*$", re.I),
    re.compile(r"^(?P<name>.+?)\s+(?:(?:bk\.?|book|vol\.?|volume|no\.?|part|pt\.?)\s*|#\s*)" + _POSITION + r"\s*$", re.I),
]


def split_position(raw):
    """'X -- [16]' -> ('X', '16'); (raw, None) if no clean position."""
    if not isinstance(raw, str):
        return raw, None
    raw = raw.strip()
    for pat in _POSITION_PATTERNS:
        m = pat.match(raw)
        if m:
            name = m.group("name").strip(" ,;:-")
            if name and not name.isdigit():
                return name, re.sub(r"\s+", "", m.group(2))
    return raw, None
