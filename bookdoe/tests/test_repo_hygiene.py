"""Repository hygiene: no secrets, tokens, personal hosts or machine paths in tracked
files. Mirrors .gitleaks.toml so the check runs in CI without the gitleaks binary.

The scan covers every git-tracked text file in the repository (falling back to a
directory walk when git is unavailable). A hit prints path:line and the rule that
matched, never the matched value in full, so a real leak is not echoed into CI logs.
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# (rule id, compiled regex). Values are deliberately broad; placeholders are allowed
# through by ALLOW_PATTERNS below.
RULES = [
    ("bookdoe-api-key", re.compile(r"\b(alex|alxd)_[A-Za-z0-9_\-]{30,}")),
    ("infisical-service-token", re.compile(r"\bst\.[a-f0-9\-]{36}\.[a-f0-9\-]{36}\.[a-f0-9]{32}")),
    ("github-token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack-token", re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{20,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret-assignment", re.compile(
        r"(?i)\b[A-Z0-9_]*(SECRET_KEY|API_KEY|PASSWORD|CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN)\s*[=:]\s*['\"]?[A-Za-z0-9+/_\-]{24,}")),
    ("private-ipv4", re.compile(
        r"\b(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")),
]

# Rules that name this project's own hosts, paths and domain live only in .gitleaks.toml
# (the public snapshot strips them), so no literal of them is kept in this file.
CONFIG_RULE_IDS = ("private-host-path", "personal-domain")


def _load_gitleaks_rules(config_path: str | Path | None = None) -> list[tuple[str, re.Pattern[str]]]:
    """Return the CONFIG_RULE_IDS rules found in a .gitleaks.toml (none if the file lacks them)."""
    path = Path(config_path) if config_path else REPO_ROOT / ".gitleaks.toml"
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    return [(r["id"], re.compile(r["regex"])) for r in cfg.get("rules", []) if r.get("id") in CONFIG_RULE_IDS]


RULES = RULES + _load_gitleaks_rules()

ALLOW_PATTERNS = [re.compile(p) for p in (
    r"change-me",
    r"example\.(com|invalid|org)",
    r"pytest-secret-key",
    r"qa-secret-key",
    r"ui(test|gate)-secret-key",       # browser QA fixtures (scripts/)
    r"172\.18\.0\.0/16",
    r"10\.0\.0\.0/(8|24)",
    r"os\.(getenv|environ)",          # reading a variable, not a value
    r"\$\{?[A-Z_]+",                   # shell/compose interpolation
    r"<[A-Za-z_\- ]+>",                # <placeholder>
)]


def _load_gitleaks_allow_paths(config_path: str | Path | None = None) -> list[str]:
    """Return the ``[allowlist] paths`` list from a .gitleaks.toml file.

    Parameters
    ----------
    config_path :
        Path to the TOML config file.  Defaults to ``REPO_ROOT/.gitleaks.toml``.
    """
    if config_path is None:
        config_path = REPO_ROOT / ".gitleaks.toml"
    config_path = Path(config_path)
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("allowlist", {}).get("paths", [])


def _anchor(patterns: list[str]) -> list[re.Pattern[str]]:
    """Compile gitleaks path regexes anchored to the whole repo-relative path."""
    return [re.compile(f"^(?:{p})$") for p in patterns]


ALLOW_PATHS = _anchor(_load_gitleaks_allow_paths())

TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml", ".json", ".sh", ".txt", ".env", ".example", ".cfg", ".ini",
                 ".js", ".mjs", ".html", ".css", ".swift", ".plist", ".xcconfig", ".rb", ""}
SKIP_DIRS = {".git", "node_modules", ".local", ".cache", "__pycache__", ".pytest_cache", ".build", "DerivedData"}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                names.append(os.path.relpath(os.path.join(dirpath, f), REPO_ROOT))
    return [REPO_ROOT / n for n in names if (REPO_ROOT / n).is_file()]


def scan_file(
    path: Path,
    allow_paths: list[re.Pattern[str]] | None = None,
    repo_root: Path | None = None,
) -> list[tuple[str, int, str]]:
    """Scan a single file for rule matches.

    Parameters
    ----------
    path :
        Absolute path to the file to scan.
    allow_paths :
        Compiled regex patterns for paths to skip.  Defaults to the
        module-level ``ALLOW_PATHS`` loaded from ``.gitleaks.toml``.
    repo_root :
        Root used to compute the relative path for allowlist matching.
        Defaults to ``REPO_ROOT``.
    """
    if allow_paths is None:
        allow_paths = ALLOW_PATHS
    if repo_root is None:
        repo_root = REPO_ROOT
    rel = path.relative_to(repo_root).as_posix()
    if any(p.search(rel) for p in allow_paths):
        return []
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in ("Makefile", "Dockerfile", "LICENSE"):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, rx in RULES:
            m = rx.search(line)
            if not m:
                continue
            if any(a.search(line) for a in ALLOW_PATTERNS):
                continue
            hits.append((rel, lineno, rule))
    return hits


def test_repository_has_no_secrets_or_machine_specific_values():
    files = tracked_files()
    assert files, "no files found to scan"
    hits = [h for f in files for h in scan_file(f)]
    assert not hits, "repository hygiene violations (path:line rule):\n" + "\n".join(f"  {p}:{n} {r}" for p, n, r in hits)


@pytest.mark.parametrize("sample,rule", [
    ("token = alxd_" + "a" * 40, "bookdoe-api-key"),
    ("BOOKDOE_SECRET_KEY=" + "f" * 48, "secret-assignment"),
    ("host 192.168.50.7", "private-ipv4"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private-key-block"),
])
def test_rules_catch_representative_samples(sample, rule, tmp_path):
    # The scanner must flag these shapes; a regex edit that silently stops matching
    # would otherwise pass the suite with a clean-looking repository.
    assert any(rx.search(sample) and r == rule for r, rx in RULES), rule
    assert not any(a.search(sample) for a in ALLOW_PATTERNS)


@pytest.mark.parametrize("sample", [
    "BOOKDOE_SECRET_KEY=change-me-to-a-long-random-secret",
    "BOOKDOE_OIDC_ISSUER=https://auth.example.com/application/o/bookdoe/",
    "BOOKDOE_TRUSTED_PROXIES=172.18.0.0/16",
    "secret_key: str = os.getenv('BOOKDOE_SECRET_KEY', '')",
])
def test_placeholders_are_allowed(sample):
    flagged = any(rx.search(sample) for _, rx in RULES)
    allowed = any(a.search(sample) for a in ALLOW_PATTERNS)
    assert not flagged or allowed, sample


def _write_gitleaks_cfg(path: Path, paths: list[str]) -> None:
    """Write a minimal .gitleaks.toml with the given allowlist paths.

    Uses simple TOML string formatting (no external writer) so the test
    does not depend on a third-party library.  Backslashes in paths are
    doubled so they survive TOML basic-string escaping.
    """
    lines = ['title = "test"', ""]
    lines.append("[allowlist]")
    lines.append("paths = [")
    for p in paths:
        # TOML basic strings require backslashes to be escaped
        escaped = p.replace("\\", "\\\\")
        lines.append(f'  "{escaped}",')
    lines.append("]")
    path.write_text("\n".join(lines) + "\n")


def test_gitleaks_allow_paths_are_honoured_by_hygiene_scan(tmp_path):
    """Prove that a path added to .gitleaks.toml is honoured by the hygiene scan.

    Creates a temporary .gitleaks.toml with an extra allowlist path, writes a file
    under that path containing a known secret, and verifies the scan skips it.
    A control run with an empty allowlist proves the same file IS flagged when
    the path is absent from the config.
    """
    # Build a modified list of paths by appending an extra one
    base_paths = _load_gitleaks_allow_paths()
    extra_path = r"tests/allowlist-test/.*"
    all_paths = list(base_paths) + [extra_path]

    # Write the temp config
    tmp_cfg = tmp_path / ".gitleaks.toml"
    _write_gitleaks_cfg(tmp_cfg, all_paths)

    # Create a file under the extra allowlisted path with a known secret
    test_dir = tmp_path / "tests" / "allowlist-test"
    test_dir.mkdir(parents=True)
    secret_file = test_dir / "leak.py"
    # Use bookdoe-api-key pattern which is unambiguous
    secret_file.write_text("token = alxd_" + "x" * 40 + "\n")

    # Load paths from our temp config via the production loader
    loaded_paths = _anchor(_load_gitleaks_allow_paths(tmp_cfg))

    # --- With the extra path allowlisted, the secret-bearing file is skipped ---
    hits_with_allowlist = scan_file(
        secret_file, allow_paths=loaded_paths, repo_root=tmp_path
    )
    assert not hits_with_allowlist, (
        f"expected no hits when path is allowlisted, got: {hits_with_allowlist}"
    )

    # --- Control: with an empty allowlist the same file IS flagged ---
    hits_without_allowlist = scan_file(
        secret_file, allow_paths=[], repo_root=tmp_path
    )
    assert hits_without_allowlist, (
        "expected hits when path is NOT allowlisted, got none"
    )
