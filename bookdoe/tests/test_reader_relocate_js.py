"""Web reader: a failing 'relocated' listener must not wedge page turns (issue #712). Evaluated with node."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONSUME_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "consume.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(expr: str):
    block = re.search(r"// <reader-relocate>.*?// </reader-relocate>", CONSUME_JS.read_text(), re.S)
    assert block, "reader-relocate markers are missing from consume.js"
    out = subprocess.run(["node", "-e", f"{block.group(0)}\nconsole.log(JSON.stringify({expr}))"],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_percent_is_null_when_epubjs_throws_on_a_cfi():
    book = "{locations: {length: () => 5, percentageFromCfi: () => { throw new Error('bad cfi'); }}}"
    assert _run(f"readerPercent({book}, 'x')") is None


def test_percent_is_null_before_locations_exist_and_passes_through_after():
    assert _run("readerPercent({locations: {length: () => 0}}, 'x')") is None
    assert _run("readerPercent({locations: {length: () => 5, percentageFromCfi: () => 0.25}}, 'x')") == 0.25


def test_throwing_relocated_listener_does_not_propagate():
    expr = "(() => { const h = safeRelocated(() => { throw new Error('boom'); }); h({start: {cfi: 'c'}}); return 'ok'; })()"
    assert _run(expr) == "ok"


def test_listener_runs_for_a_location_and_is_skipped_without_cfi():
    expr = "(() => { const seen = []; const h = safeRelocated(l => seen.push(l.start.cfi)); h({start: {cfi: 'c'}}); h({}); h(null); return seen; })()"
    assert _run(expr) == ["c"]
