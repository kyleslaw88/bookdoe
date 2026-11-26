"""Structured path evidence extraction for inspect_file (Part XVIII).

Parses a file path into separate evidence signals: author, title, series,
series_position, year, narrator, and identifiers (ASIN, ISBN).  Each field
carries a source tag ('folder' or 'filename') so downstream matching can
weight them appropriately (Part XIX).

Pure function — no filesystem access beyond the path string.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .seriesnorm import split_position

# ---------------------------------------------------------------------------
# ASIN pattern: exactly 10-char Amazon identifier starting with B0
# ---------------------------------------------------------------------------
_ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")

# ISBN-10 and ISBN-13 patterns (digits only, possibly with hyphens)
_ISBN13_RE = re.compile(r"\b(97[89]\d{10})\b")
_ISBN10_RE = re.compile(r"\b(\d{9}[\dXx])\b")

# ---------------------------------------------------------------------------
# Narrator pattern: "Narrated by X" or "Narrator: X"
# Must not match pure numeric strings (ISBNs, etc.)
# ---------------------------------------------------------------------------
_NARRATOR_RE = re.compile(
    r"(?:narrated\s+by|narrator[:\s]+)(.+?)(?:\s*[-\u2013\u2014,;)]|\)|\s*$)", re.I
)

# ---------------------------------------------------------------------------
# Year pattern: standalone 4-digit year (1900-2099)
# ---------------------------------------------------------------------------
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# ---------------------------------------------------------------------------
# Series position patterns in folder/file names
# ---------------------------------------------------------------------------
# "NN - Title" or "#NN - Title" or "Book NN - Title"
_POS_TITLE_RE = re.compile(
    r"^(?:book\s+)?(?:(?:#|no\.?|number)\s*)?(\d+(?:\.\d+)?)\s*[-\u2013\u2014]\s*(.+)$",
    re.I,
)

# "Title (NN)" or "Title (Book NN)" - but NOT years like "Title (2023)"
_TITLE_POS_RE = re.compile(
    r"^(.+?)\s*\((?:book\s*)?(\d+(?:\.\d+)?)\)\s*$", re.I
)
# Year-only parenthesized pattern to exclude
_YEAR_PAREN_RE = re.compile(r"^\(?(19\d{2}|20\d{2})\)?$")

# ---------------------------------------------------------------------------
# Known filler / release-cruft tokens to strip from title text
# ---------------------------------------------------------------------------
# NOTE: We do NOT strip bare years (19xx/20xx) here — they may be the title.
# They are stripped only when they appear as standalone parenthesized values
# or in known filler contexts.
_FILLER_RE = re.compile(
    r"(?i)\b("
    r"unabridged|abridged|webrip|webdl|audiobook|ebook|mp3|epub|mobi|"
    r"azw3|m4b|flac|ogg|opus|aac|part\s*\d+|disc\s*\d+|cd\s*\d+|"
    r"\d+\s*kbps|\d+\s*kb/s|\d+\s*kbit"
    r")\b",
)

# ---------------------------------------------------------------------------
# Common folder names that are NOT author names
# ---------------------------------------------------------------------------
_NON_AUTHOR_FOLDERS = {
    "books", "audiobooks", "library", "media", "ebooks",
    "downloads", "import", "temp", "tmp", "new", "unsorted",
}

# ---------------------------------------------------------------------------
# ASIN-only folder names (folders that contain only an ASIN)
# ---------------------------------------------------------------------------
_ASIN_ONLY_RE = re.compile(r"^B0[A-Z0-9]{8}$")


# ---------------------------------------------------------------------------
# ISBN checksum helpers
# ---------------------------------------------------------------------------

def _isbn10_check_digit(digit_str: str) -> bool:
    """Return True if the ISBN-10 string has a valid check digit."""
    if len(digit_str) != 10:
        return False
    total = 0
    for i, ch in enumerate(digit_str[:9]):
        if not ch.isdigit():
            return False
        total += int(ch) * (10 - i)
    last = digit_str[9].upper()  # Normalize X/x
    if last == "X":
        total += 10
    elif last.isdigit():
        total += int(last)
    else:
        return False
    return total % 11 == 0


def _isbn13_check_digit(digit_str: str) -> bool:
    """Return True if the ISBN-13 string has a valid check digit."""
    if len(digit_str) != 13:
        return False
    if not digit_str.isdigit():
        return False
    total = 0
    for i, ch in enumerate(digit_str[:12]):
        total += int(ch) * (1 if i % 2 == 0 else 3)
    check = (10 - (total % 10)) % 10
    return check == int(digit_str[12])


def _extract_isbn_from_text(
    text: str,
    isbn13_re: re.Pattern,
    isbn10_re: re.Pattern,
) -> tuple[str | None, str | None]:
    """Extract and validate ISBN-13 / ISBN-10 from text.

    Returns (isbn13_value | None, isbn10_value | None).
    Only checksum-valid ISBNs are returned.
    """
    isbn13_match = isbn13_re.search(text)
    if isbn13_match:
        candidate = isbn13_match.group(1)
        if _isbn13_check_digit(candidate):
            return candidate, None

    isbn10_match = isbn10_re.search(text)
    if isbn10_match:
        candidate = isbn10_match.group(1)
        if _isbn10_check_digit(candidate):
            return None, candidate.upper()
    return None, None


# ---------------------------------------------------------------------------
# Identifier extraction helpers
# ---------------------------------------------------------------------------

def _extract_asin_from_path(path_str: str) -> tuple[str | None, str | None]:
    """Extract ASIN from a path string, returning (value, source).

    source is 'folder' if found in a directory component, 'filename' if in
    the file stem, or None if not found.
    """
    # Split into directory components and the filename
    p = Path(path_str)
    dir_parts = list(p.parts[:-1])  # all directory components
    file_stem = p.stem

    # Check directory components first
    for part in dir_parts:
        m = _ASIN_RE.search(part)
        if m:
            return m.group(1), "folder"

    # Check filename stem
    m = _ASIN_RE.search(file_stem)
    if m:
        return m.group(1), "filename"

    return None, None


def _extract_isbns_from_path(path_str: str) -> tuple[dict[str, str], dict[str, str]]:
    """Extract and validate ISBNs from a path string.

    Returns (identifiers dict, source dict).
    Only checksum-valid ISBNs are included.
    """
    identifiers: dict[str, str] = {}
    sources: dict[str, str] = {}

    p = Path(path_str)
    dir_parts = list(p.parts[:-1])
    file_stem = p.stem

    # Check directory components first
    for part in dir_parts:
        i13, i10 = _extract_isbn_from_text(part, _ISBN13_RE, _ISBN10_RE)
        if i13:
            identifiers["isbn13"] = i13
            sources["isbn13"] = "folder"
        elif i10:
            identifiers["isbn10"] = i10
            sources["isbn10"] = "folder"

    # Check filename stem
    if "isbn13" not in identifiers:
        i13, i10 = _extract_isbn_from_text(file_stem, _ISBN13_RE, _ISBN10_RE)
        if i13:
            identifiers["isbn13"] = i13
            sources["isbn13"] = "filename"
        elif i10:
            identifiers["isbn10"] = i10
            sources["isbn10"] = "filename"

    return identifiers, sources


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_path_evidence(
    path: str | Path,
    root_path: str | Path | None = None,
) -> dict[str, Any]:
    """Parse a file path into structured evidence signals.

    Parameters
    ----------
    path:
        Full path to the media file.
    root_path:
        Root folder used for scanning.  If given, depth relative to root
        determines whether a parent folder is an author or just a container.

    Returns
    -------
    dict with keys:
        author (str | None), title (str), series (str | None),
        series_position (str | None), year (str | None),
        narrator (str | None),
        identifiers (dict[str, str]),
        tokens (list[str]),
        source (dict[str, str]): where each field was detected.
    """
    p = Path(path)
    root = Path(root_path) if root_path else None

    stem = p.stem
    filename = p.name
    parent = p.parent  # folder containing the file
    grandparent = parent.parent if parent != parent.parent else None

    # Start with empty evidence
    author: str | None = None
    title: str | None = None
    series: str | None = None
    series_position: str | None = None
    year: str | None = None
    narrator: str | None = None
    identifiers: dict[str, str] = {}
    tokens: list[str] = []
    source: dict[str, str] = {}
    title_from_parent: bool = False

    # ------------------------------------------------------------------
    # 1. Extract identifiers from the RELATIVE path (not the full absolute path)
    # ------------------------------------------------------------------
    if root:
        try:
            rel = p.relative_to(root)
            rel_str = str(rel)
        except ValueError:
            rel_str = str(p)
    else:
        rel_str = str(p)

    # ASIN with source tracking
    asin_val, asin_source = _extract_asin_from_path(rel_str)
    if asin_val:
        identifiers["asin"] = asin_val
        source["asin"] = asin_source

    # ISBNs with checksum validation and source tracking
    isbn_ids, isbn_sources = _extract_isbns_from_path(rel_str)
    identifiers.update(isbn_ids)
    source.update(isbn_sources)

    # ------------------------------------------------------------------
    # 2. Determine depth relative to root
    # ------------------------------------------------------------------
    if root:
        try:
            rel = p.relative_to(root)
            depth = len(rel.parts) - 1  # -1 for the filename itself
        except ValueError:
            depth = len(p.parts) - 1
    else:
        # Without a root_path the file is treated as root-level.
        depth = 0

    # ------------------------------------------------------------------
    # 3. Collect folder parts relative to root
    # ------------------------------------------------------------------
    if root:
        try:
            rel_parent = Path(rel_str).parent
            folder_parts = list(rel_parent.parts) if rel_parent != Path(".") else []
        except ValueError:
            folder_parts = list(Path(rel_str).parent.parts)
    else:
        # Without a root_path we cannot distinguish an author folder from
        # a generic container (e.g. /mnt/Dune.epub vs /media/Dune.epub).
        # Treat the file as root-level: no author is guessed from the
        # top-level directory name.
        folder_parts = []

    # ------------------------------------------------------------------
    # 4. Extract narrator from filename
    # ------------------------------------------------------------------
    narrator_match = _NARRATOR_RE.search(stem)
    if narrator_match:
        narrator = narrator_match.group(1).strip()
        source["narrator"] = "filename"

    # Also check parent folder name for narrator
    if not narrator and parent.name:
        narrator_match = _NARRATOR_RE.search(parent.name)
        if narrator_match:
            narrator = narrator_match.group(1).strip()
            source["narrator"] = "folder"

    # Also check grandparent folder name for narrator
    if not narrator and grandparent and grandparent.name:
        narrator_match = _NARRATOR_RE.search(grandparent.name)
        if narrator_match:
            narrator = narrator_match.group(1).strip()
            source["narrator"] = "folder"

    # ------------------------------------------------------------------
    # 5. Extract year from filename or parent folder
    # ------------------------------------------------------------------
    # Only extract year if it's in a parenthesized context or clearly a year field,
    # not when the entire stem is a year-like number (e.g., "1984" as a title).
    year_match = _YEAR_RE.search(stem)
    if year_match:
        # Check if the stem is ONLY a year (possibly with extension)
        stem_no_ext = stem.rsplit('.', 1)[0] if '.' in stem else stem
        if re.match(r"^(19|20)\d{2}$", stem_no_ext):
            # The stem is just a year — don't extract it as a year field
            year = None
        else:
            year = year_match.group(1)
            source["year"] = "filename"
    else:
        year_match = _YEAR_RE.search(parent.name)
        if year_match:
            year = year_match.group(1)
            source["year"] = "folder"

    # ------------------------------------------------------------------
    # 6. Determine title and author from path structure
    # ------------------------------------------------------------------

    # Check if this is a part/disc file (title comes from parent folder)
    # Only match if the stem is JUST the part indicator (e.g., "part 1", "disc 2"),
    # not if it's a full title with a part suffix (e.g., "Author - Title - Part 01")
    is_part_file = bool(re.search(r"(?i)^(part|disc|cd)[ ._-]*\d+$", stem))

    if is_part_file and parent.name:
        # Part file: title is the parent folder name
        title = _extract_title_from_stem(parent.name)
        source["title"] = "folder"
        # Author is grandparent if depth >= 2 and it's not an ASIN-only folder
        if depth >= 2 and grandparent and grandparent.name:
            gp_name = grandparent.name
            if gp_name not in _NON_AUTHOR_FOLDERS and not _ASIN_ONLY_RE.match(gp_name):
                author = gp_name
                source["author"] = "folder"
    else:
        # Normal file: try to extract title from filename stem
        title = _extract_title_from_stem(stem)
        source["title"] = "filename"

        # If the stem is a generic filename (no meaningful title),
        # fall back to the parent folder name as the title
        title_from_parent = False
        _GENERIC_STEMS = {
            "file", "book", "ebook", "audiobook",
            "cover", "sample", "preview", "temp",
            "readme", "license", "read me",
        }
        if not title or title.lower() in _GENERIC_STEMS or re.match(r"^\.[a-z]+$", stem):
            if parent.name:
                parent_stem = parent.name
                # If the parent folder is a known generic container name,
                # don't use it as a title either — return empty.
                if parent_stem.lower() in _NON_AUTHOR_FOLDERS:
                    title = ""
                    source["title"] = "filename"
                else:
                    # Extract series/position from parent folder name first
                    parent_series, parent_pos = _extract_series_from_stem(parent_stem, None)
                    if parent_series and parent_pos:
                        # Both series and position found - check if series name
                        # is the same as what would be the title (not a real series)
                        clean_title = _extract_title_from_stem(parent_stem)
                        clean_title = re.sub(r"\bB0[A-Z0-9]{8}\b", "", clean_title).strip()
                        clean_title = re.sub(r"\s+", " ", clean_title).strip()
                        if parent_series.lower() != clean_title.lower():
                            series = parent_series
                            source["series"] = "folder"
                        # Set position if found
                        try:
                            series_position = str(int(parent_pos))
                        except ValueError:
                            series_position = parent_pos
                        source["series_position"] = "folder"
                        # The title from the parent folder is what remains after
                        # stripping the series/position prefix
                        title = clean_title
                        source["title"] = "folder"
                        title_from_parent = True
                    elif parent_pos:
                        # Parent has a position prefix but no series name
                        try:
                            series_position = str(int(parent_pos))
                        except ValueError:
                            series_position = parent_pos
                        source["series_position"] = "folder"
                        # Extract clean title from parent folder name
                        title = _extract_title_from_stem(parent_stem)
                        title = re.sub(r"\bB0[A-Z0-9]{8}\b", "", title).strip()
                        title = re.sub(r"\s+", " ", title).strip()
                        source["title"] = "folder"
                        title_from_parent = True

                        # Check grandparent for series (Calibre-style: Author/Series/NN-Title/file)
                        if not series and grandparent and grandparent.name:
                            gp_name = grandparent.name
                            if not _ASIN_ONLY_RE.match(gp_name):
                                gp_series, gp_pos = _extract_series_from_stem(gp_name, title)
                                if gp_series:
                                    series = gp_series
                                    series_position = gp_pos
                                    source["series"] = "folder"
                                    if series_position:
                                        source["series_position"] = "folder"
                                else:
                                    # Grandparent might be the series name directly
                                    author_candidate = folder_parts[0] if folder_parts else None
                                    is_narrator_folder = bool(
                                        _NARRATOR_RE.search(gp_name)
                                    )
                                    if (gp_name and gp_name not in _NON_AUTHOR_FOLDERS and
                                            gp_name != author_candidate and
                                            not is_narrator_folder):
                                        series = gp_name
                                        source["series"] = "folder"
                    else:
                        # No series/position in parent — use it as title
                        title = _extract_title_from_stem(parent_stem)
                        title = re.sub(r"\bB0[A-Z0-9]{8}\b", "", title).strip()
                        title = re.sub(r"\s+", " ", title).strip()
                        source["title"] = "folder"
                        title_from_parent = True

                        # If no series found in immediate parent, check grandparent
                        if not series and grandparent and grandparent.name:
                            gp_name = grandparent.name
                            # Don't use grandparent as series if it's an ASIN-only folder
                            if not _ASIN_ONLY_RE.match(gp_name):
                                gp_series, gp_pos = _extract_series_from_stem(gp_name, title)
                                if gp_series:
                                    series = gp_series
                                    series_position = gp_pos
                                    source["series"] = "folder"
                                    if series_position:
                                        source["series_position"] = "folder"
                                elif not parent_series:
                                    # Parent had a position but no series name (e.g., "03 - Title"),
                                    # so grandparent is likely the series (Calibre-style).
                                    author_candidate = folder_parts[0] if folder_parts else None
                                    is_narrator_folder = bool(
                                        _NARRATOR_RE.search(gp_name)
                                    )
                                    if (gp_name and gp_name not in _NON_AUTHOR_FOLDERS and
                                            gp_name != author_candidate and
                                            not is_narrator_folder):
                                        series = gp_name
                                        source["series"] = "folder"

        # Check for series info in the filename (only if title came from filename)
        if not title_from_parent:
            series, series_position = _extract_series_from_stem(stem, title)
            if series:
                source["series"] = "filename"
                if series_position:
                    source["series_position"] = "filename"

    # ------------------------------------------------------------------
    # 7. Handle '{Series} #NN - {Title}' pattern in parent folder
    # ---------------------------------------------------------------------------
    if not series and parent.name and not title_from_parent:
        parent_series, parent_pos = _extract_series_from_stem(parent.name, title)
        if parent_series and parent_pos:
            series = parent_series
            series_position = parent_pos
            source["series"] = "folder"
            source["series_position"] = "folder"

    # ------------------------------------------------------------------
    # 8. Extract author from folder hierarchy
    # ------------------------------------------------------------------
    if not author and depth >= 1:
        author_candidate = folder_parts[0] if folder_parts else None
        if author_candidate and author_candidate not in _NON_AUTHOR_FOLDERS:
            # Never use an ASIN-only folder as author
            if _ASIN_ONLY_RE.match(author_candidate):
                author_candidate = None
            # Only set author if the title didn't come from the same folder
            # (i.e., the title came from the filename, not the parent folder)
            if author_candidate and (source.get("title") == "filename" or depth >= 2):
                author = author_candidate
                source["author"] = "folder"

    # ------------------------------------------------------------------
    # 9. Collect all tokens from the path
    # ------------------------------------------------------------------
    all_text = " ".join(folder_parts + [stem])
    tokens = [t for t in re.findall(r"[^\W_]+", all_text, re.I) if t.lower() not in {
        "unabridged", "abridged", "webrip", "webdl", "audiobook", "ebook",
        "mp3", "epub", "mobi", "azw3", "m4b", "flac", "ogg", "opus", "aac",
        "part", "disc", "cd", "book", "volume", "vol", "no", "number",
        "narrated", "by", "narrator",
    } and not re.match(r"^(19|20)\d{2}$", t)]

    return {
        "author": author,
        "title": title,
        "series": series,
        "series_position": series_position,
        "year": year,
        "narrator": narrator,
        "identifiers": identifiers,
        "tokens": tokens,
        "source": source,
    }


def _extract_title_from_stem(stem: str) -> str:
    """Extract a clean title from a filename stem.

    Strips release cruft, bracketed tags, position prefixes,
    parenthesized positions, narrator info, and ISBNs.
    Does NOT strip bare years (e.g. '1984') — they may be the title.
    """
    if not stem:
        return ""

    text = stem

    # Remove leading series + position prefix: "The Series #3 - Title"
    text = re.sub(
        r"^(.+?)\s*#\s*\d+(?:\.\d+)?\s*[-\u2013\u2014]\s*",
        "",
        text,
        flags=re.I,
    )

    # Remove leading position prefix: "03 - Title", "#3 - Title", "Book 3 - Title"
    text = re.sub(
        r"^(?:book\s+)?(?:(?:#|no\.?|number)\s*)?\d+(?:\.\d+)?\s*[-\u2013\u2014]\s*",
        "",
        text,
        flags=re.I,
    )

    # Remove trailing parenthesized position: "Title (3)", "Title (Book 3)"
    text = re.sub(r"\s*\((?:book\s*)?\d+(?:\.\d+)?\)\s*$", "", text)

    # Remove trailing parenthesized year: "Title (2023)"
    text = re.sub(r"\s*\((19\d{2}|20\d{2})\)\s*$", "", text)

    # Remove ISBN-13
    text = _ISBN13_RE.sub(" ", text)

    # Remove ISBN-10
    text = _ISBN10_RE.sub(" ", text)

    # Remove bracketed tags: [tag], (tag) — but not year parens already removed
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)

    # Remove narrator info
    text = _NARRATOR_RE.sub(" ", text)

    # Remove trailing " - Part N" / " - Disc N" / " - CD N" (before filler, since
    # filler may have already removed the part/disc text leaving a trailing dash)
    text = re.sub(r"\s*[-\u2013\u2014]\s*(?:part|disc|cd)\s*\d+\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*[-\u2013\u2014]\s*$", "", text)  # clean up any trailing dash

    # Remove filler tokens (but NOT bare years)
    text = _FILLER_RE.sub(" ", text)

    # Remove trailing " - NNN" (position suffix)
    text = re.sub(r"\s*[-\u2013\u2014]\s*\d{1,3}\s*$", "", text)

    # Remove trailing short numeric sequences ONLY if preceded by a dash or
    # clearly a position suffix (not part of a title like "Fahrenheit 451")
    text = re.sub(r"\s*[-\u2013\u2014]\s*\d{2,5}\s*$", "", text)

    # Remove trailing standalone numeric sequences (5+ digits, not years)
    # These are likely release tags, not part of the title
    text = re.sub(r"\s+\d{5,}\s*$", "", text)

    # Clean up whitespace and punctuation
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Fix apostrophe spacing artifacts
    text = re.sub(r"\s+'\s*", "'", text)
    text = re.sub(r"'\s+", "'", text)

    return text or stem


def _extract_series_from_stem(
    text: str,
    title: str | None = None,
) -> tuple[str | None, str | None]:
    """Extract series name and position from a text string.

    Handles patterns like:
    - "1 - Title" or "#1 - Title" or "Book 1 - Title"
    - "Title (1)" or "Title (Book 1)"
    - "{Series} #NN - {Title}"
    - Uses seriesnorm.split_position for complex series strings

    Returns (series_name, position) or (None, None).
    """
    if not text:
        return None, None

    stripped = text.strip()

    # Pattern: "{Series} #NN - {Title}" — e.g. "The Series #3 - Book Title"
    series_hash_re = re.compile(
        r"^(.+?)\s*#\s*(\d+(?:\.\d+)?)\s*[-\u2013\u2014]\s*(.+)$"
    )
    m = series_hash_re.match(stripped)
    if m:
        series_name = m.group(1).strip()
        pos = m.group(2)
        remaining_title = m.group(3).strip()
        if title and remaining_title.lower() == title.lower():
            return series_name, pos
        return series_name, pos

    # Pattern: "NN - Title" or "#NN - Title" or "Book NN - Title"
    m = _POS_TITLE_RE.match(stripped)
    if m:
        pos = m.group(1)
        remaining = m.group(2).strip()
        # If we have a title, this is likely just a position prefix
        if title and remaining.lower() == title.lower():
            return None, pos
        # Otherwise the remaining text might be the series name
        return None, pos

    # Pattern: "Title (NN)" or "Title (Book NN)" - but not years
    m = _TITLE_POS_RE.match(stripped)
    if m:
        t = m.group(1).strip()
        pos = m.group(2)
        # Skip if the "position" is actually a year
        if _YEAR_PAREN_RE.match(pos):
            return None, None
        if title and t.lower() == title.lower():
            return None, pos
        return t, pos

    # Try seriesnorm.split_position for complex series strings
    # (e.g., "Series Name -- [3]")
    clean = stripped
    series_name, pos = split_position(clean)
    if pos and series_name != clean:
        # Only accept if the series name is not the same as the cleaned text
        # (i.e., something was actually extracted)
        # Also reject if the series name matches the title (scene-style names)
        if title and series_name.lower() == title.lower():
            return None, None
        return series_name, pos

    return None, None
