"""CarPlay Audio entitlement (#467): enabled in project.yml and the entitlements file,
and the CI profile check passes/fails correctly on fixture profiles."""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "com.apple.developer.carplay-audio"
SCRIPT = REPO_ROOT / "ios" / "scripts" / "check-carplay-profile.sh"

WITH_KEY = f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Name</key><string>fixture</string>
<key>Entitlements</key>
<dict>
\t<key>{KEY}</key>
\t<true/>
</dict>
</dict></plist>
"""

WITHOUT_KEY = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Name</key><string>fixture</string>
<key>Entitlements</key>
<dict>
\t<key>aps-environment</key>
\t<string>production</string>
</dict>
</dict></plist>
"""

KEY_FALSE = WITH_KEY.replace("<true/>", "<false/>")


def _run(tmp_path, text):
    f = tmp_path / "profile.plist"
    f.write_text(text)
    return subprocess.run(["sh", str(SCRIPT), str(f)], capture_output=True, text=True)


def test_bookdoe_entitlements_has_carplay_true():
    """The bookdoe build (com.bookdoe.app) signs with Bookdoe.entitlements, so it needs the key too (#557)."""
    data = plistlib.loads((REPO_ROOT / "ios/App/Bookdoe.entitlements").read_bytes())
    assert data[KEY] is True


def test_project_yml_enables_carplay_and_drops_pending_comments():
    text = (REPO_ROOT / "ios/project.yml").read_text()
    assert f"\n        {KEY}: true" in text
    assert "DISABLED until Apple approves" not in text
    ent = (REPO_ROOT / "ios/App/Bookdoe.entitlements").read_text()
    assert "path: App/Bookdoe.entitlements" in text
    assert "pending approval" not in ent


def test_profile_check_passes_with_entitlement(tmp_path):
    r = _run(tmp_path, WITH_KEY)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_profile_check_fails_without_entitlement(tmp_path):
    r = _run(tmp_path, WITHOUT_KEY)
    assert r.returncode == 1
    assert KEY in r.stderr
    assert "PROVISIONING_PROFILE_BASE64" in r.stderr


def test_profile_check_fails_when_value_false(tmp_path):
    assert _run(tmp_path, KEY_FALSE).returncode == 1


def test_workflow_runs_check_before_archive():
    wf = (REPO_ROOT / ".github/workflows/ios.yml").read_text()
    assert "security cms -D -i" in wf
    assert wf.index("check-carplay-profile.sh") < wf.index("- name: Archive")
