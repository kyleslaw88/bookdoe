from __future__ import annotations

import re
import unicodedata

# Curly/typographic quote and apostrophe variants folded down to their ASCII form.
_QUOTE_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "`": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
}
_QUOTE_RE = re.compile("|".join(re.escape(c) for c in _QUOTE_MAP))

_AMP_ENTITY_RE = re.compile(r"&amp;", re.IGNORECASE)
_AMP_RE = re.compile(r"\s*&\s*")

# Square-bracket tags (release groups, format tags) are noise regardless of content.
_BRACKET_NOISE_RE = re.compile(r"\[[^\]]*\]")
_NOISE_TAGS = (
    r"(?:19|20)\d{2}",  # year, e.g. (2024)
    r"blu-?ray", r"web-?rip", r"web-?dl", r"dvdrip", r"brrip",
    r"\d{3,4}p",  # resolution, e.g. 1080p
    r"x264", r"x265", r"hevc",
    r"epub", r"mobi", r"pdf", r"azw3?", r"mp3", r"flac",
    r"unabridged", r"abridged", r"retail", r"remaster(?:ed)?", r"repack", r"proper",
)
_PAREN_NOISE_RE = re.compile(r"\((?:" + "|".join(_NOISE_TAGS) + r")\)", re.IGNORECASE)

# Two or more single-letter initials, e.g. "S.C." or "J.R.R." -> expanded to bare letters.
_INITIALS_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}")

# Validating roman-numeral pattern (not just a character class) to cut down on false
# positives against ordinary words that happen to be spelled from IVXLCDM letters.
_ROMAN_WORD = r"M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
_VOLUME_RE = re.compile(
    # _ROMAN_WORD's pieces are all individually optional, so it can match a zero-length
    # string -- the lookahead forces it to consume at least one roman-numeral letter.
    r"\b(?:vol(?:ume)?|bk|book|no)\.?\s*(?:\d+|(?=[ivxlcdm])" + _ROMAN_WORD + r")\b",
    re.IGNORECASE,
)

_POSSESSIVE_RE = re.compile(r"'s\b")

# Colon, spaced dash, or pipe -- the conventional title/subtitle boundary markers.
_SUBTITLE_DELIM_RE = re.compile(r"\s*:\s*|\s+-\s+|\s*\|\s*")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_quotes(text: str) -> str:
    return _QUOTE_RE.sub(lambda m: _QUOTE_MAP[m.group(0)], text)


def _expand_initials(match: re.Match[str]) -> str:
    letters = re.findall(r"[A-Za-z]", match.group(0))
    return " ".join(letters)


def _strip_release_noise(text: str) -> str:
    text = _BRACKET_NOISE_RE.sub(" ", text)
    text = _PAREN_NOISE_RE.sub(" ", text)
    return text


def normalize_query(query: str) -> str:
    """Normalize a raw search query for matching against catalog text.

    Pure and idempotent: normalize_query(normalize_query(q)) == normalize_query(q).
    """
    if not query:
        return ""
    q = unicodedata.normalize("NFC", query)
    q = _normalize_quotes(q)
    q = _strip_release_noise(q)
    q = _AMP_ENTITY_RE.sub(" and ", q)
    q = _AMP_RE.sub(" and ", q)
    q = _INITIALS_RE.sub(_expand_initials, q)
    q = _VOLUME_RE.sub(" ", q)
    q = _POSSESSIVE_RE.sub("s", q)
    q = _SUBTITLE_DELIM_RE.sub(" ", q)
    q = _WHITESPACE_RE.sub(" ", q)
    return q.strip()


def split_title_subtitle(query: str) -> tuple[str, str | None]:
    """Split a raw query into (title, subtitle) on the first colon/dash/pipe delimiter.

    Returns (query, None) when there is no delimiter that yields a non-empty title
    and subtitle on both sides.
    """
    if not query:
        return "", None
    q = _normalize_quotes(unicodedata.normalize("NFC", query)).strip()
    match = _SUBTITLE_DELIM_RE.search(q)
    if not match:
        return q, None
    title = q[: match.start()].strip()
    subtitle = q[match.end():].strip()
    if not title or not subtitle:
        return q, None
    return title, subtitle
