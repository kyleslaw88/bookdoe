"""The product name is one setting (settings.app_name, default "bookdoe"), filled into the
shells, manifest and service worker by the real routes in main.py."""
from __future__ import annotations

import dataclasses
import json
import re
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, main as main_module
from app.main import app

STATIC = Path(__file__).parent.parent / "app" / "static"
REPO = Path(__file__).resolve().parents[2]
PAGES = ("/app", "/manage", "/setup")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def relabel(monkeypatch):
    def set_name(name):
        monkeypatch.setattr(main_module, "settings", dataclasses.replace(main_module.settings, app_name=name))
    return set_name


def _head_name(html: str) -> str:
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1)


def test_default_name_is_bookdoe(monkeypatch):
    monkeypatch.delenv("BOOKDOE_APP_NAME", raising=False)
    assert config.DEFAULT_APP_NAME == "bookdoe" and config.APP_TAGLINE == "Every Story. One Library."
    assert dataclasses.replace(config.Settings()).app_name == "bookdoe"
    monkeypatch.setenv("BOOKDOE_APP_NAME", "  ")
    assert config.Settings().app_name == "bookdoe", "a blank override falls back to the default"


def test_every_page_shows_bookdoe_with_logo_and_favicon(client):
    for path in ("/app", "/manage"):
        html = client.get(path).text
        assert _head_name(html) == "bookdoe", path
        assert '<meta name="application-name" content="bookdoe" />' in html
        assert '<meta name="apple-mobile-web-app-title" content="bookdoe" />' in html
        assert '<meta name="alx-product" content="bookdoe"' in html and 'data-brand="default"' in html
        assert "/assets/brand/bookdoe-horizontal-light.svg" in html and "/assets/brand/bookdoe-horizontal-dark.svg" in html
        assert '<link rel="icon" type="image/svg+xml" href="/assets/brand/bookdoe-mark.svg" />' in html
    setup = client.get("/setup", follow_redirects=False)
    if setup.status_code == 200:   # the wizard only exists while the install is empty
        assert _head_name(setup.text) == "bookdoe setup" and "Set up bookdoe" in setup.text
    manifest = json.loads(client.get("/manifest.webmanifest").text)
    assert manifest["name"] == manifest["short_name"] == "bookdoe"
    assert "Every Story. One Library." in manifest["description"]
    sw = client.get("/sw.js").text
    assert "bookdoe is offline" in sw and "{{app_name}}" not in sw


def test_logo_files_are_served_and_the_icons_are_brand_navy(client):
    for name in ("bookdoe-mark.svg", "bookdoe-horizontal-light.svg", "bookdoe-horizontal-dark.svg"):
        r = client.get(f"/assets/brand/{name}")
        assert r.status_code == 200 and r.text.lstrip().startswith("<svg"), name
        assert 'aria-label="bookdoe"' in r.text
    sw = client.get("/sw.js").text
    assert "/assets/brand/bookdoe-horizontal-dark.svg" in sw, "the logos are in the offline shell"


def test_one_setting_relabels_every_surface(client, relabel):
    relabel("Acme <Reads>")
    for path in ("/app", "/manage"):
        html = client.get(path).text
        assert _head_name(html) == "Acme &lt;Reads&gt;", path
        assert 'data-brand="custom"' in html and 'content="Acme &lt;Reads&gt;"' in html
        assert "bookdoe" not in html.replace("bookdoe-", ""), "no hard-coded product name left in the shell"
    manifest = json.loads(client.get("/manifest.webmanifest").text)
    assert manifest["name"] == manifest["short_name"] == "Acme <Reads>"
    assert "Acme &lt;Reads&gt; is offline" in client.get("/sw.js").text


def test_service_worker_name_is_escaped_for_a_js_string_not_html(client, relabel):
    relabel("Ada's \\ \"Books\"")
    sw = client.get("/sw.js").text
    assert "Ada\\'s \\\\ \\\"Books\\\" is offline" in sw, "a real apostrophe, escaped for the JS string"
    assert "&#x27;" not in sw and "&#39;" not in sw and "{{app_name}}" not in sw


# The product name as a standalone word; identifiers, asset paths and class names that contain it are fine.
_HARD_CODED_NAME = re.compile(r"(?<![\w./-])bookdoe(?![\w-])")


def _visible_lines(path: Path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        t = line.strip()
        if t.startswith(("//", "/*", "*", "<!--")):
            continue
        yield n, line


def test_user_visible_templates_carry_no_hard_coded_product_name():
    for name in ("index.html", "player.html", "setup.html", "manifest.webmanifest", "sw.js", "player.css", "styles.css", "player.js",
                 "app.js", "merge.js", "managedbuild.js"):
        offenders = [(n, l.strip()[:80]) for n, l in _visible_lines(STATIC / name) if _HARD_CODED_NAME.search(l) and "docker compose" not in l and "const PRODUCT" not in l]
        assert not offenders, f"{name}: {offenders}"
    mgmt = (STATIC / "app.js").read_text()
    for marker in ("<h1>${e(PRODUCT)} Management</h1>", "`${PRODUCT} ${b.server.sha}", 'id="login-form">${brandLogo('):
        assert marker in mgmt, marker
    assert 'class="auth-logo">A</div>' not in mgmt, "the letter logo is gone from every auth screen"
    for name in ("app.js", "player.js"):
        assert "const PRODUCT = document.querySelector('meta[name=\"alx-product\"]')" in (STATIC / name).read_text()


def test_about_pages_are_reachable_and_name_the_product():
    player = (STATIC / "player.js").read_text()
    assert "if (r === 'about') return await renderAbout();" in player and 'href="#about"' in player
    assert "${e(PRODUCT)}" in player[player.index("async function renderAbout"):]
    mgmt = (STATIC / "app.js").read_text()
    about = mgmt[mgmt.index("async function settingsAbout"):]
    assert "about-name" in about[:400] and "${e(PRODUCT)}" in about[:400]


def test_ios_and_android_app_icons_are_opaque_1024_png():
    icons = [p for p in (REPO / "ios").rglob("*.png") if p.name in ("icon.png", "app_icon_navy.png") and "AppIcon" in str(p)]
    icons += list((REPO / "android").rglob("app_icon_navy.png"))
    assert icons, "no app icon files found"
    for p in icons:
        data = p.read_bytes()
        w, h = struct.unpack(">II", data[16:24])
        assert (w, h) == (1024, 1024), p
        assert data[25] in (0, 2), f"{p} must be opaque (colour type 6 is RGBA, 4 is grey+alpha)"


def test_server_strings_follow_the_name_setting(client, monkeypatch):
    from app import db
    from app.services import devices
    monkeypatch.setattr(devices, "settings", dataclasses.replace(devices.settings, app_name="Acme Reads"))
    from app import auth as auth_module
    gated = dataclasses.replace(main_module.settings, app_name="Acme Reads", auth_mode="oidc")
    monkeypatch.setattr(main_module, "settings", gated)
    monkeypatch.setattr(auth_module, "settings", gated)
    sent = []
    monkeypatch.setattr(devices, "deliver", lambda cfg, msg: sent.append(msg))
    db.set_setting(devices.SETTINGS_KEY, {"host": "smtp.example", "from_email": "a@example.com"})
    assert devices.send_test_email("me@example.com")["ok"]
    assert sent[0]["Subject"] == "Acme Reads test message"
    assert "Acme Reads" in sent[0].get_content() and "bookdoe" not in sent[0].get_content()
    r = client.get("/opds/v1/catalog.xml")
    assert r.status_code == 401 and r.headers["www-authenticate"] == 'Basic realm="Acme Reads OPDS"'
