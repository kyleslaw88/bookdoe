"""CSP, HSTS, and the opt-out for /docs and /openapi.json (#658)."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app, api_docs_enabled


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_csp_on_pages(client):
    csp = client.get("/app").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'self'" in csp and "object-src 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("style-src")[0]


def test_csp_inline_script_only_on_setup(client):
    assert "'unsafe-inline'" in client.get("/setup", follow_redirects=False).headers["Content-Security-Policy"].split("style-src")[0]


def test_hsts_only_over_https(client):
    assert "Strict-Transport-Security" not in client.get("/app").headers
    r = client.get("/app", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


def test_hsts_when_public_url_https(client, monkeypatch):
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(main_module.settings, public_url="https://x.example"))
    assert "Strict-Transport-Security" in client.get("/app").headers


@pytest.mark.parametrize("env,url,want", [
    ("", "http://localhost:8787", True),
    ("", "https://demo.example.com", False),
    ("1", "https://demo.example.com", True),
    ("0", "http://localhost:8787", False),
])
def test_api_docs_default(monkeypatch, env, url, want):
    monkeypatch.setenv("BOOKDOE_API_DOCS", env)
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(main_module.settings, public_url=url))
    assert api_docs_enabled() is want


def test_docs_served_locally(client):
    assert client.get("/openapi.json").status_code == 200
