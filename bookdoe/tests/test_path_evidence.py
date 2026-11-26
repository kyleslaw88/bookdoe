"""Table-driven tests for pathevidence.parse_path_evidence (Part XVIII).

Covers Audible-style, Calibre-style, scene-style, multipart, root-level,
series-position, ASIN, ISBN, narrator, and year patterns.
"""
from __future__ import annotations

import pytest

from app.services import pathevidence


# ---------------------------------------------------------------------------
# Test data: (path, root_path, expected_author, expected_title,
#             expected_series, expected_position, expected_year,
#             expected_asin, expected_isbn13, expected_isbn10,
#             expected_narrator)
# ---------------------------------------------------------------------------
PATH_TESTS = [
    # 1. Audible-style: Author/Title (Year)/file
    (
        "/media/Author Name/Book Title (2023)/book.m4b",
        "/media",
        "Author Name", "Book Title", None, None, "2023",
        None, None, None, None,
    ),
    # 2. Calibre-style: Author/Series/NN - Title/file
    (
        "/media/Author Name/The Series/03 - Book Title/file.epub",
        "/media",
        "Author Name", "Book Title", "The Series", "3", None,
        None, None, None, None,
    ),
    # 3. Simple Author/Title/file
    (
        "/media/J.K. Rowling/Harry Potter and the Philosopher's Stone/file.epub",
        "/media",
        "J.K. Rowling", "Harry Potter and the Philosopher's Stone",
        None, None, None, None, None, None, None,
    ),
    # 4. Root-level file (no author)
    (
        "/media/Dune.epub",
        "/media",
        None, "Dune", None, None, None,
        None, None, None, None,
    ),
    # 5. File with ASIN in folder name
    (
        "/media/Author Name/B00ABCDEFG/Book Title/file.m4b",
        "/media",
        "Author Name", "Book Title", None, None, None,
        "B00ABCDEFG", None, None, None,
    ),
    # 6. File with narrator info
    (
        "/media/Author Name/Book Title (Narrated by Jane Smith).m4b",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, "Jane Smith",
    ),
    # 7. Part file: title from parent folder
    (
        "/media/Author Name/Book Title/part 1.mp3",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
    # 8. Series with position in filename: "03 - Title"
    (
        "/media/Author Name/03 - Book Title/file.epub",
        "/media",
        "Author Name", "Book Title", None, "3", None,
        None, None, None, None,
    ),
    # 9. Title with position in parens: "Title (3)"
    (
        "/media/Author Name/Book Title (3)/file.epub",
        "/media",
        "Author Name", "Book Title", None, "3", None,
        None, None, None, None,
    ),
    # 10. ISBN-13 in filename (valid checksum: 9780306406157)
    (
        "/media/Author Name/Book Title 9780306406157.epub",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, "9780306406157", None, None,
    ),
    # 11. ISBN-10 in filename (valid checksum: 000000006X)
    (
        "/media/Author Name/Book Title 000000006X.epub",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, "000000006X", None,
    ),
    # 12. Malformed ISBN (too short) should be dropped
    (
        "/media/Author Name/Book Title 12345.epub",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
    # 13. Audiobook with "Unabridged" cruft
    (
        "/media/Author Name/Book Title Unabridged.m4b",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
    # 14. Book with series prefix: "Book 5 - Title"
    (
        "/media/Author Name/Book 5 - Book Title/file.epub",
        "/media",
        "Author Name", "Book Title", None, "5", None,
        None, None, None, None,
    ),
    # 15. Root-level file with bare noun (no author guessed)
    (
        "/media/Dune.epub",
        None,
        None, "Dune", None, None, None,
        None, None, None, None,
    ),
    # 16. ASIN in parent folder — ASIN is NOT the author
    (
        "/media/B00XYZ1234/Book Title/file.m4b",
        "/media",
        None, "Book Title", None, None, None,
        "B00XYZ1234", None, None, None,
    ),
    # 17. Narrator in parent folder
    (
        "/media/Author Name/Narrated by John Doe/Book Title/file.m4b",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, "John Doe",
    ),
    # 18. Year in parent folder
    (
        "/media/Author Name/Book Title (2020)/file.epub",
        "/media",
        "Author Name", "Book Title", None, None, "2020",
        None, None, None, None,
    ),
    # 19. Disc file
    (
        "/media/Author Name/Book Title/disc 2.mp3",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
    # 20. Complex series with position: "Series Name -- [3]"
    (
        "/media/Author Name/The Series Name -- [3]/Book Title/file.epub",
        "/media",
        "Author Name", "Book Title", "The Series Name", "3", None,
        None, None, None, None,
    ),
    # 21. '{Series} #NN - {Title}' pattern
    (
        "/media/Author Name/The Series #3 - Book Title/file.epub",
        "/media",
        "Author Name", "Book Title", "The Series", "3", None,
        None, None, None, None,
    ),
    # 22. 'Title [ASIN]' pattern — ASIN stripped from title
    (
        "/media/Author Name/Book Title [B00ABCDEFG]/file.m4b",
        "/media",
        "Author Name", "Book Title", None, None, None,
        "B00ABCDEFG", None, None, None,
    ),
    # 23. '{ASIN B0...}' pattern — braces stripped
    (
        "/media/Author Name/[ASIN B00ABCDEFG] Book Title/file.m4b",
        "/media",
        "Author Name", "Book Title", None, None, None,
        "B00ABCDEFG", None, None, None,
    ),
    # 24. Scene-style name: "Author - Title - Part 01"
    (
        "/media/Author Name/Author - Book Title - Part 01.mp3",
        "/media",
        "Author Name", "Author - Book Title", None, None, None,
        None, None, None, None,
    ),
    # 25. Title "1984" should NOT be corrupted by year stripping
    (
        "/media/Orwell/1984.epub",
        "/media",
        "Orwell", "1984", None, None, None,
        None, None, None, None,
    ),
    # 26. Title "Fahrenheit 451" should NOT be corrupted
    (
        "/media/Bradbury/Fahrenheit 451.epub",
        "/media",
        "Bradbury", "Fahrenheit 451", None, None, None,
        None, None, None, None,
    ),
    # 27. Malformed ISBN-13 (invalid checksum) should be dropped
    (
        "/media/Author Name/Book Title 9781234567890.epub",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
    # 28. Malformed ISBN-10 (invalid checksum) should be dropped
    (
        "/media/Author Name/Book Title 0123456780.epub",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
    # 29. Title with bitrate text stripped: "Title 320kbps"
    (
        "/media/Author Name/Book Title 320kbps.mp3",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
    # 30. Title with [tag] stripped: "Title [HQ]"
    (
        "/media/Author Name/Book Title [HQ].m4b",
        "/media",
        "Author Name", "Book Title", None, None, None,
        None, None, None, None,
    ),
]


@pytest.mark.parametrize(
    "path,root,exp_author,exp_title,exp_series,exp_pos,exp_year,"
    "exp_asin,exp_isbn13,exp_isbn10,exp_narrator",
    PATH_TESTS,
    ids=[str(i) for i in range(len(PATH_TESTS))],
)
def test_parse_path_evidence(
    path, root, exp_author, exp_title, exp_series, exp_pos, exp_year,
    exp_asin, exp_isbn13, exp_isbn10, exp_narrator,
):
    result = pathevidence.parse_path_evidence(path, root)

    assert result["author"] == exp_author, f"author mismatch for {path}"
    assert result["title"] == exp_title, f"title mismatch for {path}"
    assert result["series"] == exp_series, f"series mismatch for {path}"
    assert result["series_position"] == exp_pos, f"position mismatch for {path}"
    assert result["year"] == exp_year, f"year mismatch for {path}"
    assert result["narrator"] == exp_narrator, f"narrator mismatch for {path}"

    # Identifiers must match exactly — no spurious extras
    expected_ids = {}
    if exp_asin:
        expected_ids["asin"] = exp_asin
    if exp_isbn13:
        expected_ids["isbn13"] = exp_isbn13
    if exp_isbn10:
        expected_ids["isbn10"] = exp_isbn10

    assert result["identifiers"] == expected_ids, \
        f"identifiers mismatch for {path}: got {result['identifiers']}, expected {expected_ids}"

    # Evidence source should be set for non-None fields
    if exp_author:
        assert result["source"].get("author") in ("folder",), \
            f"author source wrong for {path}: {result['source'].get('author')}"
    if exp_title:
        assert result["source"].get("title") in ("filename", "folder"), \
            f"title source wrong for {path}: {result['source'].get('title')}"
    if exp_year:
        assert result["source"].get("year") in ("filename", "folder"), \
            f"year source wrong for {path}: {result['source'].get('year')}"
    if exp_asin:
        assert result["source"].get("asin") in ("folder", "filename"), \
            f"asin source wrong for {path}: {result['source'].get('asin')}"
    if exp_isbn13:
        assert result["source"].get("isbn13") in ("folder", "filename"), \
            f"isbn13 source wrong for {path}: {result['source'].get('isbn13')}"
    if exp_isbn10:
        assert result["source"].get("isbn10") in ("folder", "filename"), \
            f"isbn10 source wrong for {path}: {result['source'].get('isbn10')}"


def test_evidence_source_tracking():
    """Each detected field should have a source tag."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title (2023)/file.epub", "/media"
    )
    assert result["source"]["author"] == "folder"
    # Title comes from parent folder (stem "file" is generic)
    assert result["source"]["title"] == "folder"
    assert result["source"]["year"] == "folder"


def test_root_level_file_no_author():
    """A file directly in the root gets no author, not guessed from root name."""
    result = pathevidence.parse_path_evidence("media/Dune.epub", "media")
    assert result["author"] is None
    assert result["title"] == "Dune"


def test_part_file_title_from_parent():
    """Part/disc files take title from the parent folder."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title/part 1.mp3", "/media"
    )
    assert result["title"] == "Book Title"
    assert result["source"]["title"] == "folder"


def test_tokens_excludes_filler_words():
    """Collected tokens should not include filler/release-cruft words."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title Unabridged 2023.mp3", "/media"
    )
    assert "unabridged" not in result["tokens"]
    assert "2023" not in result["tokens"]
    assert "book" not in result["tokens"]  # "book" is filler in some contexts
    assert "Title" in result["tokens"]
    assert "Author" in result["tokens"]


def test_isbn10_uppercased():
    """ISBN-10 values should be uppercased."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book 000000006x.epub", "/media"
    )
    assert result["identifiers"].get("isbn10") == "000000006X"


def test_empty_stem_returns_empty_title():
    """A file with a dot-only stem (no real name) gets empty title,
    not the root folder name."""
    result = pathevidence.parse_path_evidence("/media/.epub", "/media")
    assert result["title"] == ""
    assert result["author"] is None


def test_non_author_folders_ignored():
    """Common container folder names should not be treated as authors."""
    result = pathevidence.parse_path_evidence(
        "/media/books/Book Title/file.epub", "/media"
    )
    assert result["author"] is None
    assert result["title"] == "Book Title"


def test_asin_pattern_valid():
    """Valid ASIN pattern (B0 + 8 alphanumeric chars = 10 total) is detected."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/B00ABCDEFG/file.m4b", "/media"
    )
    assert result["identifiers"].get("asin") == "B00ABCDEFG"
    assert result["source"].get("asin") == "folder"


def test_asin_pattern_invalid():
    """Invalid ASIN pattern (wrong length/prefix) is not detected."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/B00Short/file.m4b", "/media"
    )
    assert "asin" not in result["identifiers"]


def test_isbn_checksum_validation():
    """Malformed ISBNs (invalid checksum) are dropped."""
    # Invalid ISBN-13 checksum
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book 9781234567890.epub", "/media"
    )
    assert "isbn13" not in result["identifiers"]

    # Invalid ISBN-10 checksum
    result2 = pathevidence.parse_path_evidence(
        "/media/Author Name/Book 0123456780.epub", "/media"
    )
    assert "isbn10" not in result2["identifiers"]


def test_isbn_source_tagged_folder_vs_filename():
    """ISBN found in a folder should be tagged 'folder', not 'filename'."""
    result = pathevidence.parse_path_evidence(
        "/media/9780306406157/Book Title/file.epub", "/media"
    )
    assert result["identifiers"].get("isbn13") == "9780306406157"
    assert result["source"].get("isbn13") == "folder"

    result2 = pathevidence.parse_path_evidence(
        "/media/Author Name/Book 9780306406157.epub", "/media"
    )
    assert result2["identifiers"].get("isbn13") == "9780306406157"
    assert result2["source"].get("isbn13") == "filename"


def test_title_free_of_filler_text():
    """Title should not contain 'Unabridged', bitrate, or [tag] text."""
    # Unabridged
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title Unabridged.m4b", "/media"
    )
    assert "Unabridged" not in result["title"]
    assert result["title"] == "Book Title"

    # [tag]
    result2 = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title [HQ].m4b", "/media"
    )
    assert "[HQ]" not in result2["title"]
    assert result2["title"] == "Book Title"


def test_root_level_no_author_no_root_folder_as_title():
    """A root-level file with a generic stem should not use root folder as title."""
    result = pathevidence.parse_path_evidence("/media/.epub", "/media")
    assert result["title"] == ""
    assert result["author"] is None


def test_no_root_path_does_not_treat_top_ancestor_as_author():
    """Without root_path, the top ancestor should not be treated as author
    for a simple path like /media/Dune.epub."""
    result = pathevidence.parse_path_evidence("/media/Dune.epub")
    assert result["author"] is None
    assert result["title"] == "Dune"


def test_no_root_path_top_ancestor_not_in_denylist():
    """F3 follow-up: a top ancestor that is NOT in _NON_AUTHOR_FOLDERS must
    still not be treated as author when root_path is None.

    The existing test uses /media/Dune.epub which passes only because
    'media' is in _NON_AUTHOR_FOLDERS.  This test uses /mnt/Dune.epub and
    /data/Dune.epub — neither 'mnt' nor 'data' is on the denylist.
    """
    for path in ("/mnt/Dune.epub", "/data/Dune.epub", "/srv/Book.m4b"):
        result = pathevidence.parse_path_evidence(path)
        assert result["author"] is None, (
            f"Expected author=None for {path}, got {result['author']!r}"
        )

# ---------------------------------------------------------------------------
# Additional tests for reviewer findings
# ---------------------------------------------------------------------------


def test_asin_source_filename():
    """ASIN found in the filename stem should be tagged 'filename'."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title [B00ABCDEFG].m4b", "/media"
    )
    assert result["identifiers"].get("asin") == "B00ABCDEFG"
    assert result["source"].get("asin") == "filename"


def test_isbn_source_folder_vs_filename():
    """ISBN found in a folder should be tagged 'folder', not 'filename'."""
    # ISBN in folder
    result = pathevidence.parse_path_evidence(
        "/media/9780306406157/Book Title/file.epub", "/media"
    )
    assert result["identifiers"].get("isbn13") == "9780306406157"
    assert result["source"].get("isbn13") == "folder"

    # ISBN in filename
    result2 = pathevidence.parse_path_evidence(
        "/media/Author Name/Book 9780306406157.epub", "/media"
    )
    assert result2["identifiers"].get("isbn13") == "9780306406157"
    assert result2["source"].get("isbn13") == "filename"


def test_bitrate_stripped_from_title():
    """Bitrate text should be stripped from the title."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title 320kbps.mp3", "/media"
    )
    assert result["title"] == "Book Title"
    assert "320kbps" not in result["title"]


def test_tag_stripped_from_title():
    """[tag] text should be stripped from the title."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title [HQ].m4b", "/media"
    )
    assert result["title"] == "Book Title"
    assert "[HQ]" not in result["title"]


def test_unabridged_stripped_from_title():
    """'Unabridged' should be stripped from the title."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title Unabridged.m4b", "/media"
    )
    assert result["title"] == "Book Title"
    assert "Unabridged" not in result["title"]


def test_year_in_title_preserved():
    """A title that is a year (e.g. '1984') should be preserved."""
    result = pathevidence.parse_path_evidence(
        "/media/Orwell/1984.epub", "/media"
    )
    assert result["title"] == "1984"
    assert result["author"] == "Orwell"


def test_fahrenheit_451_preserved():
    """A title containing a number (e.g. 'Fahrenheit 451') should be preserved."""
    result = pathevidence.parse_path_evidence(
        "/media/Bradbury/Fahrenheit 451.epub", "/media"
    )
    assert result["title"] == "Fahrenheit 451"
    assert result["author"] == "Bradbury"


def test_scene_style_name():
    """Scene-style name 'Author - Title - Part 01' should extract correctly."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Author - Book Title - Part 01.mp3", "/media"
    )
    assert result["title"] == "Author - Book Title"
    assert result["author"] == "Author Name"


def test_series_hash_pattern():
    """'{Series} #NN - {Title}' pattern should extract series and position."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/The Series #3 - Book Title/file.epub", "/media"
    )
    assert result["series"] == "The Series"
    assert result["series_position"] == "3"
    assert result["title"] == "Book Title"


def test_asin_in_folder_not_as_author():
    """An ASIN-only folder should not be treated as an author."""
    result = pathevidence.parse_path_evidence(
        "/media/B00XYZ1234/Book Title/file.m4b", "/media"
    )
    assert result["author"] is None
    assert result["identifiers"].get("asin") == "B00XYZ1234"
    assert result["source"].get("asin") == "folder"


def test_malformed_isbn13_dropped():
    """Malformed ISBN-13 (invalid checksum) should be dropped."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title 9781234567890.epub", "/media"
    )
    assert "isbn13" not in result["identifiers"]


def test_malformed_isbn10_dropped():
    """Malformed ISBN-10 (invalid checksum) should be dropped."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title 0123456780.epub", "/media"
    )
    assert "isbn10" not in result["identifiers"]


def test_isbn10_valid_checksum_kept():
    """A valid ISBN-10 checksum should be kept."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/Book Title 000000006X.epub", "/media"
    )
    assert result["identifiers"].get("isbn10") == "000000006X"


def test_isbn13_takes_precedence_over_isbn10():
    """If both ISBN-13 and ISBN-10 patterns match, ISBN-13 wins."""
    result = pathevidence.parse_path_evidence(
        "/media/Author Name/9780306406157 000000006x.epub", "/media"
    )
    assert result["identifiers"].get("isbn13") == "9780306406157"
    assert "isbn10" not in result["identifiers"]
