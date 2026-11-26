"""Every title in the demo seed has a row in docs/DEMO_CONTENT.md (#745)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED_TITLES = Path(__file__).parent / "fixtures" / "demo_seed_titles.json"
DOC = ROOT / "docs" / "DEMO_CONTENT.md"


def _norm(s):
    return re.sub(r"\s+", " ", s.replace("’", "'")).strip().lower()


def _rows(text):
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and not set(cells[0]) <= {"-", " "}:
            rows.append(cells)
    return [r for r in rows if _norm(r[0]) != "title"]


def missing_titles(titles, doc_text):
    first_cells = [_norm(r[0]) for r in _rows(doc_text)]
    return [t for t in titles if not any(_norm(t) in c for c in first_cells)]


def test_every_seed_title_has_a_row():
    titles = json.loads(SEED_TITLES.read_text())
    assert titles
    assert missing_titles(titles, DOC.read_text()) == []


def test_rows_have_source_and_licence():
    for title, kind, source, licence in _rows(DOC.read_text()):
        assert title and kind and source and licence, title


def test_missing_row_is_detected():
    assert missing_titles(["No Such Title"], DOC.read_text()) == ["No Such Title"]


def test_table_covers_every_content_type():
    kinds = " ".join(r[1].lower() for r in _rows(DOC.read_text()))
    for needed in ("epub", "pdf", "audiobook", "podcast", "both"):
        assert needed in kinds
