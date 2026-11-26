"""Regressions the owner reported after the Player went live (2026-10-11). The browser-level
proof is scripts/ui-player-e2e.mjs (run by `make uigate`); these pin the shipped
files so the fixes cannot be lost in a refactor without a failing test.

1. EPUB text size: epub.js's fontSize() only writes an inline size on <body>, which a
   publisher stylesheet with absolute sizes (`p { font-size: 12pt }`) defeats. The
   reader now sets the size on html with !important and makes text elements inherit.
2. Library tag filter: a <select> fed by /api/v1/tags (kid-scoped server-side), sent as
   tag_id, remembered across navigation and reloads.
3. Covers: the loaded <img> was painted underneath the absolutely positioned title
   fallback; it now has its own stacking position above it.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_reader_text_size_beats_publisher_absolute_sizes():
    js = (STATIC / "consume.js").read_text()
    assert "html: { 'font-size': `${size} !important` }" in js
    assert "'p, div, li, blockquote, dd, dt, td, th, figcaption, pre': { 'font-size': 'inherit !important'" in js
    assert "rendition.themes.fontSize(size)" in js


def test_cover_image_is_stacked_above_the_fallback():
    css = (STATIC / "player.css").read_text()
    rule = re.search(r"\.pl-cover img \{([^}]*)\}", css).group(1)
    assert "position: relative" in rule and "z-index: 1" in rule
    assert "position: absolute; inset: 0" in re.search(r"\.pl-cover \.pl-fallback \{([^}]*)\}", css).group(1)
    for sel in (".pl-cover .pl-kinds", ".pl-cover .pl-progress"):
        assert "z-index: 2" in re.search(re.escape(sel) + r" \{([^}]*)\}", css).group(1), sel


def test_library_tag_filter_is_wired_and_remembered():
    js = (STATIC / "player.js").read_text()
    assert 'id="lib-tag"' in js and "q.set('tag_id', L.tag)" in js
    assert "api('/api/v1/tags')" in js, "tags come from the server, which scopes them per viewer"
    assert "localStorage.getItem('alx-pl-lib')" in js and "function rememberLib()" in js
    assert "t.name === 'kids' && state.me.account_type === 'kid'" in js, "the kids tag is redundant on a kid account"


def test_ui_gate_runs_the_browser_e2e():
    gate = (Path(__file__).resolve().parents[2] / "scripts" / "ui-gate.mjs").read_text()
    assert "ui-player-seed.py" in gate and "ui-player-e2e.mjs" in gate and "BOOKDOE_AUTH_MODE:'local'" in gate


# ---------- audio controls (Management showed them unstyled, 2026-10-12) ----------
def _lum(hex_):
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_consume_styles_travel_with_the_module_so_management_renders_the_player_styled():
    """Management only links styles.css, which has no .pl-* rules; with its
    `button { color: inherit }` the player's buttons were the browser's light-grey faces
    carrying light text in the dark theme. consume.css holds the reader/audio rules for
    both shells and consume.js loads it itself."""
    css = (STATIC / "consume.css").read_text()
    js = (STATIC / "consume.js").read_text()
    assert "'/assets/consume.css'" in js and "await ensureStyles()" in js
    assert "button { color: inherit; }" in (STATIC / "styles.css").read_text(), "the rule that made the unstyled buttons light-on-light"
    for sel in (".pl-transport", ".pl-times", ".pl-reader {", ":where(body:not(.player-body)"):
        assert sel in css, sel
    player_css = (STATIC / "player.css").read_text()
    assert ".pl-transport" not in player_css and ".pl-reader {" not in player_css, "one copy of the audio and reader rules"
    assert "/assets/consume.css" in (STATIC / "sw.js").read_text(), "precached with the other shell files"


def test_audio_control_palette_meets_wcag_aa_in_both_themes():
    css = (STATIC / "consume.css").read_text()
    def tok(block, name):
        return re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", block).group(1)
    base = (STATIC / "player.css").read_text()
    dark_root, light_root = base.split(':root {', 1)[1].split('}', 1)[0], base.split('html[data-theme="light"] {', 1)[1].split('}', 1)[0]
    dark_consume = re.search(r"\.pl-consume \{([^}]*)\}", css).group(1)
    light_consume = re.search(r'html\[data-theme="light"\] \.pl-consume \{([^}]*)\}', css).group(1)
    for name, root, extra in (("dark", dark_root, dark_consume), ("light", light_root, light_consume)):
        bg, surface2, text, muted = tok(root, "--pl-bg"), tok(root, "--pl-surface-2"), tok(root, "--pl-text"), tok(root, "--pl-muted")
        accent, ink = tok(root, "--pl-accent"), tok(root, "--pl-accent-ink")
        edge, disabled = tok(extra, "--pl-edge"), tok(extra, "--pl-disabled-ink")
        assert _ratio(text, surface2) >= 4.5 and _ratio(muted, surface2) >= 4.5 and _ratio(muted, bg) >= 4.5, name
        assert _ratio(ink, accent) >= 4.5, f"{name}: play glyph on the accent face"
        assert _ratio(accent, bg) >= 3, f"{name}: accent face against the page"
        assert _ratio(edge, bg) >= 3 and _ratio(edge, surface2) >= 3, f"{name}: control edge"
        assert 3 <= _ratio(disabled, surface2) < _ratio(text, surface2), f"{name}: disabled is dimmed but legible"


def test_chat_poll_guard_is_a_generation_counter():
    # The stale-poll guard is a numeric generation counter, not a secret; keep it out of the string-compare timing lint (CWE-208).
    js = (STATIC / "player.js").read_text()
    assert "CHAT.token" not in js
    assert js.count("gen !== CHAT.gen") == 2
