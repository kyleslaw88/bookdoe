"""Guard the install docs: HTTPS note, platform guides, Unraid template, wording."""
import pathlib
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(p):
    return (ROOT / p).read_text()


def test_readme_quick_start_and_wording():
    t = _read("README.md")
    assert "## Quick start" in t
    assert "hobby project" in t and "best-effort" in t
    assert "lawfully" in t


def test_https_note_for_ios_ats():
    for p in ("README.md", "docs/INSTALL.md"):
        t = _read(p)
        assert "App Transport Security" in t and "HTTPS" in t
    assert "reverse_proxy" in _read("docs/INSTALL.md")


def test_synology_and_unraid_docs_linked():
    assert (ROOT / "docs/SYNOLOGY.md").is_file()
    assert "docs/SYNOLOGY.md" in _read("README.md")
    assert "unraid/bookdoe.xml" in _read("docs/UNRAID.md")


def test_unraid_template_is_valid():
    root = ET.parse(ROOT / "unraid/bookdoe.xml").getroot()
    assert root.tag == "Container"
    assert root.findtext("Name") == "bookdoe"
    targets = {c.get("Target") for c in root.findall("Config")}
    assert {"8787", "/config", "/books", "/audiobooks", "OPENREADS_URL"} <= targets


def test_unraid_template_has_no_placeholder_urls():
    root = ET.parse(ROOT / "unraid/bookdoe.xml").getroot()
    for tag in ("Support", "Project"):
        url = root.findtext(tag)
        assert url.startswith("https://") and url.rstrip("/") != "https://github.com"
    assert root.find("Registry") is None
    assert "deferred" in _read("docs/UNRAID.md")
