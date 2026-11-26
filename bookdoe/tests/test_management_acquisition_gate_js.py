"""Management UI hides every acquisition surface when /api/v1/auth/me says acquisition_enabled is false (#503).

Evaluated with node against the helper block in static/app.js: no browser, no network."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

ASSET = {"id": "a1", "kind": "ebook", "status": "missing", "monitored": True}
TABS = [["roots", "Library roots"], ["indexers", "Indexers"], ["clients", "Download clients"], ["naming", "Naming"], ["jobs", "Jobs"]]
JOBS = [{"kind": k} for k in ("download-poll", "wanted-search", "upgrade-search", "author-discovery", "scan")]


def _run(expr: str):
    src = APP_JS.read_text()
    block = re.search(r"// <acquisition-gate>.*?// </acquisition-gate>", src, re.S)
    nav = re.search(r"^const nav = \[.*?^\];", src, re.S | re.M)
    assert block and nav, "acquisition-gate block or nav is missing from app.js"
    prelude = ("const stat = (l, v) => `<stat>${l}</stat>`; const queueMini = () => '<mini></mini>';\n"
               f"const ASSET = {json.dumps(ASSET)}, TABS = {json.dumps(TABS)}, JOBS = {json.dumps(JOBS)};\n")
    out = subprocess.run(["node", "-e", f"{prelude}{nav.group(0)}\n{block.group(0)}\nconsole.log(JSON.stringify({expr}))"],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _routes(on):
    return _run(f"nav.filter(n => n[1] !== null && routeVisible(n[0], {json.dumps(on)})).map(n => n[0])")


def test_flag_read_from_auth_me_payload():
    assert _run("[acquisitionOn({acquisition_enabled: true}), acquisitionOn({acquisition_enabled: false}), acquisitionOn({}), acquisitionOn(null)]") == [True, False, False, False]


def test_flag_off_has_no_acquisition_nav():
    routes = _routes(False)
    for gone in ("wanted", "queue", "requests", "calendar"):
        assert gone not in routes
    assert {"dashboard", "library", "settings", "discover", "inbox"} <= set(routes)


def test_flag_on_keeps_the_same_nav():
    assert _routes(True) == _run("nav.filter(n => n[1] !== null).map(n => n[0])")
    assert {"wanted", "queue", "requests", "calendar"} <= set(_routes(True))


def test_flag_off_has_no_indexer_or_download_client_settings():
    assert [t[0] for t in _run("settingsTabsFor(TABS, false)")] == ["roots", "naming", "jobs"]


def test_flag_on_keeps_all_settings_tabs():
    assert _run("settingsTabsFor(TABS, true)") == TABS


def test_flag_off_hides_acquisition_jobs():
    assert [j["kind"] for j in _run("jobsFor(JOBS, false)")] == ["scan"]
    assert len(_run("jobsFor(JOBS, true)")) == 5


def test_flag_off_has_no_search_monitor_or_wanted_state_on_a_book():
    assert _run("assetAcqButtonsHtml(ASSET, false)") == ""
    assert _run("monitoredBadgeHtml({monitored: true}, false)") == ""
    assert _run("wantedLabel(ASSET, false)") is False


def test_flag_on_has_search_monitor_and_wanted_state_on_a_book():
    html = _run("assetAcqButtonsHtml(ASSET, true)")
    assert "asset-search" in html and "asset-monitor" in html
    assert "Monitored" in _run("monitoredBadgeHtml({monitored: true}, true)")
    assert _run("wantedLabel(ASSET, true)") is True


def test_flag_off_dashboard_has_no_wanted_counts_queue_or_poll():
    parts = _run("dashboardAcqParts(false, {}, {}, stat, queueMini)")
    assert parts == {"actions": "", "stats": "", "queue": ""}


def test_flag_on_dashboard_shows_wanted_counts_queue_and_poll():
    parts = _run("dashboardAcqParts(true, {}, {queue: []}, stat, queueMini)")
    assert "poll-now" in parts["actions"]
    assert "Wanted audio" in parts["stats"] and "Wanted ebooks" in parts["stats"]
    assert 'data-route="queue"' in parts["queue"]


def test_init_auth_copies_flag_from_me_payload_onto_state_me():
    """/auth/me carries the flag at the top level, not inside `user`; state.me must receive it."""
    src = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text()
    assert "state.me={...me.user, acquisition_enabled:!!me.acquisition_enabled}" in src
