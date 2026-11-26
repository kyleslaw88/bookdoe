"""The build indicator: buildinfo module, /api/v1/version (+details), /readyz and Player API
capabilities, the stamped HTML shells and service worker, and the Docker stamp."""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import main as main_module
from app.config import settings as base_settings
from app.services import buildinfo, readiness

from test_pwa import HARNESS, NODE, needs_node

app = main_module.app
APP_DIR = Path(__file__).parent.parent


@pytest.fixture
def stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(buildinfo, "INFO_FILE", tmp_path / "build_info.json")   # no file
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "abc123def456")
    monkeypatch.setenv("BOOKDOE_BUILD_TIME", "2026-10-14T09:30Z")


@pytest.fixture
def unstamped(monkeypatch, tmp_path):
    monkeypatch.setattr(buildinfo, "INFO_FILE", tmp_path / "build_info.json")
    monkeypatch.delenv("BOOKDOE_BUILD_SHA", raising=False)
    monkeypatch.delenv("BOOKDOE_BUILD_TIME", raising=False)


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


# ---------- the module ----------

def test_buildinfo_reads_the_environment(stamp):
    b = buildinfo.get()
    assert b["sha"] == "abc123def456" and b["built_at"] == "2026-10-14T09:30Z"
    assert set(b) == {"version", "sha", "built_at", "started_at", "api_version", "migration_step"}
    assert b["api_version"] == "1" and b["started_at"] > 0 and isinstance(b["migration_step"], int)
    assert buildinfo.short() == {"sha": "abc123def456", "built_at": "2026-10-14T09:30Z"}


def test_buildinfo_without_a_stamp_is_dev(unstamped):
    assert buildinfo.short() == {"sha": "dev", "built_at": None}
    assert buildinfo.get()["sha"] == "dev"


def test_buildinfo_falls_back_to_the_image_file_and_env_wins(unstamped, monkeypatch, tmp_path):
    (tmp_path / "build_info.json").write_text(json.dumps({"sha": "filesha00001", "built_at": "2026-01-01T00:00Z"}))
    assert buildinfo.short() == {"sha": "filesha00001", "built_at": "2026-01-01T00:00Z"}
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "envsha000002")
    assert buildinfo.short()["sha"] == "envsha000002"


# ---------- HTTP surfaces ----------

def test_version_is_public_and_minimal(stamp):
    with TestClient(app) as c:
        r = c.get("/api/v1/version")
        assert r.status_code == 200
        assert r.json() == {"sha": "abc123def456", "built_at": "2026-10-14T09:30Z"}   # no step, no start time
        assert r.headers["cache-control"] == "no-store"


def test_version_details_are_admin_only(stamp, local_mode):
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        uid = admin.get("/api/v1/auth/me").json()["user"]["id"]
        for name, kind in (("jane", "adult"), ("owen", "kid")):
            assert admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah).status_code in (200, 201)
        d = admin.get("/api/v1/version/details")
        assert d.status_code == 200
        body = d.json()
        assert body["sha"] == "abc123def456" and body["api_version"] == "1" and body["started_at"] > 0
        assert isinstance(body["migration_step"], int) and body["migration_step"] > 0
        assert TestClient(app).get("/api/v1/version/details").status_code == 401
        for name in ("jane", "owen"):
            c = TestClient(app)
            assert c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"}).status_code == 200
            assert c.get("/api/v1/version/details").status_code == 403, name
            assert c.get("/api/v1/version").json() == {"sha": "abc123def456", "built_at": "2026-10-14T09:30Z"}
        dev = TestClient(app, headers={"Authorization": "Bearer " + auth_module.create_device(uid, name="t")["token"]})
        assert dev.get("/api/v1/version/details").status_code == 403, "devices never get the admin detail"
        assert dev.get("/api/v1/version").json() == {"sha": "abc123def456", "built_at": "2026-10-14T09:30Z"}


def test_readyz_carries_the_short_build(stamp, monkeypatch):
    monkeypatch.setattr("app.services.readiness._check_openreads", lambda: {"ok": True})
    r = TestClient(app).get("/readyz")
    assert r.json()["build"] == {"sha": "abc123def456", "built_at": "2026-10-14T09:30Z"}


def test_capabilities_carry_the_short_build_and_the_doc_says_so(stamp):
    cap = TestClient(app).get("/api/player/v1/capabilities").json()
    assert cap["build"] == {"sha": "abc123def456", "built_at": "2026-10-14T09:30Z"}
    assert cap["api_version"] == "1"
    doc = (APP_DIR.parent / "docs" / "PLAYER_API.md").read_text()
    assert "`build` (`sha`, `built_at` only)" in doc


# ---------- stamped shells and worker ----------

@pytest.mark.parametrize("path", ["/", "/app", "/manage"])
def test_html_shells_carry_the_build_and_versioned_asset_urls(stamp, path):
    html = TestClient(app).get(path).text
    assert '<meta name="alx-build" content="abc123def456" data-built="2026-10-14T09:30Z" />' in html
    assert re.findall(r'(?:src|href)="(/assets/[^"]+\.(?:js|css))[^"]*"', html), "shell references assets"
    for url in re.findall(r'(?:src|href)="(/assets/[^"]+\.(?:js|css)[^"]*)"', html):
        assert url.endswith("?v=abc123def456"), url
    # icons and the manifest are not versioned URLs
    assert 'href="/assets/brand/bookdoe-mark.svg"' in html


def test_a_new_build_changes_every_script_and_style_url(monkeypatch, unstamped):
    c = TestClient(app)
    urls = lambda: set(re.findall(r'/assets/[^"]+\.(?:js|css)\?v=[^"]+', c.get("/app").text))
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "aaaaaaaaaaaa")
    one = urls()
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "bbbbbbbbbbbb")
    two = urls()
    assert one and two and not (one & two)


def test_build_values_are_sanitised_before_they_reach_html_and_js(monkeypatch, unstamped):
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "a'b\"<script>x")
    c = TestClient(app)
    assert "<script>x" not in c.get("/app").text
    sw = c.get("/sw.js").text
    assert "const BUILD = 'abscriptx';" in sw


def test_sw_route_bakes_the_build_in_and_every_deploy_changes_its_bytes(monkeypatch, unstamped):
    c = TestClient(app)
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "111111111111")
    one = c.get("/sw.js")
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "222222222222")
    two = c.get("/sw.js")
    assert "const BUILD = '111111111111';" in one.text and "const BUILD = '222222222222';" in two.text
    assert one.text != two.text and one.headers["cache-control"] == "no-cache"
    assert one.headers["content-type"].startswith("text/javascript")


# ---------- the worker: cache names follow the build, saved media survives ----------

@needs_node
def test_sw_shell_cache_follows_the_build_and_offline_media_survive_an_update(tmp_path, monkeypatch, unstamped):
    c = TestClient(app)
    old, new = tmp_path / "sw_old.js", tmp_path / "sw_new.js"
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "oldoldoldold")
    old.write_text(c.get("/sw.js").text)
    monkeypatch.setenv("BOOKDOE_BUILD_SHA", "newnewnewnew")
    new.write_text(c.get("/sw.js").text)
    # One shared Cache Storage, two worker generations: run the old worker, store a user, a book and a
    # cover, then load the new worker into the same storage and install/activate it.
    harness = HARNESS.replace(
        "vm.runInContext(fs.readFileSync(swJs, 'utf8'), ctx, { filename: 'sw.js' });",
        "vm.runInContext(fs.readFileSync(swJs.split(',')[0], 'utf8'), ctx, { filename: 'sw.js' });\n"
        "const swFirst = ctx.self.__bookdoeSw; const firstListeners = JSON.parse(JSON.stringify(Object.keys(listeners)));\n"
        "const loadSecond = () => { for (const k of Object.keys(listeners)) delete listeners[k]; "
        "vm.runInContext('(function(){' + fs.readFileSync(swJs.split(',')[1], 'utf8') + '\\n})()', ctx, { filename: 'sw2.js' }); return ctx.self.__bookdoeSw; };\n"
        "const api0 = { loadSecond, swFirst };")
    harness = harness.replace("const api = { dispatch,", "const api = { ...api0, dispatch,")
    body = """
      const BOOK = '/api/v1/library/w1/files/f1';
      h.setNetwork({'*': {body: 'shell'}, [BOOK]: {body: 'EPUBBYTES'}});
      await h.message({type: 'set-user', uid: 'kid'});
      await h.dispatch(BOOK);                                    // stored in the content cache
      let p; h.listeners.install[0]({ waitUntil: x => { p = x; } }); await p;
      h.listeners.activate[0]({ waitUntil: x => { p = x; } }); await p;
      const namesBefore = (await h.caches.keys()).sort();
      let skips = 0; self.skipWaiting = async () => { skips++; };
      const sw2 = h.loadSecond();
      h.network.length = 0;
      h.listeners.install[0]({ waitUntil: x => { p = x; } }); await p;
      const afterInstall = { names: (await h.caches.keys()).sort(), skips, fetched: h.network.filter(x => x === '/assets/app.js').length };
      await h.message({type: 'skip-waiting'});
      h.listeners.activate[0]({ waitUntil: x => { p = x; } }); await p;
      h.setNetwork({});                                          // offline
      const book = await h.dispatch(BOOK);
      const cached = (await h.message({type: 'is-cached', url: BOOK})).cached;
      const shellKeys = (await (await h.caches.open('bookdoe-shell-newnewnewnew')).keys()).map(x => new URL(x.url).pathname + new URL(x.url).search);
      return {namesBefore, afterInstall, namesAfter: (await h.caches.keys()).sort(), skipsAfter: skips, bookStatus: book.status, bookBody: await book.text(), cached,
              build: sw2.BUILD, shellHasVersionedApp: shellKeys.includes('/assets/app.js?v=newnewnewnew')};
    """
    proc = subprocess.run([NODE, "-e", harness, "--", f"{old},{new}", body], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["namesBefore"] == ["bookdoe-content-v3-kid", "bookdoe-meta-v3", "bookdoe-shell-oldoldoldold"]
    # Installing the new worker adds its shell cache and does NOT take over by itself.
    assert "bookdoe-shell-newnewnewnew" in out["afterInstall"]["names"] and out["afterInstall"]["skips"] == 0
    # After "Update now" (skip-waiting) and activation only the old shell is gone.
    assert out["skipsAfter"] == 1
    assert out["namesAfter"] == ["bookdoe-content-v3-kid", "bookdoe-meta-v3", "bookdoe-shell-newnewnewnew"]
    assert out["bookStatus"] == 200 and out["bookBody"] == "EPUBBYTES" and out["cached"] is True, "offline books survive the update"
    assert out["build"] == "newnewnewnew" and out["shellHasVersionedApp"] is True


@needs_node
def test_sw_drops_media_caches_of_an_incompatible_schema_only():
    sw_path = APP_DIR / "app" / "static" / "sw.js"
    proc = subprocess.run([NODE, "-e", HARNESS, "--", str(sw_path), """
      for (const n of ['bookdoe-content-v2-kid', 'bookdoe-meta-v2', 'bookdoe-vendor-v2', 'bookdoe-content-v3-kid']) await h.caches.open(n);
      let p; h.listeners.activate[0]({ waitUntil: x => { p = x; } }); await p;
      return (await h.caches.keys()).sort();
    """], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == ["bookdoe-content-v3-kid"]


def test_sw_source_has_no_hand_edited_version():
    sw = (APP_DIR / "app" / "static" / "sw.js").read_text()
    assert "const BUILD = 'dev';" in sw and "VERSION = 'v3'" not in sw
    assert "bookdoe-shell-${BUILD}" in sw


# ---------- deploy plumbing ----------

def test_deploy_script_and_compose_pass_the_stamp():
    root = APP_DIR.parent
    deploy = (root / "scripts" / "deploy.sh").read_text()
    assert "GIT_SHA=$(git rev-parse --short=12 HEAD)" in deploy and "date -u +%Y-%m-%dT%H:%MZ" in deploy
    assert deploy.index("export GIT_SHA BUILD_TIME") < deploy.rindex("docker compose up -d --no-build")
    compose = (root / "docker-compose.yml").read_text()
    assert "GIT_SHA: ${GIT_SHA:-dev}" in compose and "BUILD_TIME: ${BUILD_TIME:-}" in compose
    docker = (APP_DIR / "Dockerfile").read_text()
    assert "ARG GIT_SHA=dev" in docker and "ARG BUILD_TIME=" in docker and "BOOKDOE_BUILD_SHA" in docker and "build_info.json" in docker
    assert "GIT_SHA=$(git rev-parse --short=12 HEAD) docker compose up -d --build" in (root / "docs" / "OPERATIONS.md").read_text()


def test_deploy_restarts_the_prebuilt_tagged_images_without_rebuilding():
    """deploy.sh builds and tags images first, then restarts with --no-build so a rollback tag is what runs."""
    lines = (APP_DIR.parent / "scripts" / "deploy.sh").read_text().splitlines()
    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert code.count("docker compose up -d --no-build") == 2
    assert "docker compose up -d --build" not in code


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_docker_image_reports_the_build_arg():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker daemon unavailable")
    tag = "bookdoe-buildinfo-test"
    build = subprocess.run(["docker", "build", "--build-arg", "GIT_SHA=abc123def456", "--build-arg", "BUILD_TIME=2026-10-14T09:30Z", "-t", tag, str(APP_DIR)],
                           capture_output=True, text=True, timeout=1500)
    assert build.returncode == 0, build.stderr[-2000:]
    try:
        run = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", tag, "-c",
                              "from app.services import buildinfo; print(buildinfo.get()['sha'])"], capture_output=True, text=True, timeout=120)
        assert run.stdout.strip() == "abc123def456", run.stderr
        env_unset = subprocess.run(["docker", "run", "--rm", "--entrypoint", "env", tag, "-u", "BOOKDOE_BUILD_SHA", "python", "-c",
                                    "from app.services import buildinfo; print(buildinfo.short())"], capture_output=True, text=True, timeout=120)
        assert "abc123def456" in env_unset.stdout, "build_info.json carries the stamp when the env is gone"
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
