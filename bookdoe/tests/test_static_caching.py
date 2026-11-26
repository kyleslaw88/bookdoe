"""The UI is served from unversioned URLs, so a deploy only reaches users if the
browser/CDN revalidates. Regression: a new UI tab did not appear after deploy
because /assets/app.js carried no Cache-Control and was reused from cache."""
from fastapi.testclient import TestClient

from app import main as main_module

client = TestClient(main_module.app)


def test_assets_and_shell_revalidate_instead_of_caching_blindly():
    for url in ("/assets/app.js", "/assets/styles.css", "/"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.headers["cache-control"] == "no-cache", url


def test_unchanged_asset_is_a_304_not_a_full_download():
    first = client.get("/assets/app.js")
    again = client.get("/assets/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
