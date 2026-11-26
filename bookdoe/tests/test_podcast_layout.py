"""The #509 parser sits beside the podcast repository module (issue #533)."""
from __future__ import annotations

from pathlib import Path

from app.services import podcastmeta, podcasts


def test_parser_is_beside_repository_module():
    parser, repo = Path(podcastmeta.__file__), Path(podcasts.__file__)
    assert parser.parent == repo.parent
    assert (parser.name, repo.name) == ("podcastmeta.py", "podcasts.py")
    assert parser.parent.parts[-2:] == ("app", "services")
