"""Series-name cleanup for free-text series strings coming out of OpenReads.

OpenReads already runs its own normalizer (openreads/seriesnorm.py) before minting
a canonical series id, but a metadata refresh can still hand bookdoe two
different ids for what is obviously the same series - a spelling variant that
openreads' own merge pass has not (yet) run again since, or a raw catalog string
its patterns do not cover (spelled-out position words, non-English imprint
statements). This is bookdoe's own, independent safety net: series are only
ever shown to a user after passing through here, regardless of what id OpenReads
attached.

Two heuristics, mirroring the openreads original:

1. norm_key(): a case/punctuation/article/generic-suffix-insensitive key. Names
   with the same key -- or a very close key (see similar()) -- are the same series.
2. is_junk(): free text that is not a series at all - a publisher/imprint
   statement or a library classification string - and should not be shown or
   linked at all.

Both are heuristics with known false-positive/false-negative edges; see the
docstrings below for the specific tradeoffs accepted.
"""
from __future__ import annotations

import difflib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any
import re

_LEADING_ARTICLES = {"a", "an", "the"}
GENERIC_SUFFIXES = {"mystery", "mysteries", "series", "novel", "novels", "book", "books", "saga"}

IMPRINT_WORDS = {
    "thorndike", "beeler", "wheeler", "ulverscroft", "chivers", "isis",
    "cengage", "gale", "harlequin", "berkley", "kensington", "audible",
    # Non-English imprint/collection lines seen in library-catalog "series"
    # fields (German pulp-paperback classification statements in particular).
    "heyne", "reihe", "bucher", "taschenbuch", "verlag",
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
        r"\bnr\.\s*\d+\b",  # "Heyne allgemeine Reihe -- Nr. 10624" style catalog numbers
    )
]


# Catalog identifiers and shelf noise that are not names at all (Part XV "numeric
# junk"): "1028", "#20056/1", "10|18", "100 pages", "(Gollancz SF)" (a whole
# parenthesised string is an imprint/classification note, never a title), and
# "Novels by <author>" collection labels. A real series with digits in its name
# ("39 Clues", "Fahrenheit 451 Saga") has other words and is untouched.
_NON_NAME_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"^#?\s*\d+(?:\s*[/|]\s*\d+)*$",
        r"^\d+\s*(?:pages?|pp?\.?)$",
        r"^\(.*\)$",
        r"^novels\s+by\b",
        r"^(?:book\s*)?\d+\s*(?:of|von|sur)\s*\d+$",
    )
]

_SHELFMARK = re.compile(r"^[A-Za-z]{0,4}\.?\s?\d[\d./-]*$|^[A-Z]{1,5}$")


def is_junk(name: str) -> bool:
    """True for text that looks like a publisher imprint or library
    classification string rather than an actual book series.

    A real series name essentially never has more than one " -- " segment
    (that separator is how library catalogs chain classification levels:
    imprint -- sub-imprint -- shelf number), so two or more is treated as a
    strong signal on its own, independent of language. A single " -- " whose
    tail is a shelfmark-shaped code (this function is called after
    split_position has already had its chance to read that tail as a real
    position, so anything left here was not one) is the same signal for a
    catalog that only chains two levels.
    """
    if not name:
        return False
    if name.count(" -- ") >= 2:
        return True
    if " -- " in name and _SHELFMARK.match(name.rsplit(" -- ", 1)[1].strip()):
        return True
    stripped = name.strip()
    if any(p.match(stripped) for p in _NON_NAME_PATTERNS):
        return True
    words = set(re.findall(r"[a-z]+", name.lower()))
    if words & IMPRINT_WORDS:
        return True
    if any(p.search(name) for p in IMPRINT_PATTERNS):
        return True
    return publisher_reason(name) is not None


_COMPOUND_MODIFIERS = {"graphic", "light", "visual", "illustrated", "picture", "chapter", "comic", "short"}


def _strippable(tokens) -> bool:
    if len(tokens) < 2 or not _is_generic(tokens[-1].lower()):
        return False
    return tokens[-2].lower() not in _COMPOUND_MODIFIERS


def _is_generic(token: str) -> bool:
    if token in GENERIC_SUFFIXES:
        return True
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


def is_bare(name: str) -> bool:
    return norm_key(name) == re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", name.lower().replace("&", " and "))).strip()


def bare_form(name: str) -> str:
    tokens = re.sub(r"[^\w\s'&-]", " ", name).split()
    while len(tokens) > 1 and tokens[0].lower() in _LEADING_ARTICLES:
        tokens.pop(0)
    while len(tokens) > 1 and _strippable([t.lower() for t in tokens]):
        tokens.pop()
    return " ".join(tokens)


def similar(key_a: str, key_b: str) -> bool:
    """Exact key match, or a near miss (typo) between longer names. Never
    merges names whose digits differ ("Book 1" vs "Book 2")."""
    if key_a == key_b:
        return True
    if re.findall(r"\d+", key_a) != re.findall(r"\d+", key_b):
        return False
    if min(len(key_a), len(key_b)) < 8:
        return False
    return difflib.SequenceMatcher(None, key_a, key_b).ratio() >= 0.9


def pick_canonical(variants):
    """Prefer a bare-form name, then the most frequent, then the shortest,
    then alphabetical (deterministic)."""
    counts: dict[str, int] = {}
    for v in variants:
        counts[v] = counts.get(v, 0) + 1
    return min(counts, key=lambda v: (not is_bare(v), -counts[v], len(v), v))


_WORD_NUMBERS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
    "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
}
_WORD_NUM_ALT = "|".join(_WORD_NUMBERS)

# Trailing position markers in stored/raw series strings: "X -- [16]",
# "X -- bk. 6", "X (6)", "X #6", "X, Book 6", "X ; 12", "X -- book two".
_POSITION = r"(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)"
_WORD_POSITION = rf"({_WORD_NUM_ALT})"
_POSITION_PATTERNS = [
    re.compile(r"^(?P<name>.+?)\s*(?:--|;|:|,|\s-)\s*(?:bk\.?|book|vol\.?|volume|no\.?|#|part|pt\.?)?\s*\[?\s*" + _POSITION + r"\.?\s*\]?\s*$", re.I),
    # The whole label bracketed, not just the number: "X -- [bk. 1]".
    re.compile(r"^(?P<name>.+?)\s*(?:--|;|:|,|\s-)\s*\[\s*(?:bk\.?|book|vol\.?|volume|no\.?|#|part|pt\.?)\s*" + _POSITION + r"\s*\]\s*$", re.I),
    re.compile(r"^(?P<name>.+?)\s*\(\s*(?:(?:bk\.?|book|vol\.?|volume|no\.?|#|part|pt\.?)\s*)?" + _POSITION + r"\s*\)\s*$", re.I),
    re.compile(r"^(?P<name>.+?)\s+(?:(?:bk\.?|book|vol\.?|volume|no\.?|part|pt\.?)\s*|#\s*)" + _POSITION + r"\.?\s*$", re.I),
    # Spelled-out word positions ("... -- book two", "... book one") - OpenReads'
    # own normalizer only recognises digits, so "39 Clues: Unstoppable -- book
    # two" survived as its own series instead of joining "39 Clues: Unstoppable".
    re.compile(r"^(?P<name>.+?)\s*(?:--|;|:|,|\s-)?\s*(?:bk\.?|book|vol\.?|volume|no\.?|part|pt\.?)\s+" + _WORD_POSITION + r"\s*$", re.I),
]


def split_position(raw):
    """'X -- [16]' -> ('X', '16'); 'X -- book two' -> ('X', '2'); (raw, None)
    if no clean position can be extracted."""
    if not isinstance(raw, str):
        return raw, None
    raw = raw.strip()
    for pat in _POSITION_PATTERNS:
        m = pat.match(raw)
        if m:
            name = m.group("name").strip(" ,;:-.")
            pos = m.group(2)
            pos = _WORD_NUMBERS.get(pos.lower(), pos)
            if name and not name.isdigit():
                return name, re.sub(r"\s+", "", pos)
    return raw, None


# ---------------------------------------------------------------------------
# Imprint / publisher-line / collection denylist (data, in one place).
#
# Library catalogs and ebook metadata routinely put the PUBLISHER'S LINE in the series
# field ("Knaur 60291", "Mills & Boon Desire 2-in-1 - 322", "Le livre de poche :
# thriller"). Those are shelf names, not reading orders, and their numbers are catalog
# numbers. Three tiers, all case/punctuation-insensitive:
#   PUBLISHER_NAMES    whole-name matches only (the word alone is too common to match
#                      inside a longer title: "Piper", "Ace", "Tor", "Pan").
#   PUBLISHER_WORDS    distinctive imprint words, matched as a whole word anywhere
#                      ("rororo thriller", "Droemer Knaur", "Knaur 60291").
#   PUBLISHER_PATTERNS regexes for lines and collection shapes ("2-in-1", Silhouette
#                      Desire, "Bestselling Author Collection").
# the owner can add more at runtime with the `series.ignored_names` setting
# (see ignored_match) without touching this table.
# ---------------------------------------------------------------------------
PUBLISHER_NAMES = {
    # German-language
    "piper", "fischer", "dtv", "btb", "list", "insel", "diogenes", "reclam", "suhrkamp",
    "ullstein", "luebbe", "lubbe", "bastei", "cora", "mira", "blanvalet", "penhaligon",
    "droemer", "rowohlt", "aufbau", "kiepenheuer", "atb", "tb", "taschenbuch", "romane",
    # French
    "folio", "pocket", "points", "poche", "j ai lu", "jai lu", "gallimard", "albin michel",
    "fleuve noir", "presses de la cite", "livre de poche", "le livre de poche",
    "10 18", "babel", "actes sud", "stock", "grasset", "denoel",
    # English
    "penguin", "penguin books", "penguin classics", "penguin modern classics",
    "penguin random house", "vintage", "vintage classics", "vintage books", "pan", "corgi",
    "sphere", "arrow", "tor", "tor books", "ace", "daw", "del rey", "orbit", "baen",
    "bantam", "dell", "avon", "signet", "zebra", "forge", "pocket books", "harper",
    "harpercollins", "harper collins", "random house", "hodder", "headline", "transworld",
    "gollancz", "mills and boon", "mills boon", "black lace", "oxford worlds classics",
    "everymans library", "wordsworth classics", "dover thrift editions", "modern library",
    "bestselling author collection", "bestselling author", "large print", "audible",
}
PUBLISHER_WORDS = {
    "knaur", "goldmann", "rororo", "rowohlt", "ullstein", "droemer", "btb", "suhrkamp",
    "blanvalet", "penhaligon", "luebbe", "lubbe", "bastei", "harpercollins", "silhouette",
}
PUBLISHER_PATTERNS = [
    ("publisher", re.compile(p, re.I))
    for p in (
        r"\bmills\s*(?:&|and)\s*boon\b",
        r"\bsilhouette\b",
        r"\bharlequin\b",
        r"\bblack\s+lace\b",
        r"\bbest[- ]?sell\w*\s+author\b",
        r"\blivre\s+de\s+poche\b",
        r"\bj'?\s?ai\s+lu\b",
        r"\bfischer\s+(?:taschenbuch|tb|verlag)\b",
        r"\binsel\s+taschenbuch\b",
        r"\bdtv\s+(?:premium|reihe|tb)\b",
        r"\bpiper\s+(?:verlag|taschenbuch|tb)\b",
        r"\blist\s+taschenbuch\b",
        r"^(?:julia|bianca|romana|tiffany)\s+(?:extra|exklusiv|saison|collection|band|nr)\b",
        r"^(?:penguin|vintage|oxford|wordsworth|everyman'?s|dover|modern\s+library|signet|barnes\s*&\s*noble)\b.*\b(?:classics?|library|editions?|books)\b",
        r"^penguin\b",
    )
] + [
    ("collection", re.compile(p, re.I))
    for p in (
        r"\b\d\s*-?\s*in\s*-?\s*\d\b",              # "2-in-1", "3 in 1" omnibus lines
        r"\b(?:author|writer)s?\s+collection\b",
        r"\bcollector'?s?\s+library\b",
    )
]


def _plain(name: str) -> str:
    """Lowercase, accents and punctuation folded, for denylist comparison."""
    text = (name or "").lower().replace("&", " and ")
    text = (text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
            .replace("é", "e").replace("è", "e").replace("ê", "e").replace("à", "a").replace("ç", "c")
            .replace("î", "i").replace("ô", "o").replace("û", "u").replace("'", "").replace("’", ""))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def publisher_reason(name: str) -> str | None:
    """'publisher' / 'collection' when the name is an imprint, publisher line or
    collection label from the tables above; None for an ordinary series name."""
    if not name:
        return None
    plain = _plain(name)
    plain_nodigits = re.sub(r"\s*\d[\d ]*$", "", plain).strip()  # "knaur 60291" -> "knaur"
    if plain in PUBLISHER_NAMES or plain_nodigits in PUBLISHER_NAMES:
        return "publisher"
    if set(plain.split()) & PUBLISHER_WORDS:
        return "publisher"
    for reason, pat in PUBLISHER_PATTERNS:
        if pat.search(name) or pat.search(plain):
            return reason
    return None


def ignored_match(name: str, ignored) -> bool:
    """True when `name` equals (norm_key) or, for an entry ending in '*', starts with
    an entry of the user's `series.ignored_names` setting."""
    if not ignored:
        return False
    key = norm_key(name)
    for entry in ignored:
        if not isinstance(entry, str) or not entry.strip():
            continue
        e = entry.strip()
        if e.endswith("*"):
            prefix = norm_key(e[:-1])
            if prefix and key.startswith(prefix):
                return True
        elif norm_key(e) == key or _plain(e) == _plain(name):
            return True
    return False


def classify_name(name: str, ignored=None) -> str | None:
    """Why `name` is NOT a series (None when it is one): 'ignored' (user list),
    'publisher'/'collection' (denylist) or 'junk' (catalog/shelfmark noise)."""
    if ignored_match(name, ignored):
        return "ignored"
    reason = publisher_reason(name)
    if reason:
        return reason
    return "junk" if is_junk(name) else None


# ---------------------------------------------------------------------------
# Position sanity
# ---------------------------------------------------------------------------
DEFAULT_MAX_POSITION = 200
HARD_MAX_POSITION = 999  # no series has a thousand books; guards a mistyped setting


def position_numbers(pos) -> list[float]:
    """Every number in a position string ('3' -> [3.0], '1-3' -> [1.0, 3.0])."""
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(pos or ""))]


def position_problem(pos, max_position: int | None = None) -> str | None:
    """None when `pos` looks like a place in a reading order; otherwise why not:
    'empty', 'year_like', 'isbn_like', 'too_many_digits', 'above_max', 'negative'.
    Publisher catalog numbers (Knaur 60291, Silhouette 1920) fail here."""
    text = str(pos).strip() if pos is not None else ""
    if not text:
        return "empty"
    if text.startswith("-"):
        return "negative"
    digits = re.findall(r"\d+", text)
    if not digits:
        return "empty"
    cap = min(max(int(max_position or DEFAULT_MAX_POSITION), 1), HARD_MAX_POSITION)
    for d in digits:
        if len(d) >= 9:
            return "isbn_like"
        n = int(d)
        if 1900 <= n <= 2100:
            return "year_like"
        if len(d.lstrip("0")) >= 4:
            return "too_many_digits"
        if n > cap:
            return "above_max"
    return None


def sane_position_value(pos, max_position: int | None = None) -> float | None:
    """The leading number of a sane position, else None. Used by gap counting."""
    if position_problem(pos, max_position):
        return None
    nums = position_numbers(pos)
    return nums[0] if nums else None


def normalize_position(text: str) -> str:
    """'03' -> '3', '2.50' -> '2.5', '1 - 3' -> '1-3', '2,5' -> '2.5'."""
    parts = [p.strip().replace(",", ".") for p in re.split(r"\s*-\s*", text.strip()) if p.strip()]
    out = []
    for p in parts:
        if "." in p:
            p = p.rstrip("0").rstrip(".") or "0"
        p = p.lstrip("0") or "0" if p.isdigit() else p
        if re.match(r"^0\d", p):
            p = p.lstrip("0") or "0"
        out.append(p)
    return "-".join(out)


# ---------------------------------------------------------------------------
# Parsing a raw series string into candidates
# ---------------------------------------------------------------------------
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def roman_to_int(text: str) -> int | None:
    """Value of a canonical roman numeral up to 50, else None ('IIII', 'mix' ...)."""
    t = text.lower()
    if not t or any(ch not in _ROMAN_VALUES for ch in t):
        return None
    total = 0
    for i, ch in enumerate(t):
        v = _ROMAN_VALUES[ch]
        total += -v if i + 1 < len(t) and _ROMAN_VALUES[t[i + 1]] > v else v
    if not 1 <= total <= 50:
        return None
    canon = ""
    n = total
    for val, sym in ((50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= val:
            canon += sym
            n -= val
    return total if canon == t else None


_MORE_WORD_NUMBERS = {
    "ein": "1", "eins": "1", "erste": "1", "zwei": "2", "drei": "3", "vier": "4",
    "fuenf": "5", "fünf": "5", "sechs": "6", "sieben": "7", "acht": "8", "neun": "9", "zehn": "10",
    "un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4", "cinq": "5",
    "six": "6", "sept": "7", "huit": "8", "neuf": "9", "dix": "10",
}
_ALL_WORDS = {**_WORD_NUMBERS, **_MORE_WORD_NUMBERS}
_ALL_WORD_ALT = "|".join(sorted(_ALL_WORDS, key=len, reverse=True))

_KW = r"(?:bk\.?|books?|vol\.?|volume|no\.?|nr\.?|number|part|pt\.?|band|bd\.?|teil|tome|livre|libro|deel|buch|folge|fall|n°)"
_NUM = r"\d+(?:[.,]\d+)?"
_RANGE = rf"{_NUM}(?:\s*-\s*{_NUM})?"
_OF = r"(?:\s*(?:of|von|sur|/)\s*\d+)?"
_SEP = r"(?:\s*--\s*|\s*[;,:]\s*|\s+[-–—]\s+)"

_RX_PAREN_NUM = re.compile(rf"^(?P<name>.+?)\s*[(\[]\s*(?:{_KW}\s*|#\s*)?(?P<pos>{_RANGE})\.?{_OF}\s*[)\]]\s*$", re.I)
# "Foo (Bar #3)" / "Foo (Bar, Book 3)": the position sits inside the trailing parenthesis.
_RX_PAREN_INNER = re.compile(rf"^(?P<outer>.+?)\s*\((?P<inner>[^()]*?[A-Za-z][^()]*?)(?:,\s*|\s+)(?:{_KW}\s*|#\s*)(?P<pos>{_RANGE})\)\s*$", re.I)
_RX_PAREN_WORD = re.compile(rf"^(?P<name>.+?)\s*[(\[]\s*{_KW}\s*(?P<pos>[a-zA-Zäöüé]+)\s*[)\]]\s*$", re.I)
_RX_SEP_NUM = re.compile(rf"^(?P<name>.+?){_SEP}(?:{_KW}\s*|#\s*)?\[?\s*(?P<pos>{_RANGE})\.?\s*\]?{_OF}\s*$", re.I)
_RX_KW_NUM = re.compile(rf"^(?P<name>.+?)(?:\s+{_KW}\s*|\s*#\s*)(?P<pos>{_RANGE})\.?{_OF}\s*$", re.I)
_RX_KW_WORD = re.compile(rf"^(?P<name>.+?){_SEP}?\s*{_KW}\s*(?P<pos>[a-zA-Zäöüé]+)\s*$", re.I)
_RX_N_OF_M = re.compile(rf"^(?P<name>.+?)\s+(?P<pos>{_NUM})\s+(?:of|von|sur)\s+\d+\s*$", re.I)
_RX_PURE = re.compile(rf"^(?:{_KW}\s*|#\s*)?(?P<pos>{_RANGE})\.?{_OF}$", re.I)
_RX_PURE_WORD = re.compile(rf"^{_KW}\s*(?P<pos>[a-zA-Zäöüé]+)$", re.I)


def _word_position(token: str) -> str | None:
    """Spelled-out or roman number token -> digit string (keyword-prefixed use only)."""
    t = token.lower()
    if t in _ALL_WORDS:
        return _ALL_WORDS[t]
    r = roman_to_int(t)
    return str(r) if r else None


def _balance_brackets(text: str) -> str:
    """Drop unmatched ')' ']' and dangling '(' '[' ('[Series Name' -> 'Series Name',
    'Cocktail #3)' -> 'Cocktail #3'). A dangling '(' right before a number marker
    ('Name (3') becomes a comma so the number still reads as a position."""
    closers = {")": "(", "]": "["}
    stack: list[tuple[str, int]] = []
    drop: set[int] = set()
    for i, ch in enumerate(text):
        if ch in "([":
            stack.append((ch, i))
        elif ch in ")]":
            if stack and stack[-1][0] == closers[ch]:
                stack.pop()
            else:
                drop.add(i)
    drop.update(i for _, i in stack)
    if not drop:
        return text
    out = []
    for i, ch in enumerate(text):
        if i not in drop:
            out.append(ch)
        elif ch == "(" and re.match(rf"\s*(?:{_KW}\s*|#\s*)?\d", text[i + 1:], re.I) and i > 0:
            out.append(",")
    return "".join(out)


def _split_top(text: str, seps: str, *, keep_digit_commas: bool = False) -> list[str]:
    """Split on any char of `seps` that is outside brackets. A comma between two digits
    ('2,5') is a decimal comma, not a separator."""
    parts, depth, cur = [], 0, []
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(depth - 1, 0)
        if ch in seps and depth == 0:
            if ch == "," and 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
                cur.append(ch)
                continue
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


_QUOTE_CHARS = "\"'“”„‚‘’«»‹›`´"
_QUOTE_PAIRS = {"'": "'’", "‘": "’'", "\"": "\"”“", "“": "”\"", "„": "“”\"", "«": "»", "‹": "›", "`": "`´"}
_EDGE_PUNCT = " \t\r\n,;:-–—./|\\_*~"


def _wrapped(text: str, open_ch: str, close_ch: str) -> bool:
    """True when the first char opens a bracket that only closes on the last char."""
    if len(text) < 2 or text[0] != open_ch or text[-1] != close_ch:
        return False
    depth = 0
    for i, ch in enumerate(text):
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0 and i != len(text) - 1:
                return False
    return depth == 0


def _clean_name(name: str) -> str:
    """Trim stray quotes/punctuation, unwrap '[Name]' and quote pairs."""
    text = re.sub(r"\s+", " ", name or "").strip()
    for _ in range(8):
        before = text
        text = text.strip(_EDGE_PUNCT)
        if len(text) > 1 and _QUOTE_PAIRS.get(text[0]) and text[-1] in _QUOTE_PAIRS[text[0]]:
            text = text[1:-1].strip()
        text = text.lstrip(_QUOTE_CHARS).rstrip("\"“”„«»‹›`´")
        if _wrapped(text, "[", "]"):
            text = text[1:-1]
        text = text.strip()
        if text == before:
            break
    return text


def _extract_position(piece: str):
    """(name, normalized position, raw position text) from a piece ending in a number
    marker, or None. Roman numerals and spelled-out numbers need a keyword (Book IV)."""
    for rx in (_RX_PAREN_NUM, _RX_SEP_NUM, _RX_KW_NUM, _RX_N_OF_M):
        m = rx.match(piece)
        if m and _clean_name(m.group("name")) and not _clean_name(m.group("name")).isdigit():
            raw = m.group("pos")
            return m.group("name"), normalize_position(raw), raw
    m = _RX_PAREN_INNER.match(piece)
    if m and _clean_name(m.group("outer")):
        return f"{m.group('outer').strip()} ({m.group('inner').strip()})", normalize_position(m.group("pos")), m.group("pos")
    for rx in (_RX_PAREN_WORD, _RX_KW_WORD):
        m = rx.match(piece)
        if m:
            val = _word_position(m.group("pos"))
            if val and _clean_name(m.group("name")):
                return m.group("name"), val, m.group("pos")
    return None


def _pure_marker(piece: str):
    """(normalized position, raw) when the whole piece is just a position ('8', '#3',
    'Book 3', '3 of 10', 'Vol. IV'); else None."""
    m = _RX_PURE.match(piece.strip())
    if m:
        return normalize_position(m.group("pos")), m.group("pos")
    m = _RX_PURE_WORD.match(piece.strip())
    if m:
        val = _word_position(m.group("pos"))
        if val:
            return val, m.group("pos")
    return None


@dataclass(frozen=True)
class SeriesCandidate:
    """One (name, position) read from a raw series string.

    `position` is only set when it is a sane series index; `raw_position` keeps what the
    string said (for audit) and `position_problem` says why it was dropped. `ignored` is
    None for a real series, else the reason the name is not one (see classify_name)."""
    raw: str
    name: str
    position: str | None = None
    raw_position: str | None = None
    position_problem: str | None = None
    ignored: str | None = None


@dataclass(frozen=True)
class SeriesOptions:
    ignored: tuple = ()
    max_position: int = DEFAULT_MAX_POSITION


def load_options(conn=None) -> SeriesOptions:
    """series.ignored_names and series.max_position from the settings table. `conn` lets a
    caller already inside a transaction read through it."""
    keys = ("series.ignored_names", "series.max_position")
    values: dict[str, Any] = {}
    try:
        sql = "SELECT key,value_json FROM settings WHERE key IN (?,?)"
        if conn is not None:
            rows = conn.execute(sql, keys).fetchall()
        else:
            from .. import db
            rows = db.all(sql, keys)
        for r in rows:
            if r["value_json"] is not None:
                values[r["key"]] = json.loads(r["value_json"])
    except Exception:
        values = {}
    ignored, cap = values.get(keys[0]), values.get(keys[1])
    names = tuple(str(x).strip() for x in ignored if isinstance(x, str) and x.strip()) if isinstance(ignored, list) else ()
    try:
        cap = min(max(int(cap), 1), HARD_MAX_POSITION) if cap is not None else DEFAULT_MAX_POSITION
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_POSITION
    return SeriesOptions(names, cap)


def _group_pieces(part: str) -> list[tuple[str, tuple | None]]:
    """Comma-split one ';' part into (name, marker) groups. A comma only separates two
    series when the piece after it stands alone (own number marker, or a known
    publisher line); otherwise it belongs to the name ('Mistborn, Era 2: Wax & Wayne')."""
    groups: list[tuple[str, tuple | None]] = []
    buffer: list[str] = []
    for piece in (p.strip() for p in _split_top(part, ",")):
        if not piece:
            continue
        pure = _pure_marker(piece)
        if pure is not None:
            if buffer:
                groups.append((", ".join(buffer), (pure[0], pure[1])))
                buffer = []
            elif groups and groups[-1][1] is None:
                groups[-1] = (groups[-1][0], (pure[0], pure[1]))
            else:
                buffer.append(piece)
            continue
        ext = _extract_position(piece)
        if ext:
            groups.append((", ".join(buffer + [ext[0]]), (ext[1], ext[2])))
            buffer = []
            continue
        if publisher_reason(piece):
            if buffer:
                groups.append((", ".join(buffer), None))
                buffer = []
            groups.append((piece, None))
            continue
        buffer.append(piece)
    if buffer:
        groups.append((", ".join(buffer), None))
    return groups


def parse_series(raw, *, ignored=None, max_position: int | None = None) -> list[SeriesCandidate]:
    """Split a raw series string into candidate (name, position) pairs.

    ';' always separates; a comma separates only when each side stands alone (see
    _group_pieces). Each candidate is cleaned (brackets balanced, quotes and edge
    punctuation trimmed), its position read and sanity-checked, and its name checked
    against the imprint/collection denylist and `ignored`. Never raises; never returns
    an empty name; returns [] for text with nothing nameable in it."""
    try:
        return _parse_series(raw, ignored, max_position)
    except Exception:  # pragma: no cover - defensive: a parser must not break an import
        name = _clean_name(raw) if isinstance(raw, str) else ""
        return [SeriesCandidate(raw=str(raw), name=name)] if name else []


def _parse_series(raw, ignored, max_position) -> list[SeriesCandidate]:
    if not isinstance(raw, str):
        return []
    text = unicodedata.normalize("NFKC", raw).replace("\x00", " ")
    text = re.sub(r"\s+[–—]\s+", " - ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _balance_brackets(text)
    out: list[SeriesCandidate] = []
    seen: dict[str, int] = {}
    for part in _split_top(text, ";"):
        part = part.strip()
        if not part:
            continue
        for name, marker in _group_pieces(part):
            fragment = name
            truncated = fragment.rstrip().endswith(("...", "…"))
            name = _clean_name(name)
            if truncated and len(name) < 3:
                continue
            if not name:
                continue
            pos = raw_pos = problem = None
            if marker:
                raw_pos = marker[1]
                problem = position_problem(marker[0], max_position)
                pos = None if problem else marker[0]
            reason = classify_name(name, ignored)
            key = norm_key(name) or name.lower()
            if key in seen:
                prev = out[seen[key]]
                if prev.position is None and pos is not None and prev.ignored is None:
                    out[seen[key]] = SeriesCandidate(prev.raw, prev.name, pos, raw_pos, problem, prev.ignored)
                continue
            seen[key] = len(out)
            out.append(SeriesCandidate(fragment.strip(), name, pos, raw_pos, problem, reason))
    return out
