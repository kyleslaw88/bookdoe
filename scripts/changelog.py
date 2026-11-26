"""Print the change notes in docs/changes/ (newest first), one file per change.

New changelog entries go in docs/changes/<YYYY-MM-DD>-<slug>.md instead of the shared
list in the product spec, so two branches never edit the same lines. A note is the
entry text as it would have appeared in the spec, starting with "- **YYYY-MM-DD**: ...".
Run from anywhere: python scripts/changelog.py [--check] [DIR]. --check only validates
the file names and the first line of each note.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "docs" / "changes"
NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9][a-z0-9-]*\.md$")


def problems(directory: Path) -> list[str]:
    """One message per note whose name or first line breaks the format."""
    out = []
    for f in sorted(directory.glob("*.md")):
        m = NAME.match(f.name)
        if not m:
            out.append(f"{f.name}: name must be <YYYY-MM-DD>-<slug>.md (lowercase letters, digits, hyphens)")
            continue
        first = f.read_text(encoding="utf-8").lstrip().split("\n", 1)[0]
        if not first.startswith(f"- **{m.group(1)}**"):
            out.append(f"{f.name}: first line must start with '- **{m.group(1)}**'")
    return out


def notes(directory: Path) -> str:
    """The notes concatenated newest first (file name order, reversed)."""
    files = sorted(directory.glob("*.md"), reverse=True)
    return "\n".join(f.read_text(encoding="utf-8").strip() + "\n" for f in files)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    rest = [a for a in argv if a != "--check"]
    directory = Path(rest[0]) if rest else DEFAULT_DIR
    bad = problems(directory)
    for line in bad:
        print(line, file=sys.stderr)
    if bad:
        return 1
    if not check:
        print(notes(directory), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
