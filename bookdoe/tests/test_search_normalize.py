"""Query normalization: punctuation, unicode, initials, volume markers, noise."""
from app.services.search_normalize import normalize_query, split_title_subtitle


# -- Punctuation / quotes -----------------------------------------------------

def test_curly_apostrophe_normalizes_to_straight():
    assert normalize_query("’Til Death") == normalize_query("'Til Death")
    assert normalize_query("’Til Death") == "'Til Death"


def test_curly_double_quotes_normalize_to_straight():
    assert normalize_query("“The Great Gatsby”") == '"The Great Gatsby"'


def test_backtick_and_acute_accent_normalize_to_apostrophe():
    assert normalize_query("Book`s Title") == "Books Title"
    assert normalize_query("Book´s Title") == "Books Title"


# -- Unicode (NFC) --------------------------------------------------------------

def test_unicode_nfc_normalization():
    decomposed = "Café"  # "Café" with combining acute accent
    precomposed = "Café"
    assert normalize_query(decomposed) == normalize_query(precomposed)


# -- Author initials --------------------------------------------------------------

def test_author_initials_expanded():
    assert normalize_query("J.R.R. Tolkien") == "J R R Tolkien"


def test_two_letter_initials_expanded():
    assert normalize_query("'Til Death S.C. Stephens") == "'Til Death S C Stephens"


def test_single_initial_not_expanded():
    # A lone "A." isn't a run of initials; leave it alone.
    assert normalize_query("A. Book") == "A. Book"


# -- Ampersands --------------------------------------------------------------

def test_ampersand_normalized_to_and():
    assert normalize_query("Tom & Jerry") == "Tom and Jerry"


def test_html_amp_entity_normalized_to_and():
    assert normalize_query("Tom &amp; Jerry") == "Tom and Jerry"


# -- Subtitle splitting --------------------------------------------------------------

def test_split_on_colon():
    assert split_title_subtitle("Dune: Part Two") == ("Dune", "Part Two")


def test_split_on_spaced_dash():
    assert split_title_subtitle("Dune - Part Two") == ("Dune", "Part Two")


def test_split_on_pipe():
    assert split_title_subtitle("Dune | Part Two") == ("Dune", "Part Two")


def test_no_subtitle_delimiter_returns_none():
    assert split_title_subtitle("Dune") == ("Dune", None)


def test_hyphenated_word_not_treated_as_subtitle_delimiter():
    # No surrounding whitespace around the dash, so it's not a subtitle boundary.
    assert split_title_subtitle("Spider-Man") == ("Spider-Man", None)


def test_normalize_query_flattens_subtitle_delimiter():
    assert normalize_query("Dune: Part Two") == "Dune Part Two"


# -- Possessives --------------------------------------------------------------

def test_possessive_apostrophe_s_dropped():
    assert normalize_query("Dog's Life") == "Dogs Life"


def test_leading_elision_apostrophe_not_treated_as_possessive():
    assert normalize_query("'Til Death") == "'Til Death"


# -- Volume markers --------------------------------------------------------------

def test_strips_vol_marker():
    assert normalize_query("Some Series Vol. 1") == "Some Series"


def test_strips_volume_marker():
    assert normalize_query("Some Series Volume 2") == "Some Series"


def test_strips_book_marker():
    assert normalize_query("Some Series Book 1") == "Some Series"


def test_strips_bk_marker():
    assert normalize_query("Some Series Bk. 3") == "Some Series"


def test_strips_roman_numeral_volume_marker():
    assert normalize_query("Some Series Book III") == "Some Series"


# -- Filename/release noise --------------------------------------------------------------

def test_strips_year_in_parens():
    assert normalize_query("The Hobbit (2024)") == "The Hobbit"


def test_strips_bracketed_release_tag():
    assert normalize_query("The Hobbit [BluRay]") == "The Hobbit"


def test_strips_resolution_tag():
    assert normalize_query("The Hobbit (1080p)") == "The Hobbit"


def test_strips_multiple_noise_tags():
    assert normalize_query("The Hobbit (2024) [EPUB] (Unabridged)") == "The Hobbit"


# -- Idempotence --------------------------------------------------------------

def test_idempotent_on_plain_query():
    q = "The Great Gatsby"
    assert normalize_query(normalize_query(q)) == normalize_query(q)


def test_idempotent_on_heavily_noisy_query():
    q = "’Til Death S.C. Stephens Vol. 2 (2024) [EPUB]"
    once = normalize_query(q)
    assert normalize_query(once) == once


def test_idempotent_on_possessive_and_ampersand():
    q = "Tom & Jerry's Big Adventure"
    once = normalize_query(q)
    assert normalize_query(once) == once


def test_idempotent_empty_and_blank():
    assert normalize_query("") == ""
    assert normalize_query("   ") == ""
    assert normalize_query(normalize_query("")) == ""
