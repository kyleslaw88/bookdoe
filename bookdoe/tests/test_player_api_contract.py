"""Contract tests: docs/PLAYER_API.md, the served OpenAPI schema and the auth rules
must agree. The route table in the doc is parsed and compared with /openapi.json, every
documented route is probed anonymously (401 unless the doc says `none`), and device
tokens are probed against the non-Player surface (403)."""
import dataclasses
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import main as main_module
from app.config import settings as base_settings

DOC = Path(__file__).resolve().parents[2] / "docs" / "PLAYER_API.md"
BASE = "/api/player/v1"
app = main_module.app


def documented_routes():
    text = DOC.read_text()
    table = text.split("## Routes", 1)[1].split("## Sync", 1)[0]
    rows = []
    for line in table.splitlines():
        m = re.match(r"\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+)\|", line)
        if m:
            rows.append((m.group(1), BASE + m.group(2), m.group(3).strip()))
    return rows


def served_routes():
    schema = TestClient(app).get("/openapi.json").json()
    out = set()
    for path, ops in schema["paths"].items():
        if not path.startswith(BASE):
            continue
        for method in ops:
            out.add((method.upper(), re.sub(r"\{[^}]+\}", "{x}", path)))
    return out


def norm(path):
    return re.sub(r"\{[^}]+\}", "{x}", path)


def test_every_documented_route_is_served_and_every_served_route_is_documented():
    doc = {(m, norm(p)) for m, p, _ in documented_routes()}
    served = served_routes()
    assert doc, "no routes parsed from docs/PLAYER_API.md"
    assert doc - served == set(), f"documented but not served: {sorted(doc - served)}"
    assert served - doc == set(), f"served but undocumented: {sorted(served - doc)}"


def test_openapi_describes_the_player_tag_and_models():
    schema = TestClient(app).get("/openapi.json").json()
    player_ops = [op for path, ops in schema["paths"].items() if path.startswith(BASE) for op in ops.values()]
    assert player_ops and all("player" in op.get("tags", []) for op in player_ops)
    assert all(op.get("summary") or op.get("description") for op in player_ops), "every route carries a docstring"
    comps = schema["components"]["schemas"]
    for model in ("ProgressBatch", "ProgressItem", "DeviceCodeRequest", "DeviceTokenRequest", "RequestCreate", "BookmarkRequest", "PreferencesRequest"):
        assert model in comps, model
    assert comps["ProgressBatch"]["properties"]["items"]["maxItems"] == 100


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def test_anonymous_access_matches_the_documented_auth_column(local_mode):
    with TestClient(app) as c:
        c.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        anon = TestClient(app)
        for method, path, auth in documented_routes():
            probe = path.replace("{work}", "w").replace("{asset}", "a").replace("{id}", "i").replace("{kid}", "k").replace("{file}", "f")
            r = anon.request(method, probe, json={} if method in ("POST", "PUT", "PATCH") else None)
            if auth.startswith("none"):
                assert r.status_code != 401, (method, path, r.status_code)
            else:
                assert r.status_code == 401, f"{method} {path} answered {r.status_code} without a credential"


def test_device_tokens_are_refused_outside_the_player_lane(local_mode):
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        uid = c.get("/api/v1/auth/me").json()["user"]["id"]
        tok = auth_module.create_device(uid, name="t")["token"]
        dev = TestClient(app, headers={"Authorization": f"Bearer {tok}"})
        schema = dev.get("/openapi.json").json()
        checked = 0
        for path, ops in schema["paths"].items():
            if not path.startswith("/api/v1/") or "/files/" in path or path.endswith("/cover"):
                continue
            if path in ("/api/v1/auth/me", "/api/v1/auth/setup", "/api/v1/auth/login", "/api/v1/version"):
                continue  # /me reports the device; setup and login take no credential at all; /version is the public short form
            for method in ops:
                probe = re.sub(r"\{[^}]+\}", "x", path)
                r = dev.request(method.upper(), probe, json={} if method in ("post", "put", "patch") else None)
                assert r.status_code == 403, f"{method.upper()} {path} let a device token through ({r.status_code})"
                checked += 1
        assert checked > 100
        # The one personal probe a device may use outside the lane reports the device.
        me = dev.get("/api/v1/auth/me").json()
        assert me["user"]["device"]["name"] == "t" and me["user"]["management"] is False
