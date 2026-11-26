"""Unit tests for the pure Library-filter helpers in static/app.js (evaluated with node).

The helpers sit between the <lib-filter-helpers> markers so they can be exercised without a
browser; scripts/ui-library-filters.mjs covers the wiring in a real one."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(expr: str):
    src = APP_JS.read_text()
    block = re.search(r"// <lib-filter-helpers>.*?// </lib-filter-helpers>", src, re.S)
    assert block, "helper block markers are missing from app.js"
    out = subprocess.run(["node", "-e", f"{block.group(0)}\nconsole.log(JSON.stringify({expr}))"],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_defaults_are_all_empty():
    assert set(_run("libFilterDefaults()").values()) == {""}


def test_empty_state_has_no_active_filters():
    assert _run("libActiveFilters(libFilterDefaults())") == []


def test_empty_query_is_not_a_filter_never_the_literal_string():
    assert _run("libActiveFilters({...libFilterDefaults(), q:''})") == []
    assert _run("libActiveFilters({...libFilterDefaults(), q:'x'}).length") == 1


def test_active_filters_use_labels_and_keep_order():
    chips = _run("libActiveFilters({q:'dune',kind:'ebook',status:'',seriesId:'s1',tagId:'',readStatus:'finished'},"
                 " (k,v)=>({kind:'Ebooks',seriesId:'The 39 Clues'}[k]))")
    assert [c["key"] for c in chips] == ["q", "kind", "seriesId", "readStatus"]
    assert chips[0]["label"] == "Search: “dune”"
    assert chips[1]["label"] == "Media: Ebooks"
    assert chips[2]["label"] == "Series: The 39 Clues"
    assert chips[3]["label"] == "Reading: finished"   # no label lookup: falls back to the raw value


def test_hash_without_filters():
    assert _run("parseLibraryHash('library')") == {"path": "library", "filters": None}
    assert _run("parseLibraryHash('library/abc/read/epub')")["filters"] is None


def test_hash_filters_are_parsed_and_decoded():
    r = _run("parseLibraryHash('library?q=dead%20world&tag=T1&series=S1&read=reading&kind=ebook&status=missing')")
    assert r == {"path": "library", "filters": {"q": "dead world", "tagId": "T1", "seriesId": "S1",
                                                "readStatus": "reading", "kind": "ebook", "status": "missing"}}


def test_hash_ignores_blank_unknown_and_invalid_values():
    assert _run("parseLibraryHash('library?q=%20&foo=1')")["filters"] is None
    assert _run("parseLibraryHash('library?kind=video&status=lost&q=a')")["filters"] == {"q": "a"}
