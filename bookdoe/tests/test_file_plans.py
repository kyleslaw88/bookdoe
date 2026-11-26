"""P5-10: file-operation plan schema and plan API (Part XXIV). Plan + preview only.

Every test that calls the API snapshots the library tree first and asserts it is
byte-for-byte and mtime-for-mtime identical afterwards.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import fileops

APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def client():
    return TestClient(app)


def _tree(base: Path) -> dict:
    return {
        str(p.relative_to(base)): (p.is_dir(), None if p.is_dir() else p.stat().st_size, p.stat().st_mtime_ns,
                                   p.read_bytes() if p.is_file() else None)
        for p in sorted(base.rglob("*"))
    }


def _root(base: Path, name: str, mode: str = "managed", kind: str = "ebook", enabled: int = 1) -> dict:
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    rid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, name, str(path), kind, mode, 1, enabled, t, t),
        )
    return {"id": rid, "path": path}


def _book(root: dict, rel: str, *, kind: str = "ebook", title: str = "Dune", author: str = "Frank Herbert",
          flagged_managed: int = 0, content: bytes = b"book-bytes") -> str:
    """A work + asset + media_files row backed by a real file under the root."""
    path = root["path"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    wid, cid, aid, fid, t = db.uid(), db.uid(), db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)", (wid, title, title, "resolved", t, t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (cid, author, author, t, t))
        c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)", (wid, cid, "author", 0))
        c.execute("INSERT INTO media_assets(id,work_id,kind,format,status,added_at,updated_at) VALUES (?,?,?,?,?,?,?)", (aid, wid, kind, path.suffix.lstrip("."), "owned", t, t))
        c.execute(
            "INSERT INTO media_files(id,asset_id,path,relative_path,part_number,size_bytes,mtime,fingerprint,container,embedded_json,adopted,managed,missing,discovered_at,verified_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, aid, str(path), rel, 1, len(content), path.stat().st_mtime, "fp-1", path.suffix.lstrip("."), "{}", 0, flagged_managed, 0, t, t),
        )
    return fid


def _post(client, library: Path, params: dict, kind: str = "rename_move"):
    before = _tree(library)
    r = client.post("/api/v1/file-plans", json={"kind": kind, "params": params})
    assert _tree(library) == before, "planning must not touch the file tree"
    return r


def test_managed_file_plan_is_previewed_with_before_after_and_effects(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "old/Dune.epub", flagged_managed=0)  # the media_files.managed flag is not the authority
    r = _post(client, lib, {"file_id": fid, "dest_relative": "Frank Herbert/Dune.epub", "retag": True})
    assert r.status_code == 200
    plan = r.json()
    assert plan["status"] == "previewed" and plan["refusals"] == [] and plan["executable"] is True
    step = plan["steps"][0]
    assert step["source_path"] == str(root["path"] / "old/Dune.epub")
    assert step["dest_path"] == str((root["path"] / "Frank Herbert/Dune.epub").resolve())
    assert step["status"] == "pending"
    assert step["expected"] == {"size": len(b"book-bytes"), "mtime": (root["path"] / "old/Dune.epub").stat().st_mtime,
                                "fingerprint": "fp-1", "root_id": root["id"], "dest_root_id": root["id"]}
    pv = plan["preview"]
    assert pv["items"][0]["before"] == step["source_path"] and pv["items"][0]["after"] == step["dest_path"]
    assert pv["effects"] == ["move", "database update", "retag"]
    assert any("would be created" in w for w in pv["warnings"])
    assert client.get(f"/api/v1/file-plans/{plan['id']}").json() == plan
    assert [p["id"] for p in client.get("/api/v1/file-plans").json()["items"]] == [plan["id"]]


def test_same_folder_rename_effects(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "a/Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "a/Dune - Frank Herbert.epub"}).json()
    assert plan["preview"]["effects"] == ["rename", "database update"]
    assert plan["steps"][0]["op"] == "rename" and plan["refusals"] == []


def test_adopted_root_file_is_refused_and_unexecutable(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "adopted", mode="adopt")
    fid = _book(root, "Dune.epub", flagged_managed=1)  # a lying flag must not matter
    r = _post(client, lib, {"file_id": fid, "dest_relative": "New/Dune.epub"})
    assert r.status_code == 200
    plan = r.json()
    assert plan["executable"] is False and plan["status"] == "previewed"
    assert any("adopted root" in x for x in plan["refusals"])


def test_observe_root_file_is_refused(client, tmp_path):
    lib = tmp_path / "lib"
    fid = _book(_root(lib, "observe", mode="observe"), "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "x.epub"}).json()
    assert plan["executable"] is False and any("observe root" in x for x in plan["refusals"])


def test_destination_that_exists_is_refused(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "Dune.epub")
    (root["path"] / "Taken.epub").write_bytes(b"other")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "Taken.epub"}).json()
    assert plan["executable"] is False and "Destination already exists" in plan["refusals"]


def test_destination_escaping_the_root_is_refused(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "../outside/Dune.epub"}).json()
    assert plan["executable"] is False and any("escapes the root" in x for x in plan["refusals"])
    assert plan["steps"][0]["dest_path"] is None
    absolute = _post(client, lib, {"file_id": fid, "dest_relative": str(tmp_path / "abs.epub")}).json()
    assert any("escapes the root" in x for x in absolute["refusals"])


def test_destination_through_symlink_out_of_root_is_refused(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "Dune.epub")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root["path"] / "link").symlink_to(outside, target_is_directory=True)
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "link/Dune.epub"}).json()
    assert plan["executable"] is False and any("escapes the root" in x for x in plan["refusals"])


def test_destination_through_symlink_and_dotdot_resolves_as_the_filesystem_would(client, tmp_path):
    """'link/../x' is resolved on disk (link -> a/b, so it lands in a/), not collapsed lexically to x."""
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "Dune.epub")
    (root["path"] / "a" / "b").mkdir(parents=True)
    (root["path"] / "link").symlink_to(root["path"] / "a" / "b", target_is_directory=True)
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "link/../Dune.epub"}).json()
    assert plan["refusals"] == [] and plan["executable"] is True
    assert plan["steps"][0]["dest_path"] == str((root["path"] / "a" / "Dune.epub").resolve())


def test_destination_in_other_media_kind_root_is_refused(client, tmp_path):
    lib = tmp_path / "lib"
    ebooks = _root(lib, "ebooks")
    audio = _root(lib, "audio", kind="audiobook")
    fid = _book(ebooks, "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "Dune.epub", "dest_root_id": audio["id"]}).json()
    assert plan["executable"] is False and any("holds audiobook files" in x for x in plan["refusals"])


def test_destination_in_adopted_root_is_refused(client, tmp_path):
    lib = tmp_path / "lib"
    managed = _root(lib, "managed")
    adopted = _root(lib, "adopted", mode="adopt")
    fid = _book(managed, "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "Dune.epub", "dest_root_id": adopted["id"]}).json()
    assert plan["executable"] is False and any("adopted root" in x for x in plan["refusals"])


def test_cross_root_move_to_mixed_managed_root_is_allowed(client, tmp_path):
    lib = tmp_path / "lib"
    src = _root(lib, "managed")
    dst = _root(lib, "mixed", kind="mixed")
    fid = _book(src, "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "Dune.epub", "dest_root_id": dst["id"]}).json()
    assert plan["refusals"] == [] and plan["preview"]["effects"] == ["move", "database update"]
    assert plan["steps"][0]["expected"]["dest_root_id"] == dst["id"]


def test_missing_source_and_same_path_are_refused(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "Dune.epub")
    same = _post(client, lib, {"file_id": fid, "dest_relative": "Dune.epub"}).json()
    assert "Destination equals the source" in same["refusals"]
    (root["path"] / "Dune.epub").unlink()
    gone = _post(client, lib, {"file_id": fid, "dest_relative": "Other.epub"}).json()
    assert "Source file is missing on disk" in gone["refusals"] and gone["executable"] is False


def test_naming_template_matches_naming_preview(client, tmp_path):
    lib = tmp_path / "lib"
    root = _root(lib, "managed")
    fid = _book(root, "inbox/whatever.epub", title="Dune: Messiah")
    wid = db.one("SELECT a.work_id FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE f.id=?", (fid,))["work_id"]
    pv = client.get(f"/api/v1/naming/ebook/preview?work_id={wid}&ext=epub").json()
    plan = _post(client, lib, {"file_id": fid, "use_naming_template": True}).json()
    assert plan["refusals"] == []
    expected = (root["path"] / pv["folder"] / pv["files"][0]).resolve()
    assert Path(plan["steps"][0]["dest_path"]) == expected


def test_bad_requests_are_400_or_404_not_refusals(client, tmp_path):
    lib = tmp_path / "lib"
    fid = _book(_root(lib, "managed"), "Dune.epub")
    assert client.post("/api/v1/file-plans", json={"kind": "nope", "params": {}}).status_code == 400
    assert client.post("/api/v1/file-plans", json={"kind": "rename_move", "params": {"file_id": fid}}).status_code == 400
    both = {"file_id": fid, "dest_relative": "a.epub", "use_naming_template": True}
    assert client.post("/api/v1/file-plans", json={"kind": "rename_move", "params": both}).status_code == 400
    assert client.post("/api/v1/file-plans", json={"kind": "rename_move", "params": {"file_id": fid, "dest_relative": "a", "x": 1}}).status_code == 400
    assert client.post("/api/v1/file-plans", json={"kind": "rename_move", "params": {"file_id": "nope", "dest_relative": "a"}}).status_code == 404
    assert client.post("/api/v1/file-plans", json={"kind": "rename_move", "params": {}, "extra": 1}).status_code == 422
    assert client.get("/api/v1/file-plans/nope").status_code == 404
    assert client.get("/api/v1/file-plans").json() == {"items": []}


def test_cancel_plan_and_state_rules(client, tmp_path):
    lib = tmp_path / "lib"
    fid = _book(_root(lib, "managed"), "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "New.epub"}).json()
    before = _tree(lib)
    r = client.post(f"/api/v1/file-plans/{plan['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["executable"] is False
    assert _tree(lib) == before
    assert client.post(f"/api/v1/file-plans/{plan['id']}/cancel").status_code == 409
    assert client.post("/api/v1/file-plans/nope/cancel").status_code == 404
    assert [p["id"] for p in client.get("/api/v1/file-plans?status=cancelled").json()["items"]] == [plan["id"]]


def test_plan_expires_after_24h(client, tmp_path):
    lib = tmp_path / "lib"
    fid = _book(_root(lib, "managed"), "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "New.epub"}).json()
    assert plan["expires_at"] - plan["created_at"] == fileops.PLAN_TTL_SECONDS == 24 * 3600
    with db.tx() as c:
        c.execute("UPDATE file_operation_plans SET expires_at=? WHERE id=?", (db.now() - 1, plan["id"]))
    stale = client.get(f"/api/v1/file-plans/{plan['id']}").json()
    assert stale["expired"] is True and stale["executable"] is False


def test_no_execute_route_exists(client, tmp_path):
    lib = tmp_path / "lib"
    fid = _book(_root(lib, "managed"), "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "New.epub"}).json()
    for suffix in ("execute", "run", "approve"):
        assert client.post(f"/api/v1/file-plans/{plan['id']}/{suffix}").status_code in (404, 405)
    # The app's catch-all GET handler makes any unknown POST a 405 (not a 404); GET shows the route is absent.
    assert client.get(f"/api/v1/file-plans/{plan['id']}/execute").status_code == 404
    assert client.get(f"/api/v1/file-plans/{plan['id']}").json()["status"] == "previewed"
    assert [r.path for r in app.routes if "file-plans" in getattr(r, "path", "")
            and "execute" in r.path] == []


def test_plan_steps_cascade_with_plan_and_creator_is_recorded(client, tmp_path):
    lib = tmp_path / "lib"
    fid = _book(_root(lib, "managed"), "Dune.epub")
    plan = _post(client, lib, {"file_id": fid, "dest_relative": "New.epub"}).json()
    assert plan["created_by"] == db.one("SELECT created_by FROM file_operation_plans")["created_by"]
    with db.tx() as c:
        c.execute("DELETE FROM file_operation_plans WHERE id=?", (plan["id"],))
    assert db.one("SELECT COUNT(*) AS n FROM file_operation_steps")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM media_files")["n"] == 1  # deleting a plan never touches files


def test_fileops_module_is_read_only():
    """The planner must never mutate the filesystem: no os/shutil/Path mutation APIs."""
    forbidden_attrs = {"rename", "renames", "replace", "unlink", "remove", "rmdir", "removedirs", "mkdir",
                       "makedirs", "write_text", "write_bytes", "touch", "link", "symlink", "symlink_to",
                       "hardlink_to", "copy", "copy2", "copyfile", "copytree", "move", "rmtree", "chmod",
                       "truncate", "utime"}
    for name in ("fileops.py",):
        tree = ast.parse((APP_DIR / "services" / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                assert not any(m.split(".")[0] == "shutil" for m in mods), f"{name} imports shutil"
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    assert fn.attr not in forbidden_attrs, f"{name}:{node.lineno} calls .{fn.attr}()"
                if isinstance(fn, ast.Name) and fn.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    assert mode is None or (isinstance(mode, ast.Constant) and not set("wax+") & set(mode.value)), f"{name}:{node.lineno} opens for writing"
        text = (APP_DIR / "services" / name).read_text()
        assert "shutil" not in text and "os.rename" not in text and "os.unlink" not in text


def test_file_plan_routes_require_admin_and_csrf(tmp_path, monkeypatch):
    from app import auth as auth_module
    seen = {r.path: r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/file-plans")}
    assert set(seen) == {"/api/v1/file-plans", "/api/v1/file-plans/{plan_id}", "/api/v1/file-plans/{plan_id}/cancel"}
    for route in seen.values():
        deps = {d.call for d in route.dependant.dependencies}
        assert auth_module.require_admin in deps, route.path
        if "POST" in route.methods:
            assert auth_module.require_csrf in deps, route.path
