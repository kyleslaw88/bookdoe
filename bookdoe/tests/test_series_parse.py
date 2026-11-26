"""Parser table tests for services/seriesnorm.parse_series (split, clean, position, denylist).

Each row is (raw string, [(name, position, ignored?)]). The first block is the production
data the owner reported on 2026-10-14, verbatim; the rest are realistic German/French/English
strings reviewed by hand. `ignored` means "not a series" (publisher, imprint, collection,
catalog noise) - it is recorded as a rejected assertion, never a series row."""
import random
import string

import pytest

from app.services import seriesnorm
from app.services.seriesnorm import parse_series

PRODUCTION = [
    ("[The three i's", [("The three i's", None, False)]),
    ("[Manwhore", [("Manwhore", None, False)]),
    ("[Under the never sky", [("Under the never sky", None, False)]),
    ("[Three Pines mystery]", [("Three Pines mystery", None, False)]),
    ("[Jewel]", [("Jewel", None, False)]),
    ("Cocktail #3)", [("Cocktail", "3", False)]),
    (
        "The Anetakis Tycoons - 1, Bestselling Author Collection - 45, Mills & Boon Desire 2-in-1 - 322, Silhouette Desire",
        [("The Anetakis Tycoons", "1", False), ("Bestselling Author Collection", "45", True),
         ("Mills & Boon Desire 2-in-1", None, True), ("Silhouette Desire", None, True)],
    ),
    (
        "The Anetakis Tycoons - 2, Bestselling Author Collection - 46, Mills & Boon Desire 2-in-1 - 330, Silhouette Desire",
        [("The Anetakis Tycoons", "2", False), ("Bestselling Author Collection", "46", True),
         ("Mills & Boon Desire 2-in-1", None, True), ("Silhouette Desire", None, True)],
    ),
    (
        "Mistborn, Era 2: Wax & Wayne (#3), The Mistborn Saga (#6), T...",
        [("Mistborn, Era 2: Wax & Wayne", "3", False), ("The Mistborn Saga", "6", False)],
    ),
    ("Vera Stanhope ermittelt, 8; rororo", [("Vera Stanhope ermittelt", "8", False), ("rororo", None, True)]),
    ("Knaur", [("Knaur", None, True)]),
    ("Goldmann", [("Goldmann", None, True)]),
    ("Le livre de poche : thriller", [("Le livre de poche : thriller", None, True)]),
    ("The Anetakis Tycoons - 1920", [("The Anetakis Tycoons", None, False)]),
    ("Knaur 60291", [("Knaur 60291", None, True)]),
]

REALISTIC = [
    ('The Dark Tower (Book 3)', [('The Dark Tower', '3', False)]),
    ('The Dark Tower, Book 3', [('The Dark Tower', '3', False)]),
    ('The Dark Tower #3', [('The Dark Tower', '3', False)]),
    ('The Dark Tower, #3', [('The Dark Tower', '3', False)]),
    ('The Dark Tower - 3', [('The Dark Tower', '3', False)]),
    ('The Dark Tower -- 3', [('The Dark Tower', '3', False)]),
    ('The Dark Tower (#3)', [('The Dark Tower', '3', False)]),
    ('The Dark Tower [3]', [('The Dark Tower', '3', False)]),
    ('The Dark Tower, Vol. 3', [('The Dark Tower', '3', False)]),
    ('The Dark Tower Vol 3', [('The Dark Tower', '3', False)]),
    ('Die Tribute von Panem, Band 2', [('Die Tribute von Panem', '2', False)]),
    ('Die Tribute von Panem (Band 2)', [('Die Tribute von Panem', '2', False)]),
    ('Die Tribute von Panem 2', [('Die Tribute von Panem 2', None, False)]),
    ('Die Rebellen von Panem - Teil 3', [('Die Rebellen von Panem', '3', False)]),
    ('Das Lied von Eis und Feuer, Bd. 4', [('Das Lied von Eis und Feuer', '4', False)]),
    ('Le Seigneur des anneaux, Tome 2', [('Le Seigneur des anneaux', '2', False)]),
    ('Les Rougon-Macquart, tome 5', [('Les Rougon-Macquart', '5', False)]),
    ('Les Rougon-Macquart (Tome 5)', [('Les Rougon-Macquart', '5', False)]),
    ('Millennium, 1', [('Millennium', '1', False)]),
    ('Millennium Trilogie 3', [('Millennium Trilogie 3', None, False)]),
    ('Harry Potter, Book Two', [('Harry Potter', '2', False)]),
    ('Harry Potter, Book II', [('Harry Potter', '2', False)]),
    ('Foo, Vol. IV', [('Foo', '4', False)]),
    ('Rocky III', [('Rocky III', None, False)]),
    ('Henry VIII', [('Henry VIII', None, False)]),
    ('Catch-22', [('Catch-22', None, False)]),
    ('Fahrenheit 451', [('Fahrenheit 451', None, False)]),
    ('District 9', [('District 9', None, False)]),
    ('Spider-Man 2', [('Spider-Man 2', None, False)]),
    ('Doctor Who - 2005', [('Doctor Who', None, False)]),
    ('Doctor Who #2005', [('Doctor Who', None, False)]),
    ('Foo #2.5', [('Foo', '2.5', False)]),
    ('Foo, 2.50', [('Foo', '2.5', False)]),
    ('Foo, 03', [('Foo', '3', False)]),
    ('Foo 3 of 10', [('Foo', '3', False)]),
    ('Foo, Book 3 of 10', [('Foo', '3', False)]),
    ('3 of 10', [('3 of 10', None, True)]),
    ('Foo (3 of 10)', [('Foo', '3', False)]),
    ('Foo (1-3)', [('Foo', '1-3', False)]),
    ('Foo, Books 1-3', [('Foo', '1-3', False)]),
    ('"Foo"', [('Foo', None, False)]),
    ("'Foo'", [('Foo', None, False)]),
    ('"Foo', [('Foo', None, False)]),
    ('(Foo', [('Foo', None, False)]),
    ('Foo)', [('Foo', None, False)]),
    ('[Foo]', [('Foo', None, False)]),
    ('[Foo] (Book 2)', [('Foo', '2', False)]),
    ('Foo (Bar', [('Foo Bar', None, False)]),
    ('Foo (Bar #3', [('Foo Bar', '3', False)]),
    ('Foo (Bar #3)', [('Foo (Bar)', '3', False)]),
    ('Foo ; Bar', [('Foo', None, False), ('Bar', None, False)]),
    ('Foo; Bar #2', [('Foo', None, False), ('Bar', '2', False)]),
    ('Foo (#1); Bar (#2); Baz (#3)', [('Foo', '1', False), ('Bar', '2', False), ('Baz', '3', False)]),
    ('Foo, Bar', [('Foo, Bar', None, False)]),
    ('Foo, Bar, Baz', [('Foo, Bar, Baz', None, False)]),
    ('Smith, John', [('Smith, John', None, False)]),
    ('Alex Cross, Book 3', [('Alex Cross', '3', False)]),
    ('Alex Cross, Jack Reacher', [('Alex Cross, Jack Reacher', None, False)]),
    ('Alex Cross #3, Jack Reacher #5', [('Alex Cross', '3', False), ('Jack Reacher', '5', False)]),
    ('Mistborn, Era 2: Wax & Wayne', [('Mistborn, Era 2: Wax & Wayne', None, False)]),
    ('Mistborn, Era 2: Wax & Wayne (#3)', [('Mistborn, Era 2: Wax & Wayne', '3', False)]),
    ('Wheel of Time, 1; Rad', [('Wheel of Time', '1', False), ('Rad', None, False)]),
    ('Vera Stanhope ermittelt, 8; rororo', [('Vera Stanhope ermittelt', '8', False), ('rororo', None, True)]),
    ('Vera Stanhope, 8', [('Vera Stanhope', '8', False)]),
    ('Knaur 60291', [('Knaur 60291', None, True)]),
    ('Knaur', [('Knaur', None, True)]),
    ('Knaur Taschenbuch', [('Knaur Taschenbuch', None, True)]),
    ('Droemer Knaur', [('Droemer Knaur', None, True)]),
    ('Goldmann', [('Goldmann', None, True)]),
    ('Goldmann 47462', [('Goldmann 47462', None, True)]),
    ('Goldmann Taschenbuch, 12', [('Goldmann Taschenbuch', '12', True)]),
    ('Le livre de poche : thriller', [('Le livre de poche : thriller', None, True)]),
    ('Le livre de poche, 37185', [('Le livre de poche', None, True)]),
    ('Livre de Poche', [('Livre de Poche', None, True)]),
    ('Heyne', [('Heyne', None, True)]),
    ('Heyne Bücher', [('Heyne Bücher', None, True)]),
    ('Heyne allgemeine Reihe', [('Heyne allgemeine Reihe', None, True)]),
    ('Piper', [('Piper', None, True)]),
    ('Piper Verlag', [('Piper Verlag', None, True)]),
    ('Piper Taschenbuch, 6523', [('Piper Taschenbuch', None, True)]),
    ('Fischer Taschenbuch', [('Fischer Taschenbuch', None, True)]),
    ('Fischer', [('Fischer', None, True)]),
    ('btb', [('btb', None, True)]),
    ('Penguin Classics', [('Penguin Classics', None, True)]),
    ('Penguin Books', [('Penguin Books', None, True)]),
    ('Penguin Random House', [('Penguin Random House', None, True)]),
    ('Mills & Boon', [('Mills & Boon', None, True)]),
    ('Mills & Boon Desire 2-in-1 - 322', [('Mills & Boon Desire 2-in-1', None, True)]),
    ('Mills and Boon Modern', [('Mills and Boon Modern', None, True)]),
    ('Harlequin', [('Harlequin', None, True)]),
    ('Harlequin Presents', [('Harlequin Presents', None, True)]),
    ('Harlequin Presents #1920', [('Harlequin Presents', None, True)]),
    ('Silhouette Desire', [('Silhouette Desire', None, True)]),
    ('Silhouette Romance', [('Silhouette Romance', None, True)]),
    ('Silhouette Intimate Moments #44', [('Silhouette Intimate Moments', '44', True)]),
    ('Bestselling Author Collection - 45', [('Bestselling Author Collection', '45', True)]),
    ('Black Lace', [('Black Lace', None, True)]),
    ('Large Print', [('Large Print', None, True)]),
    ('Thorndike Press Large Print', [('Thorndike Press Large Print', None, True)]),
    ('2-in-1', [('2-in-1', None, True)]),
    ('3 in 1 Collection', [('3 in 1 Collection', None, True)]),
    ('Rororo', [('Rororo', None, True)]),
    ('rororo thriller', [('rororo thriller', None, True)]),
    ('rororo rotfuchs', [('rororo rotfuchs', None, True)]),
    ("J'ai lu", [("J'ai lu", None, True)]),
    ("J'ai lu, 4567", [("J'ai lu", None, True)]),
    ('Folio', [('Folio', None, True)]),
    ('Folio policier', [('Folio policier', None, False)]),
    ('Gollancz', [('Gollancz', None, True)]),
    ('Pocket', [('Pocket', None, True)]),
    ('Pocket Books', [('Pocket Books', None, True)]),
    ('Tor', [('Tor', None, True)]),
    ('Tor Teen', [('Tor Teen', None, False)]),
    ('The Hunger Games', [('The Hunger Games', None, False)]),
    ('The 39 Clues', [('The 39 Clues', None, False)]),
    ('39 Clues: Unstoppable -- book two', [('39 Clues: Unstoppable', '2', False)]),
    ('The Expanse', [('The Expanse', None, False)]),
    ('Discworld', [('Discworld', None, False)]),
    ('Sherlock Holmes', [('Sherlock Holmes', None, False)]),
    ('Pippi Langstrumpf', [('Pippi Langstrumpf', None, False)]),
    ('Die drei ???', [('Die drei ???', None, False)]),
    ('Die drei Fragezeichen, 14', [('Die drei Fragezeichen', '14', False)]),
    ('TKKG, 7', [('TKKG', '7', False)]),
    ('Hanni und Nanni', [('Hanni und Nanni', None, False)]),
    ('Asterix, Band 12', [('Asterix', '12', False)]),
    ('Astérix, 12', [('Astérix', '12', False)]),
    ('Les Aventures de Tintin', [('Les Aventures de Tintin', None, False)]),
    ('Thursday Next', [('Thursday Next', None, False)]),
    ('Aubrey–Maturin', [('Aubrey–Maturin', None, False)]),
    ('Aubrey-Maturin, Book 3', [('Aubrey-Maturin', '3', False)]),
    ('Wings of Fire, Book 1', [('Wings of Fire', '1', False)]),
    ('Percy Jackson & the Olympians', [('Percy Jackson & the Olympians', None, False)]),
    ('Percy Jackson and the Olympians, Book 1', [('Percy Jackson and the Olympians', '1', False)]),
    ('A Song of Ice and Fire', [('A Song of Ice and Fire', None, False)]),
    ('Game of Thrones', [('Game of Thrones', None, False)]),
    ("St. Mary's Chronicles", [("St. Mary's Chronicles", None, False)]),
    ('Dr. Siri Paiboun', [('Dr. Siri Paiboun', None, False)]),
    ("J. D. Robb's In Death", [("J. D. Robb's In Death", None, False)]),
    ('In Death, #12', [('In Death', '12', False)]),
    ('In Death #1920', [('In Death', None, False)]),
    ('Wallander, 1990', [('Wallander', None, False)]),
    ('Series Name (Book 1920)', [('Series Name', None, False)]),
    ('---', []),
    ('[[[', []),
    ('...', []),
    ('#3', [('#3', None, True)]),
    ('3', [('3', None, True)]),
    ('()', [('()', None, True)]),
    (',', []),
    ('Foo,,, Bar', [('Foo, Bar', None, False)]),
    ('Foo ;; Bar', [('Foo', None, False), ('Bar', None, False)]),
    ('Foo -- ', [('Foo', None, False)]),
    (' Foo  --  3 ', [('Foo', '3', False)]),
    ('Foo — 3', [('Foo', '3', False)]),
    ('Foo – Bar', [('Foo - Bar', None, False)]),
    ('Rizzoli & Isles, 12', [('Rizzoli & Isles', '12', False)]),
    ('Kommissar Dupin, Fall 3', [('Kommissar Dupin', '3', False)]),
    ('Die Chroniken von Narnia, Teil 1', [('Die Chroniken von Narnia', '1', False)]),
    ('Abby Cooper, Psychic Eye Mystery, 7', [('Abby Cooper, Psychic Eye Mystery', '7', False)]),
    ('Dresden Files, The, 10', [('Dresden Files, The', '10', False)]),
    ('Dresden Files, The #10', [('Dresden Files, The', '10', False)]),
    ('Foo Series #3', [('Foo Series', '3', False)]),
    ('Foo Saga, 2', [('Foo Saga', '2', False)]),
    ('Foo (Series, #2)', [('Foo (Series)', '2', False)]),
    ('Foo (Series #2)', [('Foo (Series)', '2', False)]),
    ('Foo (Bar, #2)', [('Foo (Bar)', '2', False)]),
    ('Foo (Bar)', [('Foo (Bar)', None, False)]),
    ('Foo (Bar); Baz', [('Foo (Bar)', None, False), ('Baz', None, False)]),
    ('Kluftinger, 4; Piper', [('Kluftinger', '4', False), ('Piper', None, True)]),
    ('Kluftinger, 4 ; Piper Verlag', [('Kluftinger', '4', False), ('Piper Verlag', None, True)]),
    ('Foo (Bar #3)', [('Foo (Bar)', '3', False)]),
    ('Foo (Series, #2)', [('Foo (Series)', '2', False)]),
    ('Foo (Bar, Book 2)', [('Foo (Bar)', '2', False)]),
    ("'Foo'", [('Foo', None, False)]),
    ('„Foo“', [('Foo', None, False)]),
    ('«Foo»', [('Foo', None, False)]),
    ('Foo (Bar #3) x', [('Foo (Bar #3) x', None, False)]),
]


def _shape(raw):
    return [(c.name, c.position, bool(c.ignored)) for c in parse_series(raw)]


@pytest.mark.parametrize("raw,expected", PRODUCTION, ids=[r[:40] for r, _ in PRODUCTION])
def test_production_strings_verbatim(raw, expected):
    assert _shape(raw) == expected


@pytest.mark.parametrize("raw,expected", REALISTIC, ids=[f"{i}:{r[:30]}" for i, (r, _) in enumerate(REALISTIC)])
def test_realistic_strings(raw, expected):
    assert _shape(raw) == expected


def test_table_has_the_promised_breadth():
    assert len(REALISTIC) >= 80 and len(PRODUCTION) >= 12


def test_rejected_positions_keep_the_raw_value_for_audit():
    cands = parse_series("The Anetakis Tycoons - 1920")
    assert cands[0].position is None and cands[0].raw_position == "1920" and cands[0].position_problem == "year_like"
    k = parse_series("Mills & Boon Desire 2-in-1 - 322")[0]
    assert k.raw_position == "322" and k.position_problem == "above_max" and k.ignored == "publisher"


def test_a_comma_inside_a_name_is_not_a_split():
    assert [c.name for c in parse_series("Mistborn, Era 2: Wax & Wayne")] == ["Mistborn, Era 2: Wax & Wayne"]
    assert [(c.name, c.position) for c in parse_series("Alex Cross, Book 3")] == [("Alex Cross", "3")]
    assert [c.name for c in parse_series("Smith, John")] == ["Smith, John"]


@pytest.mark.parametrize("pos,problem", [
    ("1", None), ("0", None), ("0.5", None), ("200", None), ("201", "above_max"), ("1920", "year_like"),
    ("2005", "year_like"), ("60291", "too_many_digits"), ("27598", "too_many_digits"), ("9783453", "too_many_digits"),
    ("9783453567890", "isbn_like"), ("-3", "negative"), ("", "empty"), ("abc", "empty"), ("1-3", None), ("1-300", "above_max"),
])
def test_position_sanity(pos, problem):
    assert seriesnorm.position_problem(pos) == problem


def test_position_cap_is_a_setting():
    assert seriesnorm.position_problem("250") == "above_max"
    assert seriesnorm.position_problem("250", 300) is None
    assert seriesnorm.position_problem("1920", 999) == "year_like"  # years stay rejected whatever the cap
    assert [c.position for c in parse_series("Big Series #250", max_position=300)] == ["250"]
    assert [c.position for c in parse_series("Big Series #250")] == [None]


def test_roman_numerals_only_with_a_keyword():
    assert seriesnorm.roman_to_int("IV") == 4 and seriesnorm.roman_to_int("XXXIX") == 39
    assert seriesnorm.roman_to_int("IIII") is None and seriesnorm.roman_to_int("MIX") is None
    assert _shape("Foo, Book IV") == [("Foo", "4", False)]
    assert _shape("Henry VIII") == [("Henry VIII", None, False)]


def test_decimals_are_kept():
    assert _shape("Foo #2.5") == [("Foo", "2.5", False)]
    assert seriesnorm.normalize_position("2.50") == "2.5" and seriesnorm.normalize_position("007") == "7"


def test_ignored_names_setting_extends_the_denylist():
    assert [c.ignored for c in parse_series("Fantasy Klassiker")] == [None]
    assert [c.ignored for c in parse_series("Fantasy Klassiker", ignored=["fantasy klassiker"])] == ["ignored"]
    assert [c.ignored for c in parse_series("Bastei Sonderband 7", ignored=["bastei sonder*"])] == ["ignored"]
    assert [c.ignored for c in parse_series("Harry Potter", ignored=["fantasy klassiker"])] == [None]


def test_is_junk_covers_the_publisher_table_and_still_spares_real_series():
    for junk in ("Knaur", "Goldmann", "rororo thriller", "Mills & Boon Desire", "Silhouette Desire", "Penguin Classics",
                 "Le livre de poche", "Bestselling Author Collection", "Harlequin Presents"):
        assert seriesnorm.is_junk(junk), junk
    for ok in ("The Expanse", "Piper Chase", "Tor Teen", "Percy Jackson & the Olympians", "Die drei ???", "Pocket Full of Rye"):
        assert not seriesnorm.is_junk(ok), ok


def test_split_position_still_works_and_no_longer_eats_hyphenated_titles():
    assert seriesnorm.split_position("Molly Murphy mysteries -- [16]") == ("Molly Murphy mysteries", "16")
    assert seriesnorm.split_position("Catch-22") == ("Catch-22", None)


def test_parser_never_raises_and_never_returns_an_empty_name():
    rng = random.Random(1914)
    alphabet = string.ascii_letters + string.digits + " ,;:-–—()[]\"'#.&/\\|?!…«»“”\t\n" + "äöüßéèçÅ中"
    samples = ["", " ", None, 5, [], "\x00", "(((((", ")))))", "[]()", "#", "- - -", ",;,;", "Book", "Vol. ", "3 of", "…", "a" * 5000]
    for _ in range(3000):
        samples.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60))))
    for raw in samples:
        cands = parse_series(raw)
        assert isinstance(cands, list)
        for c in cands:
            assert c.name and c.name.strip() == c.name
            assert c.position is None or seriesnorm.position_problem(c.position) is None
