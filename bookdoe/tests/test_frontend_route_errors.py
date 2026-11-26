"""D6 and D4 (review 2026-10-10): a failed page load must render the error panel, and
search must stay reachable on a phone.

route() used to `return renderX()` inside its try block without awaiting, so a rejected
promise never reached the catch and the loading skeleton stayed on screen forever.
The node check below runs the real app.js with a fetch() that answers 500 and asserts
the "Something went wrong" panel is what ends up in #view.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "app" / "static"
APP_JS = STATIC / "app.js"
CSS = STATIC / "styles.css"
NODE = shutil.which("node")

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [appJs] = process.argv.slice(1);
function el() {
  return { innerHTML: '', textContent: '', value: '', dataset: {}, style: {}, hidden: false,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, appendChild() {}, remove() {}, closest: () => null, focus() {}, insertAdjacentHTML() {},
    setAttribute() {}, querySelector: () => el(), querySelectorAll: () => [] };
}
const view = el();
const doc = Object.assign(el(), {
  documentElement: el(), body: el(), createElement: () => el(),
  querySelector: s => s === '#view' ? view : el(), querySelectorAll: () => [],
});
const ctx = {
  view, console, setTimeout, clearTimeout, URLSearchParams,
  fetch: async () => ({ ok: false, status: 500, headers: { get: () => 'application/json' }, json: async () => ({ detail: 'database is locked' }), text: async () => '' }),
  document: doc, window: { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
  location: { hash: '#dashboard', origin: 'http://x', reload() {} },
  localStorage: { getItem: () => null, setItem() {} }, confirm: () => true,
  done: r => { console.log(JSON.stringify(r ?? null)); process.exit(0); },
  fail: er => { console.error(er && er.stack || String(er)); process.exit(1); },
};
vm.createContext(ctx);
const src = fs.readFileSync(appJs, 'utf8').replace(/\ninitAuth\(\);\s*$/, '\n');
vm.runInContext(src, ctx, { filename: 'app.js' });
vm.runInContext(`(async () => {
  state.me = { username: 'ada', is_admin: true, access_scope: 'full' };
  await route();
  return view.innerHTML;
})().then(done, fail)`, ctx, { filename: 'test-body.js' });
"""


def _route_body() -> str:
    js = APP_JS.read_text()
    start = js.index("async function route()")
    end = js.index("window.addEventListener('hashchange',route)")
    return js[start:end]


def test_every_page_render_in_route_is_awaited():
    body = _route_body()
    unawaited = re.findall(r"return (?!await\b)\s*\(?\s*(?:isKidsScope\(\)\s*\?\s*)?render\w+\(", body)
    assert not unawaited, f"route() returns promises without await, so its catch never sees a failed load: {unawaited}"
    assert body.count("return await") >= 15


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_failed_dashboard_load_renders_the_error_panel():
    out = subprocess.run([NODE, "-e", HARNESS, str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    html = json.loads(out.stdout.strip().splitlines()[-1])
    assert "Something went wrong" in html
    assert "database is locked" in html
    assert "skeleton" not in html, "the loading skeleton must be replaced, not left on screen"


def test_global_search_is_not_hidden_on_narrow_viewports():
    """Spec Part XXXIV: search remains available on mobile; never removed for width."""
    css = CSS.read_text()
    narrow = [m.group(0) for m in re.finditer(r"@media\s*\(max-width:\s*\d+px\)\s*\{.*?\n\}", css, re.S)]
    assert narrow, "expected narrow-viewport media blocks"
    for block in narrow:
        for rule in re.findall(r"\.global-search(?:\s+input)?\s*\{[^}]*\}", block):
            assert "display:none" not in rule.replace(" ", ""), rule
    assert "<div class=\"global-search\">" in (STATIC / "index.html").read_text()
