"""deploy/demo: the entry script seeds /config as described and the compose file is well formed."""
import os
import re
import subprocess
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[2] / "deploy" / "demo"
SCRIPT = DEMO / "demo-entry.sh"


def _run(tmp_path, dbs=("demo.db",), covers=True, **env):
    seed, cfg = tmp_path / "seed", tmp_path / "config"
    seed.mkdir(); cfg.mkdir()
    for d in dbs:
        (seed / d).write_text("seed-db")
    if covers:
        (seed / "covers").mkdir(); (seed / "covers" / "a.jpg").write_text("c")
    (cfg / "stale.txt").write_text("old")
    e = dict(os.environ, SEED_DIR=str(seed), CONFIG_DIR=str(cfg), DEMO_DRY_RUN="1", **env)
    e.pop("SEED_MODE", None)
    r = subprocess.run(["sh", str(SCRIPT)], env=e, capture_output=True, text=True)
    return r, cfg


def test_seed_is_copied_as_bookdoe_db_and_config_wiped(tmp_path):
    r, cfg = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (cfg / "bookdoe.db").read_text() == "seed-db"
    assert (cfg / "covers" / "a.jpg").exists()
    assert not (cfg / "stale.txt").exists()
    m = re.search(r"auto-reset in (\d+)s", r.stdout)
    assert m and 600 < int(m.group(1)) <= 86400 + 600


@pytest.mark.parametrize("dbs", [(), ("a.db", "b.db")])
def test_refuses_unless_exactly_one_seed_db(tmp_path, dbs):
    r, cfg = _run(tmp_path, dbs=dbs, covers=False)
    assert r.returncode != 0
    assert "exactly one .db" in r.stderr
    assert (cfg / "stale.txt").exists()  # nothing wiped on refusal


def test_seed_mode_skips_reset(tmp_path):
    seed, cfg = tmp_path / "seed", tmp_path / "config"
    seed.mkdir(); cfg.mkdir()
    ep = tmp_path / "ep.sh"
    ep.write_text("#!/bin/sh\necho ran-entrypoint \"$@\"\n"); ep.chmod(0o755)
    r = subprocess.run(["sh", str(SCRIPT), "x"], capture_output=True, text=True,
                       env=dict(os.environ, SEED_MODE="1", ENTRYPOINT=str(ep), SEED_DIR=str(seed), CONFIG_DIR=str(cfg)))
    assert r.returncode == 0 and "ran-entrypoint x" in r.stdout


def test_compose_parses_and_has_no_secrets():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((DEMO / "docker-compose.yml").read_text())
    assert set(doc["services"]) >= {"app", "edge"}
    app = doc["services"]["app"]
    assert any(v.endswith("/seed:ro") for v in app["volumes"])
    assert not any(k for k in app["environment"] if "PASSWORD" in k or "SECRET" in k)


def test_edge_conf_balanced():
    t = (DEMO / "edge.conf").read_text()
    assert t.count("{") == t.count("}") and "server" in t
