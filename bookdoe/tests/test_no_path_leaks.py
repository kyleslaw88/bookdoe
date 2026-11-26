"""Non-admin callers never see filesystem paths, raw file names or root/profile ids in library,
Player or OPDS payloads; an administrator still does. Lanes: adult, kid, device token, trusted
browser cookie."""
import json
import re

from fastapi.testclient import TestClient

from app import auth
from app import main as main_module
from app.services import tagging

from test_asset_formats import _asset
from test_player_api import _login, local_mode, pair, world  # noqa: F401

PATHY = re.compile(r"(^|\")/(tmp|var|home|mnt|data)[^\"]*|\.(epub|mobi|mp3|m4b)\b|Both Formats-|Spoken Book-", re.I)
FORBIDDEN_KEYS = ('"path"', '"relative_path"', '"root_folder_id"', '"profile_id"', '"download_path"')


def _urls(wid, aid):
    return [f"/api/v1/library?limit=50", f"/api/v1/library/{wid}", "/api/player/v1/library", f"/api/player/v1/library/{wid}",
            f"/api/player/v1/audio/{wid}/{aid}", "/api/player/v1/home", "/api/player/v1/recent", "/api/player/v1/favorites",
            "/opds/v2/catalog.json?limit=50", "/opds/v1/catalog.xml"]


def _leaks(client, urls, tmp_path):
    found, seen = [], 0
    for u in urls:
        r = client.get(u)
        if r.status_code != 200:
            continue
        seen += 1
        body = r.text
        if str(tmp_path) in body or any(k in body for k in FORBIDDEN_KEYS) or PATHY.search(body):
            found.append(u)
    return found, seen


def _fixtures(tmp_path):
    wid, aid = _asset(tmp_path, "Both Formats", "ebook", ["both.epub", "both.mobi"], "mobi")
    tagging.add_tag(wid, tagging.kids_tag_id())
    awid, aaid = _asset(tmp_path, "Many Parts", "audiobook", [f"Chapter {i}.mp3" for i in range(1, 6)], "mp3", dur=600)
    tagging.add_tag(awid, tagging.kids_tag_id())
    return wid, aid


def test_non_admin_lanes_get_no_paths_but_admin_does(world, tmp_path):  # noqa: F811
    wid, aid = _fixtures(tmp_path)
    audio_wid, audio_aid = world["audio"][0], world["audio"][1]
    jane, jh = world["jane"]; owen, oh = world["owen"]
    jdev, _, _ = pair(jane, jh, name="jane dev"); odev, _, _ = pair(owen, oh, name="owen dev")
    trusts = {}
    for name, (c, h) in (("jane", world["jane"]), ("owen", world["owen"])):
        r = c.post("/api/player/v1/devices/trust", json={"name": name}, headers=h)
        assert r.status_code == 201, r.text
        cookie = TestClient(main_module.app)
        cookie.cookies.set(auth.DEVICE_COOKIE, c.cookies.get(auth.DEVICE_COOKIE))
        trusts[name] = cookie
    lanes = {"adult": jane, "kid": owen, "adult device": jdev, "kid device": odev, "adult cookie": trusts["jane"], "kid cookie": trusts["owen"]}
    total = 0
    for lane, client in lanes.items():
        found, seen = _leaks(client, _urls(wid, aid) + _urls(audio_wid, audio_aid), tmp_path)
        total += seen
        assert found == [], f"{lane} sees paths in {found}"
    assert total > 20
    # The detail page still works for the UI: file ids, a display name, format and readability.
    d = jane.get(f"/api/v1/library/{audio_wid}").json()
    f = d["assets"][0]["files"][0]
    assert f["name"].startswith("Track ") and f["format"] == "mp3" and f["readable"] is True and f["size_bytes"] and f["id"]
    # An administrator keeps them.
    admin, _ = world["admin"]
    assert str(tmp_path) in admin.get(f"/api/v1/library/{wid}").text
    assert '"path"' in admin.get("/api/v1/library?limit=50").text
