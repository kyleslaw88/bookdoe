"""Player UI acquisition gate (#502). The browser proof is scripts/ui-player-acquisition.mjs (run by
`make uigate`); these pin the shipped player.js so the gating cannot be dropped in a refactor."""
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "app" / "static" / "player.js").read_text()


def test_flag_is_read_from_auth_me_without_new_endpoint():
    assert "state.acquisition = me.acquisition_enabled !== false" in JS
    assert "function acquisitionOn() { return state.acquisition !== false; }" in JS


def test_every_acquisition_surface_checks_the_flag():
    for needle in (
        "state.pendingApprovals && acquisitionOn()",            # nav badge
        "if (!isApprover() || !acquisitionOn()) return;",       # badge poll
        "acquisitionOn() ? 'Search the library or request a book.'",  # home card
        "${acquisitionOn() ? '<section class=\"pl-section\" id=\"disc-mine\">",  # My requests
        "const ext = !acq ? ''",                                # Elsewhere results
        "d.in_library.filter(x => acq || x.visible)",           # request-only rows / Request button
        "const ext = acquisitionOn() ? cards.map",              # chat "Not in your library" cards
        "${acq ? `<section class=\"pl-section\"><div class=\"pl-section-head\"><h2>Waiting",  # approvals
        "if (acq) Promise.all(['approved', 'denied']",          # decided requests
    ):
        assert needle in JS, needle


def test_approvals_loading_subtitle_and_kid_waiting_text():
    assert '<p class="pl-sub">${lead}</p>${skeleton(2)}' in JS
    assert "${acq && k.pending_requests ?" in JS
