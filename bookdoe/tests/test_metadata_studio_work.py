"""P4-06b: Metadata Studio Work tab. The real app.js runs in node against a live server
(see test_inbox_ui.run_ui); a small DOM stub hands each rendered field its own slice of
the page so the buttons and forms wireStudioWork binds are the ones the tests click."""
from __future__ import annotations

import dataclasses
import json

from app import db, main
from app.config import settings
from app.services import covers, metadata
from test_inbox_ui import _seed_work, live, needs_node, run_ui  # noqa: F401  (live is a fixture)

DOM = r"""
  const view = { innerHTML: '', querySelector: () => view, querySelectorAll: () => [], addEventListener() {}, setAttribute() {}, insertAdjacentHTML() {}, appendChild() {}, classList: { toggle() {}, add() {}, remove() {} } };
  const fields = {}, manual = {}, toasts = [];
  const chunk = key => (view.innerHTML.split(`data-studio-field="${key}"`)[1] || '').split('data-studio-field="')[0];
  const field = key => {
    const el = fields[key] = { input: { value: '' }, buttons: [] };
    el.querySelector = () => el.input;
    el.querySelectorAll = sel => sel === '[data-action-index]'
      ? (el.buttons = [...chunk(key).matchAll(/data-action-index="(\d+)">([^<]*)</g)].map(m => ({ dataset: { actionIndex: m[1] }, label: m[2] })))
      : [];
    return el;
  };
  document.querySelector = sel => {
    const m = /data-studio-(field|manual)="(\w+)"/.exec(sel);
    if (m && m[1] === 'manual') {
      const input = { value: '' }, toggled = [];
      return manual[m[2]] ||= { input, toggled, querySelector: () => input, classList: { toggle: c => toggled.push(c) } };
    }
    return m ? field(m[2]) : view;
  };
  document.querySelectorAll = () => [];
  document.body = { style: {}, classList: { toggle() {}, add() {}, remove() {} } };
  toast = (msg, kind) => toasts.push([msg, kind || 'good']);
  state.me = { is_admin: true };
  const click = async (key, label, nth = 0) => {
    const b = fields[key].buttons.filter(x => x.label === label)[nth];
    if (!b) throw new Error(`no ${label} button on ${key}: ${chunk(key)}`);
    await b.onclick();
  };
  const submit = async (key, value) => { fields[key].input.value = value; await fields[key].onsubmit({ preventDefault() {} }); };
  const submitManual = async (key, value) => { manual[key].input.value = value; await manual[key].onsubmit({ preventDefault() {} }); };
"""


def _provider_work() -> str:
    """Two provider syncs: title/description/language observed, subtitle/date never."""
    meta = {"id": "or-studio", "title": "Studio Book", "description": "first", "primary_language": "en",
            "contributors": [{"name": "Ann Author", "role": "author"}, {"name": "Ned Narrator", "role": "narrator"}],
            "series": [], "identifiers": [], "editions": []}
    wid = metadata.upsert_work(meta)
    metadata.upsert_work({**meta, "description": "second"})
    return wid


def _sections(html: str) -> dict[str, str]:
    parts = html.split('data-studio-field="')[1:]
    return {p.split('"', 1)[0]: p for p in parts}


def _work(wid: str) -> dict:
    return db.one("SELECT * FROM works WHERE id=?", (wid,))


def _locked(wid: str, field: str) -> bool:
    return db.one("SELECT 1 x FROM metadata_field_locks WHERE entity_type='work' AND entity_id=? AND field_name=?", (wid, field)) is not None


@needs_node
def test_work_tab_renders_evidence_cards_for_observed_fields_and_inputs_for_the_rest(live):
    wid = _provider_work()
    html = run_ui(DOM + f"location.hash = '#library/{wid}/studio/work'; await route(); return view.innerHTML;", live)
    s = _sections(html)
    assert list(s) == ["cover", "title", "subtitle", "authors", "contributors", "description", "first_publish_date",
                       "primary_language", "subjects", "series", "series_position"]
    for key in ("title", "description", "primary_language"):  # full evidence treatment
        assert 'class="evidence-card"' in s[key] and "Current value" in s[key] and "Source: provider" in s[key]
        assert "Unlocked" in s[key] and "Competing observations" in s[key]
        for label in (">Lock<", ">Manual value<", ">Restore automatic<"):
            assert label in s[key]
    assert "evidence-title\">second<" in s["description"] and "first <span" in s["description"]
    assert s["description"].count(">Choose</button>") == 1  # the one competing observation
    assert "No competing observations." in s["title"] and ">Choose</button>" not in s["title"]
    assert "Unknown author" not in html  # a field card is not a book record
    for key in ("subtitle", "first_publish_date"):  # instrumented, but nothing observed yet
        assert "studio-fallback" in s[key] and 'name="value"' in s[key] and "evidence-card" not in s[key]
        assert "No observations recorded yet" in s[key]
    assert 'value="Ann Author"' in s["authors"] and "studio-fallback" in s["authors"]
    for key in ("series", "series_position"):
        assert "studio-fallback" in s[key]
    # Contributors render as a structured, role-labeled list grouped by role.
    assert "studio-contributors" in s["contributors"] and "studio-contributor-group" in s["contributors"]
    assert "studio-readonly" in s["contributors"]
    assert "Ann Author" in s["contributors"] and "Ned Narrator" in s["contributors"]
    assert "(author)" in s["contributors"] and "(narrator)" in s["contributors"]
    assert "studio-readonly" in s["subjects"] and "does not store genres" in s["subjects"]
    for key in ("contributors", "subjects"):
        assert 'name="value"' not in s[key] and "data-action-index" not in s[key]


@needs_node
def test_card_controls_fire_the_field_action_endpoints(live):
    wid = _provider_work()
    out = run_ui(DOM + f"""
      const steps = {{}};
      const snap = () => view.innerHTML.split('data-studio-field="description"')[1].split('data-studio-field="')[0];
      location.hash = '#library/{wid}/studio/work'; await route();
      await click('description', 'Lock'); steps.locked = snap();
      await click('description', 'Unlock'); steps.unlocked = snap();
      await click('description', 'Choose');
      await click('description', 'Manual value'); steps.revealed = manual.description.toggled;
      await submitManual('description', 'Typed by hand'); steps.manual = snap();
      await click('description', 'Restore automatic'); steps.restored = snap();
      return {{ steps, toasts }};
    """, live)
    steps = out["steps"]
    assert ">Unlock<" in steps["locked"] and "Locked by" in steps["locked"]
    assert ">Lock<" in steps["unlocked"] and "Unlocked" in steps["unlocked"]
    assert steps["revealed"] == ["hidden"]  # 'Manual value' reveals the inline manual form
    assert "Source: manual" in steps["manual"] and "Typed by hand" in steps["manual"]
    assert "Source: provider" in steps["restored"] and "evidence-title\">second<" in steps["restored"]
    assert [t[1] for t in out["toasts"]] == ["good"] * 5
    assert _work(wid)["description"] == "second" and not _locked(wid, "description")
    events = [h["event_type"] for h in db.all("SELECT event_type FROM history WHERE work_id=? ORDER BY created_at,rowid", (wid,))]
    assert [x for x in events if x.startswith("metadata.field_")] == [
        "metadata.field_locked", "metadata.field_unlocked", "metadata.field_chosen", "metadata.field_manual", "metadata.field_restored"]
    chosen = db.one("SELECT data_json FROM history WHERE work_id=? AND event_type='metadata.field_chosen'", (wid,))["data_json"]
    assert json.loads(chosen)["value"] == "first"  # Choose applied the competing observation


@needs_node
def test_fallback_inputs_save_and_an_observed_field_then_gets_its_card(live):
    wid = _provider_work()
    out = run_ui(DOM + f"""
      location.hash = '#library/{wid}/studio/work'; await route();
      await submit('series_position', '3');  // no series yet: refused with a message
      await submit('subtitle', 'A Subtitle');
      await submit('authors', 'Ann Author, Bob Second');
      await submit('series', 'The Saga');
      await submit('series_position', '2');
      return {{ html: view.innerHTML, toasts }};
    """, live)
    assert out["toasts"][0] == ["Set a series before its position", "bad"]
    assert [t[0] for t in out["toasts"][1:]] == ["Subtitle saved", "Authors saved", "Series saved", "Series position saved"]
    w = _work(wid)
    assert w["subtitle"] == "A Subtitle" and _locked(wid, "subtitle")  # P4-05 manual endpoint
    authors = db.all("SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (wid,))
    assert [a["name"] for a in authors] == ["Ann Author", "Bob Second"]
    series = db.all("SELECT s.name,m.position FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (wid,))
    assert [(x["name"], str(x["position"])) for x in series] == [("The Saga", "2")]
    s = _sections(out["html"])
    assert "evidence-card" in s["subtitle"] and "Source: manual" in s["subtitle"]  # now observed: full card
    assert 'value="The Saga"' in s["series"] and 'value="2"' in s["series_position"]


@needs_node
def test_work_without_any_observations_is_all_plain_inputs(live):
    wid = _seed_work("Hand Added", author="Test Author")
    html = run_ui(DOM + f"location.hash = '#library/{wid}/studio'; await route(); return view.innerHTML;", live)
    s = _sections(html)
    assert "evidence-card" not in html and 'value="Hand Added"' in s["title"]
    assert all("studio-fallback" in s[k] for k in s if k not in ("contributors", "subjects", "cover"))



@needs_node
def test_contributors_render_structured_role_labeled_list(live):
    """Contributors render as a structured, role-labeled list grouped by role,
    not as a flat comma-separated string.  Roles come from P4-07 role data."""
    wid = _provider_work()
    html = run_ui(DOM + f"location.hash = '#library/{wid}/studio/work'; await route(); return view.innerHTML;", live)
    s = _sections(html)
    # Structured HTML: studio-contributors wrapper, studio-contributor-group divs, li items
    assert "studio-contributors" in s["contributors"]
    assert "studio-contributor-group" in s["contributors"]
    assert "<li>" in s["contributors"]
    # Role labels present for both roles
    assert "(author)" in s["contributors"] and "(narrator)" in s["contributors"]
    # Both contributor names present
    assert "Ann Author" in s["contributors"] and "Ned Narrator" in s["contributors"]
    # No flat-string format
    assert "Ann Author (Author), Ned Narrator (Narrator)" not in s["contributors"]
    # No editable input inside contributors
    assert 'name="value"' not in s["contributors"]
    assert "data-action-index" not in s["contributors"]


@needs_node
def test_contributors_degrade_gracefully_when_no_authors_or_narrators(live):
    """When a work has no authors and no narrators, the contributors field
    renders without JS errors and shows a dash placeholder."""
    wid = _seed_work("No Contributors Book")
    html = run_ui(DOM + f"location.hash = '#library/{wid}/studio/work'; await route(); return view.innerHTML;", live)
    s = _sections(html)
    # No crash, no evidence-card, no flat string
    assert "evidence-card" not in s["contributors"]
    assert "studio-readonly" in s["contributors"]
    # Shows dash placeholder when no contributors
    assert "\u2014" in s["contributors"] or "muted" in s["contributors"]


@needs_node
def test_contributors_degrade_when_only_authors_present(live):
    """When a work has authors but no narrators, contributors shows only authors
    with role labels, no JS errors."""
    wid = _seed_work("Author Only Book", author="Solo Author")
    html = run_ui(DOM + f"location.hash = '#library/{wid}/studio/work'; await route(); return view.innerHTML;", live)
    s = _sections(html)
    assert "studio-contributors" in s["contributors"]
    assert "Solo Author" in s["contributors"]
    assert "(author)" in s["contributors"]
    assert "studio-readonly" in s["contributors"]


# The cover section's controls are looked up by id inside it. An id resolves only when
# the rendered section really contains it, and always to the same object, so whatever
# wireStudioWork bound during route() is what the tests read back and fire. fetch is
# wrapped (not replaced) to record what the handlers sent to the live server.
COVER_DOM = DOM + r"""
  const coverNodes = {};
  const coverRoot = { querySelectorAll: () => [], querySelector: sel => {
    const id = sel.replace(/^#/, '');
    return chunk('cover').includes(`id="${id}"`) ? (coverNodes[id] ||= { id, value: '', disabled: false, innerHTML: '' }) : null;
  } };
  const baseQuery = document.querySelector;
  document.querySelector = sel => sel === '[data-studio-field="cover"]'
    ? (view.innerHTML.includes('data-studio-field="cover"') ? coverRoot : null) : baseQuery(sel);
  const cover = id => coverRoot.querySelector('#' + id);
  const requests = [], realFetch = fetch;
  fetch = (path, opts = {}) => {
    const body = opts.body == null ? null : typeof opts.body === 'string' ? opts.body : Array.from(new Uint8Array(opts.body));
    requests.push({ path, method: opts.method || 'GET', contentType: (opts.headers || {})['Content-Type'] || null, body });
    return realFetch(path, opts);
  };
  const coverCalls = () => requests.filter(r => r.path.includes('/cover'));
  const bound = () => ({ fetch: typeof cover('studio-cover-fetch')?.onclick, upload: typeof cover('studio-cover-file-input')?.onchange,
                         remove: typeof cover('studio-cover-remove')?.onclick });
  // What the refreshed thumbnail now points at, fetched back from the server.
  const thumb = async () => {
    const src = (/src="([^"]+)"/.exec(cover('studio-cover-thumb').innerHTML) || [])[1];
    if (!src) return { src: null };
    const r = await realFetch(src);
    return { src, status: r.status, bytes: r.ok ? Array.from(new Uint8Array(await r.arrayBuffer())) : null };
  };
"""

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(64, 128))


def _manual_cover(wid: str) -> bytes | None:
    hit = covers.cached(wid)
    return hit["path"].read_bytes() if hit and hit["source"] == "manual" else None


def _assert_thumb_refreshed(out: dict, wid: str) -> None:
    assert out["thumb"]["src"].startswith(f"/api/v1/library/{wid}/cover?t=")  # cache-busted, so the new art shows


@needs_node
def test_route_binds_the_cover_controls_and_upload_puts_the_file_bytes(live):
    """Upload: the file's bytes and type go to PUT /cover/upload, become the manual cover,
    and the thumbnail is re-pointed at it."""
    wid = _seed_work("Cover Upload", author="Test Author")
    assert _manual_cover(wid) is None
    out = run_ui(COVER_DOM + f"""
      location.hash = '#library/{wid}/studio/work'; await route();
      const wired = bound();
      const file = {{ type: 'image/png', arrayBuffer: async () => new Uint8Array({list(PNG)}).buffer }};
      await cover('studio-cover-file-input').onchange({{ target: {{ files: [file] }} }});
      return {{ wired, calls: coverCalls(), toasts, thumb: await thumb() }};
    """, live)
    assert out["wired"] == {"fetch": "function", "upload": "function", "remove": "function"}
    assert out["calls"] == [{"path": f"/api/v1/library/{wid}/cover/upload", "method": "PUT", "contentType": "image/png", "body": list(PNG)}]
    assert out["toasts"] == [["Cover updated", "good"]]
    assert _manual_cover(wid) == PNG
    _assert_thumb_refreshed(out, wid)
    assert out["thumb"]["status"] == 200 and bytes(out["thumb"]["bytes"]) == PNG


@needs_node
def test_cover_fetch_puts_the_url_and_stores_the_image_it_points_at(live):
    """Fetch: the URL goes to PUT /cover as {url}; the server downloads it (here, another
    work's cover served by the same live server, so no outside network) and stores it."""
    source, wid = _seed_work("Cover Source"), _seed_work("Cover Fetch", author="Test Author")
    covers.set_manual(source, JPEG)
    url = f"{live}/api/v1/library/{source}/cover"
    out = run_ui(COVER_DOM + f"""
      location.hash = '#library/{wid}/studio/work'; await route();
      const btn = cover('studio-cover-fetch');
      cover('studio-cover-url-input').value = '  {url}  ';
      await btn.onclick();
      return {{ calls: coverCalls(), toasts, thumb: await thumb(), disabled: btn.disabled }};
    """, live)
    assert out["calls"] == [{"path": f"/api/v1/library/{wid}/cover", "method": "PUT", "contentType": "application/json", "body": json.dumps({"url": url}, separators=(",", ":"))}]
    assert out["toasts"] == [["Cover updated", "good"]]
    assert _manual_cover(wid) == JPEG
    _assert_thumb_refreshed(out, wid)
    assert out["thumb"]["status"] == 200 and bytes(out["thumb"]["bytes"]) == JPEG
    assert out["disabled"] is False  # re-enabled after the request


@needs_node
def test_cover_fetch_reports_the_server_error_and_ignores_an_empty_url(live):
    """An empty URL sends nothing; a URL that is not an image surfaces the server's 400
    detail as a bad toast and leaves the work without a cover."""
    wid = _seed_work("Cover Fetch Error", author="Test Author")
    url = f"{live}/api/v1/library/no-such-work/cover"  # a local 404, not an image
    out = run_ui(COVER_DOM + f"""
      location.hash = '#library/{wid}/studio/work'; await route();
      const btn = cover('studio-cover-fetch');
      await btn.onclick();
      const afterEmpty = coverCalls().length;
      cover('studio-cover-url-input').value = '{url}';
      await btn.onclick();
      return {{ afterEmpty, calls: coverCalls(), toasts, thumb: cover('studio-cover-thumb').innerHTML, disabled: btn.disabled }};
    """, live)
    assert out["afterEmpty"] == 0
    assert [(c["method"], c["path"]) for c in out["calls"]] == [("PUT", f"/api/v1/library/{wid}/cover")]
    assert out["toasts"] == [["Could not fetch a usable image from that URL", "bad"]]
    assert out["thumb"] == "" and out["disabled"] is False  # thumbnail untouched on failure
    assert _manual_cover(wid) is None


@needs_node
def test_cover_remove_deletes_the_manual_cover(live, monkeypatch):
    """Remove: DELETE /cover drops the manual cover; the thumbnail is re-pointed and the
    server now answers 404 for it."""
    # Removing re-runs automatic resolution; keep it from asking remote providers.
    monkeypatch.setattr(main, "settings", dataclasses.replace(settings, cover_remote_fetch=False))
    wid = _seed_work("Cover Remove", author="Test Author")
    covers.set_manual(wid, PNG)
    out = run_ui(COVER_DOM + f"""
      location.hash = '#library/{wid}/studio/work'; await route();
      const before = (await realFetch('/api/v1/library/{wid}/cover')).status;
      await cover('studio-cover-remove').onclick();
      return {{ before, calls: coverCalls(), toasts, thumb: await thumb() }};
    """, live)
    assert out["before"] == 200
    assert [(c["method"], c["path"], c["body"]) for c in out["calls"]] == [("DELETE", f"/api/v1/library/{wid}/cover", None)]
    assert out["toasts"] == [["Cover removed", "good"]]
    assert _manual_cover(wid) is None and not covers.cover_file(wid).exists()
    _assert_thumb_refreshed(out, wid)
    assert out["thumb"]["status"] == 404


@needs_node
def test_review_edit_manually_opens_the_studio_work_tab(live):
    """The Inbox's 'Edit manually' action on an unresolved book lands on the Studio's Work tab."""
    wid = _seed_work("Needs Review", author="Test Author")
    tid = db.uid()
    with db.tx() as c:
        c.execute(
            "INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "metadata_match", "work", wid, "Needs Review", "{}", "open", 0, db.now()))
    out = run_ui(DOM + f"""
      // A browser stores location.hash with its leading '#', however it was assigned.
      let hash = '';
      Object.defineProperty(location, 'hash', {{ get: () => hash, set: v => {{ hash = v.startsWith('#') ? v : '#' + v; }} }});
      await loadInbox('review');
      const t = inboxState.review.items.find(x => x.id === '{tid}');
      await reviewTaskActions(t).find(a => a.label === 'Edit manually').handler();
      await route();
      return {{ hash: location.hash, html: view.innerHTML }};
    """, live)
    assert out["hash"] == f"#library/{wid}/studio/work"
    html = out["html"]
    assert 'class="tab active" role="tab" aria-selected="true" data-studio-tab="work"' in html
    assert 'data-studio-panel="work"' in html and 'data-studio-field="cover"' in html
    assert 'value="Needs Review"' in _sections(html)["title"]


@needs_node
def test_title_description_and_language_are_edited_through_the_studio(live):
    """The evidence-card fields (title, description, primary_language) take a manual value
    through the card's 'Manual value' form, which persists and locks it."""
    wid = _provider_work()
    out = run_ui(DOM + f"""
      location.hash = '#library/{wid}/studio/work'; await route();
      for (const [key, value] of [['title', 'Edited Title'], ['description', 'Edited description'], ['primary_language', 'fr']]) {{
        await click(key, 'Manual value');
        await submitManual(key, value);
      }}
      return {{ html: view.innerHTML, toasts }};
    """, live)
    assert [t[1] for t in out["toasts"]] == ["good"] * 3
    w = _work(wid)
    assert (w["title"], w["description"], w["primary_language"]) == ("Edited Title", "Edited description", "fr")
    s = _sections(out["html"])
    for key in ("title", "description", "primary_language"):
        assert _locked(wid, key) and "Source: manual" in s[key]


@needs_node
def test_subtitle_date_series_and_authors_are_edited_through_the_studio(live):
    """The remaining fields the old edit form had are plain Studio inputs until observed;
    saving them persists the value (and subtitle/date, being instrumented, then get a card)."""
    wid = _provider_work()
    out = run_ui(DOM + f"""
      location.hash = '#library/{wid}/studio/work'; await route();
      await submit('subtitle', 'A Subtitle');
      await submit('first_publish_date', '2024-01-15');
      await submit('series', 'The Saga');
      await submit('authors', 'Ann Author, Bob Second');
      return {{ html: view.innerHTML, toasts }};
    """, live)
    assert [t[0] for t in out["toasts"]] == ["Subtitle saved", "Original publication date saved", "Series saved", "Authors saved"]
    w = _work(wid)
    assert w["subtitle"] == "A Subtitle" and w["first_publish_date"] == "2024-01-15"
    authors = db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord",
        (wid,),
    )
    assert [a["name"] for a in authors] == ["Ann Author", "Bob Second"]
    series = db.all("SELECT s.name FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (wid,))
    assert [x["name"] for x in series] == ["The Saga"]
    s = _sections(out["html"])
    for key in ("subtitle", "first_publish_date"):  # saved through the manual endpoint: now observed
        assert "evidence-card" in s[key] and "Source: manual" in s[key]
    assert 'value="The Saga"' in s["series"] and 'value="Ann Author, Bob Second"' in s["authors"]


@needs_node
def test_plain_input_fields_vs_evidence_cards_documented(live):
    """Verify which fields render as plain inputs vs evidence cards.

    Evidence cards (have observations): title, description, primary_language
    Plain inputs (no observations yet): subtitle, first_publish_date, series, series_position, authors
    Structured readonly: contributors (role-labeled list)
    Plain readonly: subjects (no edit endpoint)
    """
    wid = _provider_work()
    html = run_ui(DOM + f"location.hash = '#library/{wid}/studio/work'; await route(); return view.innerHTML;", live)
    s = _sections(html)

    # Evidence cards: title, description, primary_language
    for key in ("title", "description", "primary_language"):
        assert "evidence-card" in s[key], f"{key} should be an evidence card"

    # Plain inputs (studio-fallback): subtitle, first_publish_date, series, series_position, authors
    for key in ("subtitle", "first_publish_date", "series", "series_position", "authors"):
        assert "studio-fallback" in s[key], f"{key} should be a plain input"
        assert 'name="value"' in s[key], f"{key} should have an editable input"

    # Contributors: structured readonly (not a plain input)
    assert "studio-contributors" in s["contributors"]
    assert "studio-readonly" in s["contributors"]
    assert 'name="value"' not in s["contributors"]

    # Subjects: plain readonly
    assert "studio-readonly" in s["subjects"]
    assert 'name="value"' not in s["subjects"]

def test_studio_action_results_are_announced_through_the_shell_live_region():
    """Every Work-tab action reports its outcome only through toast(); the served shell's
    #toast-stack must be a live region so that outcome reaches screen readers too."""
    from fastapi.testclient import TestClient

    from app import main as main_module

    # The Management shell lives at /manage since the Player split; the Player's own
    # toast stack (#pl-toast) is checked alongside.
    html = TestClient(main_module.app).get("/manage").text
    stack = html.split('id="toast-stack"', 1)[1].split(">", 1)[0]
    player = TestClient(main_module.app).get("/app").text
    pl_stack = player.split('id="pl-toast"', 1)[1].split(">", 1)[0]
    assert 'role="status"' in pl_stack and 'aria-live="polite"' in pl_stack
    assert 'role="status"' in stack and 'aria-live="polite"' in stack


def test_old_edit_form_is_gone_from_the_shipped_frontend():
    """The flat edit dialog and its Work-page button are removed; covers are edited only in the Studio."""
    from test_inbox_ui import STATIC_DIR

    for name in ("app.js", "index.html"):
        text = (STATIC_DIR / name).read_text()
        for gone in ('id="edit-work"', "'#edit-work'", "edit-work-form", "edit-save", 'id="cover-url-fetch"', 'id="cover-url-input"', 'id="cover-edit-thumb"', 'id="cover-remove"'):
            assert gone not in text, f"{gone} still in {name}"
