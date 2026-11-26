"""The product is called bookdoe (#598). The old name may appear only in the compatibility shim,
its tests, docs/UPGRADING.md, the changelog fragments and the documented legacy-compat lines of the
mobile apps (#599)."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NEEDLE = "alexa" + "ndria"   # not spelled out, so this file never matches itself

ALLOWED_FILES = {
    "bookdoe/app/legacy.py",                       # the compatibility shim
    "bookdoe/tests/test_legacy_compat.py",         # its tests
    "bookdoe/tests/test_no_legacy_name.py",
    "docs/UPGRADING.md",
    "scripts/release/scrub.py",                    # rewrites it for the public snapshot
}
ALLOWED_PREFIXES = (
    "docs/changes/",                               # changelog fragments are history
)
# iOS legacy-compat lines (#599): the background session id, the keychain service, the old servers/queue
# file names and the old ServerKind raw value stay so an update installs over the current TestFlight build.
PENDING_MOBILE_PREFIXES = ()
PENDING_MOBILE_FILES = {
    "ios/App/DownloadManager.swift", "ios/README.md",
    "ios/Packages/BookdoeKit/Sources/BookdoeKit/Auth/CredentialStore.swift",
    "ios/Packages/BookdoeKit/Sources/BookdoeKit/Models/Models.swift",
    "ios/Packages/BookdoeKit/Sources/BookdoeKit/Servers/ServerStore.swift",
    "ios/Packages/BookdoeKit/Sources/BookdoeKit/Sync/PositionGuard.swift",
    "ios/Packages/BookdoeKit/Sources/BookdoeKit/Sync/ProgressQueue.swift",
    "ios/Packages/BookdoeKit/Tests/BookdoeKitTests/LegacyCompatTests.swift",
    # Rename 3/3 (docs and repo-level files):
    "CONTRIBUTING.md", "README.md", ".gitignore", "docs/PORTABILITY_AND_IOS_PLAN.md", "docs/VNEXT_LEDGER.md",
    # CI secret names are stored on the CI host and keep their names.
    ".github/workflows/ios.yml",
}


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def _allowed(path: str) -> bool:
    return path in ALLOWED_FILES or path in PENDING_MOBILE_FILES or path.startswith(ALLOWED_PREFIXES)


def test_old_product_name_appears_only_in_the_allowed_files():
    offenders = []
    for path in _tracked():
        if _allowed(path):
            continue
        if NEEDLE in path.lower():
            offenders.append(f"{path} (file name)")
            continue
        try:
            text = (REPO_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if NEEDLE in text.lower():
            line = next(n for n, l in enumerate(text.splitlines(), 1) if NEEDLE in l.lower())
            offenders.append(f"{path}:{line}")
    assert not offenders, "the old product name is back: " + ", ".join(offenders[:20])
