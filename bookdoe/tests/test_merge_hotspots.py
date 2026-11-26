"""Guards for the files every feature branch used to edit: UI gate plan, change notes, migration numbers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

# The checks `make uigate` ran on the shared instance before the plan was discovered by file name.
# The order matters: ui-managed-build copies into the managed library and ui-merge/ui-duplicates change works,
# so they stay after the read-only checks on the shared seeded instance.
_ALL = ["ui-overflow.mjs", "ui-mobile-nav.mjs", "ui-drawer-scroll.mjs", "ui-player.mjs", "ui-build.mjs", "ui-queue.mjs",
        "ui-convert.mjs", "ui-asset-buttons.mjs", "ui-files.mjs", "ui-merge.mjs", "ui-library-filters.mjs",
        "ui-duplicates.mjs", "ui-managed-build.mjs"]
_MOBILE_ONLY = {"ui-drawer-scroll.mjs", "ui-mobile-nav.mjs", "ui-overflow.mjs"}
BEFORE = {360: _ALL, 390: _ALL, 1280: [c for c in _ALL if c not in _MOBILE_ONLY]}

ADDED_SINCE = {"ui-player-acquisition.mjs"}

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def _plan(stdout: str) -> dict[int, list[str]]:
    out = {}
    for line in stdout.strip().splitlines():
        width, checks = line.split(":", 1)
        out[int(width)] = checks.split()
    return out


@needs_node
def test_ui_gate_plan_is_the_same_checks_in_the_same_order_as_before():
    r = _run("node", "scripts/ui-gate.mjs", "--list")
    assert r.returncode == 0, r.stderr
    # Checks added later by file name (not by editing the plan) are left out of the comparison.
    plan = {w: [c for c in checks if c not in ADDED_SINCE] for w, checks in _plan(r.stdout).items()}
    assert plan == BEFORE


@needs_node
def test_ui_gate_discovers_a_new_script_by_its_header(tmp_path):
    for f in SCRIPTS.glob("ui-*.mjs"):
        shutil.copy(f, tmp_path / f.name)
    (tmp_path / "ui-brand-new.mjs").write_text("// ui-gate: widths=390,1280\nconsole.log('x');\n")
    r = _run("node", "scripts/ui-gate.mjs", "--list", str(tmp_path))
    assert r.returncode == 0, r.stderr
    plan = _plan(r.stdout)
    assert "ui-brand-new.mjs" in plan[390] and "ui-brand-new.mjs" in plan[1280]
    assert "ui-brand-new.mjs" not in plan[360]


@needs_node
def test_ui_gate_orders_by_the_order_key_then_file_name(tmp_path):
    for name, header in [("ui-zed.mjs", "widths=390 order=5"), ("ui-alpha.mjs", "widths=390 order=20"),
                         ("ui-beta.mjs", "widths=390 order=20"), ("ui-last.mjs", "widths=390")]:
        (tmp_path / name).write_text(f"// ui-gate: {header}\n")
    r = _run("node", "scripts/ui-gate.mjs", "--list", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _plan(r.stdout)[390] == ["ui-zed.mjs", "ui-alpha.mjs", "ui-beta.mjs", "ui-last.mjs"]


@needs_node
def test_ui_gate_refuses_a_script_with_no_header_and_no_entry(tmp_path):
    for f in SCRIPTS.glob("ui-*.mjs"):
        shutil.copy(f, tmp_path / f.name)
    (tmp_path / "ui-forgotten.mjs").write_text("console.log('x');\n")
    r = _run("node", "scripts/ui-gate.mjs", "--list", str(tmp_path))
    assert r.returncode == 2
    assert "ui-forgotten.mjs" in r.stderr


def test_migration_check_passes_on_the_real_file():
    r = _run(sys.executable, "scripts/check-migrations.py")
    assert r.returncode == 0, r.stderr


def _app_copy(tmp_path: Path, step_files: dict[str, str]) -> Path:
    """A copy of bookdoe/app's migration files plus extra step modules."""
    app = tmp_path / "app"
    shutil.copytree(REPO / "bookdoe" / "app" / "migration_steps", app / "migration_steps")
    shutil.copy(REPO / "bookdoe" / "app" / "migrations.py", app / "migrations.py")
    for name, body in step_files.items():
        (app / "migration_steps" / name).write_text(body, encoding="utf-8")
    return app


def _step(number: int, name: str) -> str:
    func = name.replace("-", "_")
    return f"from ..migrations import register\n\n\n@register({number})\ndef _migrate_{func}(conn):\n    return {{}}\n"


@needs_node
def test_check_esm_checks_the_six_former_modules_and_every_other_non_classic_script():
    """Before the glob the script listed six files by hand; after it, all static/*.js except the classic scripts."""
    before = {"player.js", "consume.js", "convert.js", "merge.js", "dupes.js", "managedbuild.js"}
    classic = {"app.js", "sw.js", "pwa.js"}
    r = _run("sh", "scripts/check-esm.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    checked = {line.split("/")[-1] for line in r.stdout.splitlines() if line.startswith("ok   ")}
    on_disk = {p.name for p in (REPO / "bookdoe" / "app" / "static").glob("*.js")}
    assert checked == on_disk - classic
    assert before <= checked and not (checked & classic)


def test_migration_check_fails_on_a_duplicate_number(tmp_path):
    app = _app_copy(tmp_path, {"one.py": _step(900, "one"), "two.py": _step(900, "two")})
    r = _run(sys.executable, "scripts/check-migrations.py", str(app))
    assert r.returncode == 1
    assert "step number 900 is registered more than once" in r.stderr
    assert "migration_steps/one.py" in r.stderr and "migration_steps/two.py" in r.stderr


def test_migration_check_fails_on_a_number_already_in_migrations_py(tmp_path):
    app = _app_copy(tmp_path, {"late.py": _step(41, "late")})
    r = _run(sys.executable, "scripts/check-migrations.py", str(app))
    assert r.returncode == 1
    assert "step number 41 is registered more than once" in r.stderr


def test_migration_check_fails_on_a_duplicate_name(tmp_path):
    app = _app_copy(tmp_path, {"again.py": _step(900, "duplicate_finder")})
    r = _run(sys.executable, "scripts/check-migrations.py", str(app))
    assert r.returncode == 1
    assert "step name 'duplicate_finder' is used more than once" in r.stderr


def test_a_step_module_in_migration_steps_is_loaded_and_applied_by_the_real_migrate_entry_point(tmp_path):
    """A copy of the app package plus one new step module: the real import path registers it and init_db applies it."""
    app = tmp_path / "app"
    shutil.copytree(REPO / "bookdoe" / "app", app, ignore=shutil.ignore_patterns("__pycache__"))
    (app / "migration_steps" / "zz_probe.py").write_text(
        "from ..migrations import register\n\n\n@register(900)\ndef _migrate_probe_step(conn):\n"
        "    conn.execute('CREATE TABLE probe_step_table (id INTEGER)')\n    return {'created': 1}\n", encoding="utf-8")
    script = (
        "from app import db, migrations as m\n"
        "last = [(v, n) for v, n, _ in m.MIGRATIONS][-2:]\n"
        "assert [v for v, _, _ in m.MIGRATIONS] == sorted(v for v, _, _ in m.MIGRATIONS)\n"
        "assert last[-1] == (900, 'probe_step'), last\n"
        "db.init_db()\n"
        "with db.session() as c:\n"
        "    assert c.execute(\"SELECT name FROM migration_steps WHERE version=900\").fetchone()[0] == 'probe_step'\n"
        "    assert c.execute(\"SELECT 1 FROM sqlite_master WHERE name='probe_step_table'\").fetchone()\n"
        "print('ok')\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "BOOKDOE_DB_PATH": str(tmp_path / "fresh.db"),
           "BOOKDOE_DATA_DIR": str(tmp_path / "data"), "BOOKDOE_BACKUP_DIR": str(tmp_path / "backups"),
           "BOOKDOE_COVER_DIR": str(tmp_path / "covers"), "BOOKDOE_SCHEDULER_ENABLED": "0"}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=tmp_path, env=env)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)


def _scratch_repo(tmp_path: Path) -> Path:
    """A scratch repo whose main holds the real files every feature branch used to edit."""
    repo = tmp_path / "scratch"
    repo.mkdir()
    for rel in ["scripts/ui-gate.mjs", "scripts/check-migrations.py", "scripts/check-esm.sh", "scripts/changelog.py",
                "bookdoe/app/migrations.py", "bookdoe/app/migration_steps/__init__.py", "docs/VNEXT_SPEC.md"]:
        if not (REPO / rel).exists():
            continue  # the public snapshot ships no spec
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, repo / rel)
    for f in SCRIPTS.glob("ui-*.mjs"):
        shutil.copy(f, repo / "scripts" / f.name)
    # The public snapshot ships no change notes, so the folder may be empty or absent here.
    if (REPO / "docs" / "changes").is_dir():
        shutil.copytree(REPO / "docs" / "changes", repo / "docs" / "changes")
    else:
        (repo / "docs" / "changes").mkdir(parents=True)
        (repo / "docs" / "changes" / ".gitkeep").write_text("")  # git does not track an empty folder
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    return repo


def _feature_branch(repo: Path, name: str, number: int) -> None:
    """A branch with what a feature PR adds: one UI check, one change note, one migration step."""
    assert _git(repo, "checkout", "-q", "-b", name, "main").returncode == 0
    (repo / "scripts" / f"ui-{name}.mjs").write_text("// ui-gate: widths=390,1280\nconsole.log('x');\n")
    (repo / "docs" / "changes" / f"2026-10-16-{name}.md").write_text(f"- **2026-10-16**: {name}\n")
    (repo / "bookdoe" / "app" / "migration_steps" / f"{name}.py").write_text(_step(number, name))
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", name).returncode == 0


@pytest.mark.parametrize("first,second", [("feat-a", "feat-b"), ("feat-b", "feat-a")])
def test_two_feature_branches_merge_in_either_order_without_conflict(tmp_path, first, second):
    repo = _scratch_repo(tmp_path)
    _feature_branch(repo, "feat-a", 100)
    _feature_branch(repo, "feat-b", 101)
    assert _git(repo, "checkout", "-q", "main").returncode == 0
    for branch in (first, second):
        merged = _git(repo, "merge", "--no-edit", branch)
        assert merged.returncode == 0, merged.stdout + merged.stderr
    names = {p.name for p in (repo / "bookdoe" / "app" / "migration_steps").glob("feat-*.py")}
    assert names == {"feat-a.py", "feat-b.py"}
    assert _run(sys.executable, str(repo / "scripts" / "check-migrations.py"), str(repo / "bookdoe" / "app")).returncode == 0
    assert _run(sys.executable, str(repo / "scripts" / "changelog.py"), "--check", str(repo / "docs" / "changes")).returncode == 0
    if shutil.which("node"):
        plan = _run("node", str(repo / "scripts" / "ui-gate.mjs"), "--list")
        assert "ui-feat-a.mjs" in plan.stdout and "ui-feat-b.mjs" in plan.stdout, plan.stderr


def test_two_branches_that_pick_the_same_migration_number_merge_but_the_check_fails(tmp_path):
    repo = _scratch_repo(tmp_path)
    _feature_branch(repo, "feat-a", 100)
    _feature_branch(repo, "feat-b", 100)
    assert _git(repo, "checkout", "-q", "main").returncode == 0
    for branch in ("feat-a", "feat-b"):
        assert _git(repo, "merge", "--no-edit", branch).returncode == 0
    r = _run(sys.executable, str(repo / "scripts" / "check-migrations.py"), str(repo / "bookdoe" / "app"))
    assert r.returncode == 1 and "step number 100 is registered more than once" in r.stderr


def test_change_notes_are_valid_and_a_bad_one_is_rejected(tmp_path):
    assert _run(sys.executable, "scripts/changelog.py", "--check").returncode == 0
    (tmp_path / "2026-10-15-ok.md").write_text("- **2026-10-15**: fine\n")
    (tmp_path / "notes.md").write_text("- **2026-10-15**: bad name\n")
    r = _run(sys.executable, "scripts/changelog.py", "--check", str(tmp_path))
    assert r.returncode == 1 and "notes.md" in r.stderr


def test_change_notes_are_concatenated_newest_first(tmp_path):
    (tmp_path / "2026-10-14-a.md").write_text("- **2026-10-14**: older\n")
    (tmp_path / "2026-10-15-b.md").write_text("- **2026-10-15**: newer\n")
    r = _run(sys.executable, "scripts/changelog.py", str(tmp_path))
    assert r.returncode == 0
    assert r.stdout.index("newer") < r.stdout.index("older")
