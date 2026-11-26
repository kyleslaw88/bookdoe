"""The README Quick start embeds the setup wizard and web Player screenshots (#678)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_embeds_screenshots_and_files_exist():
    refs = re.findall(r"!\[[^\]]*\]\((docs/img/[^)]+)\)", README)
    assert "docs/img/setup-wizard.png" in refs
    assert "docs/img/player.png" in refs
    for ref in refs:
        f = ROOT / ref
        assert f.is_file(), ref
        assert f.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", ref
