"""#635: indexer fallback GUIDs and player-API ETags use SHA-256, not SHA-1."""
from __future__ import annotations

import hashlib
import json
import re

from app import player_api
from app.integrations import indexers


def test_indexer_hash_is_sha256():
    assert len(indexers._hash("x")) == 64
    assert indexers._hash("x") == hashlib.sha256(b"x").hexdigest()


def test_player_api_etag_is_sha256_prefix():
    payload = {"b": 1, "a": [1, 2]}
    tag = player_api._etag(payload)
    assert re.fullmatch(r'"[0-9a-f]{20}"', tag)
    want = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20]
    assert tag == '"' + want + '"'
