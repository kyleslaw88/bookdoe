"""Tests for ``services.matchevidence.evidence_for()``.

Covers the three acceptance criteria from issue #267:
1. Exact-ISBN-matched detected/work pair → identifier=exact, title/author states derived correctly.
2. Title-only local match → identifier=unavailable, author/year states (including conflict when years differ by >1).
3. Purity: no DB access, no network, no side effects; identical inputs → identical outputs.
"""
from __future__ import annotations

import copy

import pytest

from app.services import matchevidence as me
from app.services import search_candidates as sc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _detected(isbn=None, title="Test Book", authors=None, series=None,
              series_position=None, publication_date=None, narrators=None,
              duration_seconds=None, identifiers=None, evidence=None,
              provider_evidence=None, embedded_tags=None):
    """Build a minimal detected dict."""
    d = {
        "title": title,
        "authors": authors or ["Jane Author"],
        "identifiers": identifiers or ({"isbn13": isbn} if isbn else {}),
        "series": series or [],
        "series_position": series_position,
        "publication_date": publication_date,
        "narrators": narrators or [],
        "duration_seconds": duration_seconds,
        "evidence": evidence or {},
        "provider_evidence": provider_evidence,
        "embedded_tags": embedded_tags,
    }
    return d


def _work_row(title="Test Book", authors=None, series=None,
              series_position=None, first_publish_date=None,
              identifiers=None, narrators=None, duration_seconds=None):
    """Build a minimal work row dict."""
    w = {
        "title": title,
        "authors": authors or ["Jane Author"],
        "series": series or [],
        "series_position": series_position,
        "first_publish_date": first_publish_date,
        "identifiers": identifiers or [],
        "narrators": narrators or [],
        "duration_seconds": duration_seconds,
    }
    return w


# ---------------------------------------------------------------------------
# AC1: Exact-ISBN match
# ---------------------------------------------------------------------------

class TestExactIsbnMatch:
    """AC1: evidence_for() given an exact-ISBN-matched detected/work pair
    returns identifier=exact, title/author states derived correctly."""

    def test_identifier_exact_on_isbn13_match(self):
        detected = _detected(isbn="9781234567897", title="Test Book",
                             authors=["Jane Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"],
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        id_sig = next(s for s in signals if s["signal"] == me.SIGNAL_IDENTIFIER)
        assert id_sig["state"] == me.STATE_EXACT
        assert id_sig["detail"] == "exact isbn13 match"
        assert id_sig["weight"] == 1.0

    def test_title_exact_on_isbn_match(self):
        detected = _detected(isbn="9781234567897", title="Test Book",
                             authors=["Jane Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"],
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        title_sig = next(s for s in signals if s["signal"] == me.SIGNAL_TITLE)
        assert title_sig["state"] == me.STATE_EXACT

    def test_author_exact_on_isbn_match(self):
        detected = _detected(isbn="9781234567897", title="Test Book",
                             authors=["Jane Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"],
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        author_sig = next(s for s in signals if s["signal"] == me.SIGNAL_AUTHOR)
        assert author_sig["state"] == me.STATE_EXACT

    def test_all_signals_present(self):
        detected = _detected(isbn="9781234567897")
        work = _work_row(identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        signal_names = {s["signal"] for s in signals}
        for sig in me.ALL_SIGNALS:
            assert sig in signal_names, f"Missing signal: {sig}"

    def test_year_exact_when_same(self):
        detected = _detected(isbn="9781234567897", publication_date="2020-01-15")
        work = _work_row(first_publish_date="2020-06-01",
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        year_sig = next(s for s in signals if s["signal"] == me.SIGNAL_YEAR)
        assert year_sig["state"] == me.STATE_EXACT

    def test_year_similar_when_within_one_year(self):
        detected = _detected(isbn="9781234567897", publication_date="2020-01-15")
        work = _work_row(first_publish_date="2021-06-01",
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        year_sig = next(s for s in signals if s["signal"] == me.SIGNAL_YEAR)
        assert year_sig["state"] == me.STATE_SIMILAR

    def test_year_conflict_when_differs_by_more_than_one(self):
        detected = _detected(isbn="9781234567897", publication_date="2020-01-15")
        work = _work_row(first_publish_date="2025-06-01",
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        year_sig = next(s for s in signals if s["signal"] == me.SIGNAL_YEAR)
        assert year_sig["state"] == me.STATE_CONFLICT

    def test_series_exact_when_matching(self):
        detected = _detected(isbn="9781234567897", series=["My Series"],
                             series_position=1)
        work = _work_row(series=["My Series"], series_position=1,
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        series_sig = next(s for s in signals if s["signal"] == me.SIGNAL_SERIES)
        assert series_sig["state"] == me.STATE_EXACT
        pos_sig = next(s for s in signals if s["signal"] == me.SIGNAL_SERIES_POSITION)
        assert pos_sig["state"] == me.STATE_EXACT


# ---------------------------------------------------------------------------
# AC2: Title-only local match
# ---------------------------------------------------------------------------

class TestTitleOnlyMatch:
    """AC2: evidence_for() given a title-only local match returns
    identifier=unavailable and author/year states (including conflict
    when years differ by more than 1)."""

    def test_identifier_unavailable_without_isbn(self):
        detected = _detected(title="Test Book", authors=["Jane Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"])
        signals = me.evidence_for(detected, work)
        id_sig = next(s for s in signals if s["signal"] == me.SIGNAL_IDENTIFIER)
        assert id_sig["state"] == me.STATE_UNAVAILABLE
        assert "no identifiers" in id_sig["detail"]

    def test_title_exact_on_title_only_match(self):
        detected = _detected(title="Test Book", authors=["Jane Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"])
        signals = me.evidence_for(detected, work)
        title_sig = next(s for s in signals if s["signal"] == me.SIGNAL_TITLE)
        assert title_sig["state"] == me.STATE_EXACT

    def test_author_exact_on_title_only_match(self):
        detected = _detected(title="Test Book", authors=["Jane Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"])
        signals = me.evidence_for(detected, work)
        author_sig = next(s for s in signals if s["signal"] == me.SIGNAL_AUTHOR)
        assert author_sig["state"] == me.STATE_EXACT

    def test_year_unavailable_when_no_dates(self):
        detected = _detected(title="Test Book")
        work = _work_row(title="Test Book")
        signals = me.evidence_for(detected, work)
        year_sig = next(s for s in signals if s["signal"] == me.SIGNAL_YEAR)
        assert year_sig["state"] == me.STATE_UNAVAILABLE

    def test_year_conflict_when_differs_by_more_than_one(self):
        detected = _detected(title="Test Book", publication_date="2020-01-15")
        work = _work_row(title="Test Book", first_publish_date="2025-06-01")
        signals = me.evidence_for(detected, work)
        year_sig = next(s for s in signals if s["signal"] == me.SIGNAL_YEAR)
        assert year_sig["state"] == me.STATE_CONFLICT
        assert "2020" in year_sig["detail"]
        assert "2025" in year_sig["detail"]

    def test_year_similar_when_within_one_year(self):
        detected = _detected(title="Test Book", publication_date="2020-01-15")
        work = _work_row(title="Test Book", first_publish_date="2021-06-01")
        signals = me.evidence_for(detected, work)
        year_sig = next(s for s in signals if s["signal"] == me.SIGNAL_YEAR)
        assert year_sig["state"] == me.STATE_SIMILAR

    def test_author_conflict_when_different(self):
        detected = _detected(title="Test Book", authors=["Alice Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"])
        signals = me.evidence_for(detected, work)
        author_sig = next(s for s in signals if s["signal"] == me.SIGNAL_AUTHOR)
        assert author_sig["state"] == me.STATE_CONFLICT

    def test_title_conflict_when_different(self):
        detected = _detected(title="Different Book", authors=["Jane Author"])
        work = _work_row(title="Test Book", authors=["Jane Author"])
        signals = me.evidence_for(detected, work)
        title_sig = next(s for s in signals if s["signal"] == me.SIGNAL_TITLE)
        assert title_sig["state"] == me.STATE_CONFLICT


# ---------------------------------------------------------------------------
# AC3: Purity
# ---------------------------------------------------------------------------

class TestPurity:
    """AC3: Function is pure — no DB access, no network, no side effects.
    Identical inputs always produce identical outputs."""

    def test_identical_inputs_produce_identical_outputs(self):
        detected = _detected(isbn="9781234567897", title="Test Book",
                             authors=["Jane Author"], publication_date="2020-01-15")
        work = _work_row(title="Test Book", authors=["Jane Author"],
                         first_publish_date="2020-06-01",
                         identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        result1 = me.evidence_for(detected, work)
        result2 = me.evidence_for(detected, work)
        assert result1 == result2

    def test_deep_copy_inputs_still_identical(self):
        detected = _detected(isbn="9781234567897")
        work = _work_row(identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        result1 = me.evidence_for(copy.deepcopy(detected), copy.deepcopy(work))
        result2 = me.evidence_for(copy.deepcopy(detected), copy.deepcopy(work))
        assert result1 == result2

    def test_no_db_import(self):
        """The module must not import db — it is pure."""
        assert "app.db" not in dir(me)
        assert "db" not in me.__dict__

    def test_no_network_import(self):
        """The module must not import httpx or requests."""
        assert "httpx" not in dir(me)
        assert "requests" not in dir(me)

    def test_signal_structure(self):
        """Every signal dict has exactly the four required keys."""
        detected = _detected(isbn="9781234567897")
        work = _work_row(identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        for sig in signals:
            assert set(sig.keys()) == {"signal", "state", "detail", "weight"}
            assert sig["state"] in me.VALID_STATES
            assert isinstance(sig["weight"], float)
            assert sig["weight"] > 0

    def test_narrator_unavailable_when_only_one_side_has_data(self):
        detected = _detected(narrators=["Alice Narrator"])
        work = _work_row()
        signals = me.evidence_for(detected, work)
        narr_sig = next(s for s in signals if s["signal"] == me.SIGNAL_NARRATOR)
        assert narr_sig["state"] == me.STATE_UNAVAILABLE

    def test_narrator_exact_when_both_sides_match(self):
        detected = _detected(narrators=["Alice Narrator"])
        work = _work_row(narrators=["Alice Narrator"])
        signals = me.evidence_for(detected, work)
        narr_sig = next(s for s in signals if s["signal"] == me.SIGNAL_NARRATOR)
        assert narr_sig["state"] == me.STATE_EXACT

    def test_duration_unavailable_when_only_one_side_has_data(self):
        detected = _detected(duration_seconds=3600)
        work = _work_row()
        signals = me.evidence_for(detected, work)
        dur_sig = next(s for s in signals if s["signal"] == me.SIGNAL_DURATION)
        assert dur_sig["state"] == me.STATE_UNAVAILABLE

    def test_duration_exact_when_both_sides_match(self):
        detected = _detected(duration_seconds=3600)
        work = _work_row(duration_seconds=3600)
        signals = me.evidence_for(detected, work)
        dur_sig = next(s for s in signals if s["signal"] == me.SIGNAL_DURATION)
        assert dur_sig["state"] == me.STATE_EXACT

    def test_duration_similar_within_tolerance(self):
        detected = _detected(duration_seconds=3600)
        work = _work_row(duration_seconds=3500)
        signals = me.evidence_for(detected, work)
        dur_sig = next(s for s in signals if s["signal"] == me.SIGNAL_DURATION)
        assert dur_sig["state"] == me.STATE_SIMILAR

    def test_duration_conflict_outside_tolerance(self):
        detected = _detected(duration_seconds=3600)
        work = _work_row(duration_seconds=1800)
        signals = me.evidence_for(detected, work)
        dur_sig = next(s for s in signals if s["signal"] == me.SIGNAL_DURATION)
        assert dur_sig["state"] == me.STATE_CONFLICT

    def test_inputs_not_mutated_by_evidence_for(self):
        """Deep-copy inputs before the call and assert they are unchanged afterwards."""
        detected = _detected(
            isbn="9781234567897",
            title="Test Book",
            authors=["Jane Author"],
            series=["My Series"],
            narrators=["Alice Narrator"],
            duration_seconds=3600,
            identifiers={"isbn13": "9781234567897"},
            evidence=[{"path": "book.epub"}],
            provider_evidence={"openreads": {"confidence": 0.99}},
            embedded_tags={"genre": "fiction"},
        )
        work = _work_row(
            title="Test Book",
            authors=["Jane Author"],
            series=["My Series"],
            first_publish_date="2020-06-01",
            identifiers=[{"namespace": "isbn13", "value": "9781234567897"}],
            narrators=["Alice Narrator"],
            duration_seconds=3600,
        )
        original_detected = copy.deepcopy(detected)
        original_work = copy.deepcopy(work)
        me.evidence_for(detected, work)
        assert detected == original_detected, "detected was mutated"
        assert work == original_work, "work was mutated"

    def test_state_set_matches_valid_states(self):
        """Every signal state is one of VALID_STATES (shared vocabulary).

        The state vocabulary is defined in search_candidates and imported by
        matchevidence; this test asserts equality (not mere subset) so that
        any drift between the two modules is caught immediately.
        """
        # The vocabulary is a single source of truth: search_candidates.
        assert set(me.VALID_STATES) == set(sc.VALID_STATES), (
            f"VALID_STATES differ: me={me.VALID_STATES} sc={sc.VALID_STATES}"
        )
        # Each individual STATE_* constant must match.
        assert me.STATE_EXACT == sc.STATE_EXACT
        assert me.STATE_SIMILAR == sc.STATE_SIMILAR
        assert me.STATE_CONFLICT == sc.STATE_CONFLICT
        assert me.STATE_UNAVAILABLE == sc.STATE_UNAVAILABLE
        # The emitted states from evidence_for() must be a subset of the
        # shared vocabulary (completeness check).
        detected = _detected(
            isbn="9781234567897",
            title="Test Book",
            authors=["Jane Author"],
            series=["My Series"],
            narrators=["Alice Narrator"],
            duration_seconds=3600,
            identifiers={"isbn13": "9781234567897"},
            evidence=[{"path": "book.epub"}],
            provider_evidence={"openreads": {"confidence": 0.99}},
            embedded_tags={"genre": "fiction"},
        )
        work = _work_row(
            title="Test Book",
            authors=["Jane Author"],
            series=["My Series"],
            first_publish_date="2020-06-01",
            identifiers=[{"namespace": "isbn13", "value": "9781234567897"}],
            narrators=["Alice Narrator"],
            duration_seconds=3600,
        )
        signals = me.evidence_for(detected, work)
        states = {sig["state"] for sig in signals}
        assert states.issubset(set(sc.VALID_STATES)), (
            f"Unexpected states found: {states - set(sc.VALID_STATES)}"
        )

    def test_provider_agreement_unavailable_without_evidence(self):
        detected = _detected()
        work = _work_row()
        signals = me.evidence_for(detected, work)
        pa_sig = next(s for s in signals if s["signal"] == me.SIGNAL_PROVIDER_AGREEMENT)
        assert pa_sig["state"] == me.STATE_UNAVAILABLE

    def test_provider_agreement_similar_with_evidence(self):
        """Presence of provider evidence is a positive signal, not an exact match."""
        detected = _detected(provider_evidence={"openreads": {"confidence": 0.99}})
        work = _work_row()
        signals = me.evidence_for(detected, work)
        pa_sig = next(s for s in signals if s["signal"] == me.SIGNAL_PROVIDER_AGREEMENT)
        assert pa_sig["state"] == me.STATE_SIMILAR

    def test_embedded_metadata_unavailable_without_tags(self):
        detected = _detected(evidence={})
        work = _work_row()
        signals = me.evidence_for(detected, work)
        em_sig = next(s for s in signals if s["signal"] == me.SIGNAL_EMBEDDED_METADATA)
        assert em_sig["state"] == me.STATE_UNAVAILABLE

    def test_directory_structure_unavailable_without_evidence(self):
        detected = _detected(evidence={})
        work = _work_row()
        signals = me.evidence_for(detected, work)
        ds_sig = next(s for s in signals if s["signal"] == me.SIGNAL_DIRECTORY_STRUCTURE)
        assert ds_sig["state"] == me.STATE_UNAVAILABLE

    def test_embedded_metadata_similar_with_tags(self):
        """Presence of embedded tags is a positive signal, not an exact match."""
        detected = _detected(embedded_tags={"genre": "fiction"})
        work = _work_row()
        signals = me.evidence_for(detected, work)
        em_sig = next(s for s in signals if s["signal"] == me.SIGNAL_EMBEDDED_METADATA)
        assert em_sig["state"] == me.STATE_SIMILAR

    def test_directory_structure_similar_with_evidence(self):
        """Presence of directory structure evidence is a positive signal, not an exact match."""
        detected = _detected(evidence=[{"path": "book.epub"}])
        work = _work_row()
        signals = me.evidence_for(detected, work)
        ds_sig = next(s for s in signals if s["signal"] == me.SIGNAL_DIRECTORY_STRUCTURE)
        assert ds_sig["state"] == me.STATE_SIMILAR

    def test_identifier_conflict_when_namespace_overlaps_but_value_differs(self):
        """Detected identifiers sharing a namespace with the work but differing in value yield conflict."""
        detected = _detected(identifiers={"isbn13": "9780000000000"})
        work = _work_row(identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        id_sig = next(s for s in signals if s["signal"] == me.SIGNAL_IDENTIFIER)
        assert id_sig["state"] == me.STATE_CONFLICT
        assert "share namespace" in id_sig["detail"]

    def test_identifier_unavailable_when_no_namespace_overlap(self):
        """Detected identifiers with no overlapping namespace yield unavailable."""
        detected = _detected(identifiers={"custom_id": "abc123"})
        work = _work_row(identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        id_sig = next(s for s in signals if s["signal"] == me.SIGNAL_IDENTIFIER)
        assert id_sig["state"] == me.STATE_UNAVAILABLE

    def test_isbn10_match(self):
        detected = _detected(identifiers={"isbn10": "0000000010"})
        work = _work_row(identifiers=[{"namespace": "isbn10", "value": "0000000010"}])
        signals = me.evidence_for(detected, work)
        id_sig = next(s for s in signals if s["signal"] == me.SIGNAL_IDENTIFIER)
        assert id_sig["state"] == me.STATE_EXACT

    def test_asin_match(self):
        detected = _detected(identifiers={"asin": "B000000ASIN"})
        work = _work_row(identifiers=[{"namespace": "asin", "value": "B000000ASIN"}])
        signals = me.evidence_for(detected, work)
        id_sig = next(s for s in signals if s["signal"] == me.SIGNAL_IDENTIFIER)
        assert id_sig["state"] == me.STATE_EXACT

    def test_title_similarity_substring(self):
        detected = _detected(title="Test Book: A Novel")
        work = _work_row(title="Test Book")
        signals = me.evidence_for(detected, work)
        title_sig = next(s for s in signals if s["signal"] == me.SIGNAL_TITLE)
        assert title_sig["state"] == me.STATE_SIMILAR

    def test_author_partial_overlap(self):
        detected = _detected(authors=["Jane Author", "Bob Coauthor"])
        work = _work_row(authors=["Jane Author"])
        signals = me.evidence_for(detected, work)
        author_sig = next(s for s in signals if s["signal"] == me.SIGNAL_AUTHOR)
        assert author_sig["state"] == me.STATE_SIMILAR

    def test_series_conflict_when_different(self):
        detected = _detected(series=["Series A"])
        work = _work_row(series=["Series B"])
        signals = me.evidence_for(detected, work)
        series_sig = next(s for s in signals if s["signal"] == me.SIGNAL_SERIES)
        assert series_sig["state"] == me.STATE_CONFLICT

    def test_series_position_conflict(self):
        detected = _detected(series=["My Series"], series_position=1)
        work = _work_row(series=["My Series"], series_position=2)
        signals = me.evidence_for(detected, work)
        pos_sig = next(s for s in signals if s["signal"] == me.SIGNAL_SERIES_POSITION)
        assert pos_sig["state"] == me.STATE_CONFLICT

    def test_work_authors_as_plain_list(self):
        """Work row with plain string authors (not dicts)."""
        detected = _detected(authors=["Jane Author"])
        work = _work_row(authors=["Jane Author"])
        signals = me.evidence_for(detected, work)
        author_sig = next(s for s in signals if s["signal"] == me.SIGNAL_AUTHOR)
        assert author_sig["state"] == me.STATE_EXACT

    def test_weight_values_are_reasonable(self):
        """Identifier has highest weight; others are lower."""
        detected = _detected(isbn="9781234567897")
        work = _work_row(identifiers=[{"namespace": "isbn13", "value": "9781234567897"}])
        signals = me.evidence_for(detected, work)
        weights = {s["signal"]: s["weight"] for s in signals}
        assert weights[me.SIGNAL_IDENTIFIER] == 1.0
        assert weights[me.SIGNAL_TITLE] == 0.3
        assert weights[me.SIGNAL_AUTHOR] == 0.2
        assert weights[me.SIGNAL_NARRATOR] == 0.1
        assert weights[me.SIGNAL_DURATION] == 0.05
