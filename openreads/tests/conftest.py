import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def _no_live_audiobookdb(monkeypatch):
    """Unit tests never touch the network; tests that exercise AudiobookDB
    turn it on and mock its transport."""
    import audiobookdb
    monkeypatch.setattr(audiobookdb, "ENABLED", False)
    yield
