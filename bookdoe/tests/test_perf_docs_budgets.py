"""The search budgets in docs/OPERATIONS.md must equal those asserted by the perf suite."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

import test_large_library_perf as perf

DOC = Path(__file__).resolve().parents[2] / "docs" / "OPERATIONS.md"
REPORT = DOC.with_name("P4-08_REPORT.md")
# The internal report is not part of the public snapshot; its guards then have nothing to check.
needs_report = pytest.mark.skipif(not REPORT.is_file(), reason="internal report not shipped")


def _doc_budgets():
    rows = {}
    for line in DOC.read_text().splitlines():
        m = re.match(r"\|\s*(.+?)\s*\|\s*([\d.]+)s\s*\|$", line)
        if m:
            rows[m.group(1)] = float(m.group(2))
    return rows


def test_docs_search_budgets_match_perf_test():
    rows = _doc_budgets()
    assert rows["Library text search (`/api/v1/library?q=`)"] == perf.LIBRARY_SEARCH_BUDGET_S
    assert rows["Global search (`/api/v1/search?q=`)"] == perf.GLOBAL_SEARCH_BUDGET_S


def test_search_budgets_are_distinct():
    # Equal constants would let a global-row/library-constant mix-up pass the test above.
    assert perf.LIBRARY_SEARCH_BUDGET_S != perf.GLOBAL_SEARCH_BUDGET_S


# A citation is a line starting `tests/test_perf_docs_budgets.py::<name>`, either alone
# (the `--collect-only -q` form) or followed by a status (the `-v` form, e.g. `... PASSED [ 20%]`).
CITATION = re.compile(r"^tests/test_perf_docs_budgets\.py::(\w+)(?=\s|$)", re.MULTILINE)


def _cited(report):
    return set(CITATION.findall(report))


def _collected_names():
    # Pytest collects module-level callables named test_*; match against those, not source text.
    module = sys.modules[__name__]
    return {n for n, v in vars(module).items() if n.startswith("test_") and callable(v)}


def _listed_test_names(text):
    # Names of defined tests that appear whole in `text`; prose containing "test_" alone is not a listing.
    return {n for n in _collected_names() if re.search(rf"\b{re.escape(n)}\b", text)}


def test_module_docstring_does_not_list_test_names():
    # A hand-kept list of test names in the docstring drifts when tests are renamed.
    doc = __doc__
    assert doc and doc.strip(), "module docstring is missing"
    listed = _listed_test_names(doc)
    assert not listed, f"docstring lists test names: {sorted(listed)}"


def test_listed_test_names_flags_defined_names_only():
    # The guard must catch a real listing, ignore prose about "test_" and undefined names.
    assert _listed_test_names("Covers test_search_budgets_are_distinct.") == {"test_search_budgets_are_distinct"}
    assert _listed_test_names("Names start with test_ by convention.") == set()
    assert _listed_test_names("Covers test_renamed_away.") == set()
    assert _listed_test_names("x_test_search_budgets_are_distinct_y") == set()


@needs_report
def test_report_cites_only_existing_tests():
    report = REPORT.read_text()
    cited = _cited(report)
    assert cited
    assert cited <= _collected_names()


def test_citation_matching_rejects_unknown_and_unanchored_names():
    line = "tests/test_perf_docs_budgets.py::test_search_budgets_are_distinct"
    assert _cited(line) == {"test_search_budgets_are_distinct"}
    assert _cited(f"{line} PASSED [ 40%]") == {"test_search_budgets_are_distinct"}  # -v form
    assert _cited(f"see {line}") == set()  # not at line start: ignored
    assert _cited(f"{line}.") == set()  # prose with trailing punctuation: ignored
    assert _cited("tests/test_perf_docs_budgets.py::test_renamed_away") - _collected_names()


@needs_report
def test_report_does_not_name_a_pull_request_body_for_154():
    report = " ".join(REPORT.read_text().split())  # rewrapping must not matter
    assert "PR body for #" + "154" not in report  # split so this source does not match itself
    sentence = "a pull request body may copy it, but nothing here depends on that."
    heading = report.index("### Acceptance mapping")
    table = report.index("| Criterion | Proof |")
    assert heading < report.index(sentence) < table


def _report_docstring_proof_block():
    # The fenced output block that follows the `ast.get_docstring` command block in the report.
    lines = REPORT.read_text().splitlines()
    command = next(n for n, line in enumerate(lines) if "ast.get_docstring" in line)
    fences = [n for n, line in enumerate(lines) if line.startswith("```") and n > command]
    close_cmd, open_out, close_out = fences[:3]
    return "\n".join(lines[open_out + 1 : close_out])


@needs_report
def test_report_docstring_proof_matches_module_docstring():
    # The pasted proof is not trusted as text: it must equal the live docstring and keep grep -c 'test_' at 0.
    block = _report_docstring_proof_block()
    assert block == __doc__.strip()
    assert "test_" not in block


@needs_report
def test_report_phrase_is_greppable_on_a_single_line():
    text = REPORT.read_text().splitlines()
    lines = [n for n, line in enumerate(text, 1) if "pull request body may copy" in line]
    # `grep -n 'pull request body may copy'` must return a match line
    assert lines, "phrase wrapped across lines in the report"
    heading = next((n for n, line in enumerate(text, 1) if line.startswith("### Acceptance mapping")), None)
    table = next((n for n, line in enumerate(text, 1) if line.strip() == "| Criterion | Proof |"), None)
    assert heading is not None, "report has no '### Acceptance mapping' heading"
    assert table is not None, "report has no '| Criterion | Proof |' table header"
    assert heading < lines[0] < table, "phrase must sit between the heading and the table"

