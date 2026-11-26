"""Pure-function tests for the series-name cleanup heuristics."""
from app.services import seriesnorm


def test_is_junk_flags_multi_segment_library_classification_strings():
    assert seriesnorm.is_junk("Heyne-Bucher -- 01. -- Heyne allgemeine Reihe -- Nr. 10624")


def test_is_junk_flags_known_imprint_words_regardless_of_case():
    assert seriesnorm.is_junk("Thorndike Press Large Print")
    assert seriesnorm.is_junk("HEYNE allgemeine Reihe")


def test_is_junk_leaves_real_series_alone():
    assert not seriesnorm.is_junk("The 39 Clues")
    assert not seriesnorm.is_junk("Harry Potter")
    assert not seriesnorm.is_junk("A Song of Ice and Fire")


def test_split_position_extracts_spelled_out_word_numbers():
    assert seriesnorm.split_position("39 Clues: Unstoppable -- book two") == ("39 Clues: Unstoppable", "2")
    assert seriesnorm.split_position("39 Clues: Doublecross -- book one") == ("39 Clues: Doublecross", "1")


def test_split_position_extracts_digit_and_bracket_forms():
    assert seriesnorm.split_position("Molly Murphy mysteries -- [16]") == ("Molly Murphy mysteries", "16")
    assert seriesnorm.split_position("A Royal Spyness Mystery (6)") == ("A Royal Spyness Mystery", "6")


def test_split_position_leaves_a_clean_name_alone():
    assert seriesnorm.split_position("The 39 Clues") == ("The 39 Clues", None)


def test_norm_key_merges_spelling_variants_but_not_different_numbers():
    assert seriesnorm.norm_key("A Molly Murphy mystery") == seriesnorm.norm_key("The Molly Murphy mysteries")
    assert seriesnorm.norm_key("Wings of Fire Book 1") != seriesnorm.norm_key("Wings of Fire Book 2")


def test_similar_never_merges_different_digits():
    assert not seriesnorm.similar(seriesnorm.norm_key("Book 1"), seriesnorm.norm_key("Book 2"))


def test_split_position_handles_a_bracketed_whole_label():
    assert seriesnorm.split_position("39 clues. Superspecial -- [bk. 1]") == ("39 clues. Superspecial", "1")


def test_split_position_tolerates_a_trailing_full_stop():
    assert seriesnorm.split_position("Bill Hodges trilogy -- 3.") == ("Bill Hodges trilogy", "3")


def test_is_junk_flags_a_trailing_shelfmark_code():
    assert seriesnorm.is_junk("Fawcett premier book -- R333")
    assert seriesnorm.is_junk("Hard Case Crime novel -- HCC")
    assert seriesnorm.is_junk("Signet -- AE 4293")
    assert seriesnorm.is_junk("Best seller -- 593/3")


def test_is_junk_does_not_flag_a_real_trailing_word():
    assert not seriesnorm.is_junk("An Example Series -- Dark Hunter")
    assert not seriesnorm.is_junk("Trackers -- Livre 2")


import pytest

LIVE_JUNK = ["#20056/1", "(Gollancz detection)", "(Gollancz SF)", "100 pages", "1028", "10|18"]


@pytest.mark.parametrize("name", LIVE_JUNK + ["12 pages", "#7", "20056/1", "Novels by Terry Pratchett", "novels by Jo Nesbo"])
def test_is_junk_flags_the_live_production_junk_and_spec_patterns(name):
    assert seriesnorm.is_junk(name)


@pytest.mark.parametrize("name", [
    "The Expanse", "Discworld", "39 Clues", "Expeditionary Force", "Dune",
    "Harry Potter", "Wheel of Time", "Fahrenheit 451 Saga", "Novels of the Malazan Empire",
    "Ender's Game (Ender Quintet)", "The 100",
])
def test_is_junk_keeps_real_series_with_digits_or_parentheses(name):
    assert not seriesnorm.is_junk(name)
