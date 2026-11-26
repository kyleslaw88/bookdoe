"""CODING.md must only cite things that exist, and its factual claims must be true.

Everything is parsed out of CODING.md itself (prose, backticks, fenced blocks and the
layout tree), so a bogus path, make target, command or identifier added to the guide
fails here without this file being edited.
"""
from __future__ import annotations

import ast
import configparser
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import db
from app.services import paths

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC = (REPO_ROOT / "CODING.md").read_text()
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
FILE_EXTS = (".py", ".md", ".txt", ".ini", ".sh", ".mjs", ".js", ".yml")


def _fences(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.S)


def _prose(text: str) -> str:
    return re.sub(r"```[a-z]*\n.*?```", "", text, flags=re.S)


def _spans(text: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", _prose(text))


def _layout_tree() -> list[str]:
    """Full repo-relative paths of every entry in the layout fence, built from indentation."""
    block = next(b for b in _fences(DOC) if re.search(r"^bookdoe/\s*$", b, re.M))
    stack: list[tuple[int, str]] = []
    out = []
    for line in block.splitlines():
        entry = line.split("#")[0].rstrip()
        if not entry.strip():
            continue
        indent = len(entry) - len(entry.lstrip())
        name = entry.strip().split()[0]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full = "/".join([s[1].rstrip("/") for s in stack] + [name.rstrip("/")])
        stack.append((indent, name))
        out.append(full)
    return out


def _repo_basenames() -> set[str]:
    names = set()
    for dirpath, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        names.update(files)
    return names


def _cited_paths() -> set[str]:
    """Slash-containing path tokens anywhere in the doc (prose, spans, fences), plus tree entries."""
    found = set(_layout_tree())
    stripped = re.sub(r"```[a-z]*\n(.*?)```", lambda m: m.group(1) if "bookdoe/" not in m.group(1).split("\n")[0] else "", DOC, flags=re.S)
    for tok in re.findall(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", stripped):
        tok = tok.rstrip(".,;:")
        if "/" in tok and not tok.startswith(("origin/", "http")) and not tok.endswith("/main"):
            found.add(tok)
    return found


def _command_lines() -> list[str]:
    """Commands from fenced blocks (comment stripped) and backticked spans that start like a command."""
    cmds = []
    for block in _fences(DOC):
        if re.search(r"^bookdoe/\s*$", block, re.M):
            continue
        for line in block.splitlines():
            line = re.split(r"\s+#\s", line)[0].strip()
            if line:
                cmds.append(line)
    cmds += [s for s in _spans(DOC) if re.match(r"(cd|make|python|git|PYTHONPATH=\S+ python) ", s)]
    return cmds


def _defined_names(py: Path) -> set[str]:
    names = set()
    for n in ast.walk(ast.parse(py.read_text())):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
    return names


def _shell(cmd: str) -> subprocess.CompletedProcess:
    """Run *cmd* verbatim from the repo root with the test interpreter first on PATH as `python`."""
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    env.pop("PYTHONPATH", None)
    return subprocess.run(cmd, shell=True, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def test_parser_sees_the_layout_and_prose_paths():
    """Guard the guard: if parsing silently found nothing, the checks below would pass vacuously."""
    tree = _layout_tree()
    assert "bookdoe/app/migrations.py" in tree
    assert "bookdoe/app/services" in tree
    cited = _cited_paths()
    # The public snapshot drops the ledger and its CODING.md rule, so the ledger is optional.
    # The public snapshot ships no spec, and CODING.md stops citing it there.
    assert "bookdoe/app/services/paths.py" in cited
    if (REPO_ROOT / "docs" / "VNEXT_SPEC.md").exists():
        assert "docs/VNEXT_SPEC.md" in cited
    assert len(cited) >= 15


def test_every_cited_path_exists():
    # Tokens on a `cd bookdoe && ...` command line are relative to that directory.
    in_bookdoe = {t for c in _command_lines() if c.startswith("cd bookdoe &&") for t in c.split()}
    missing = [p for p in sorted(_cited_paths())
               if not (REPO_ROOT / p).exists() and not (p in in_bookdoe and (REPO_ROOT / "bookdoe" / p).exists())]
    assert not missing, f"CODING.md cites paths that do not exist: {missing}"


def test_every_cited_filename_exists_in_the_repo():
    """Bare names such as `main.py` (backticks, prose, quotes) must be real files."""
    names = _repo_basenames()
    tokens = set(re.findall(r"(?<![\w/.-])([A-Za-z0-9_-]+\.(?:%s))\b" % "|".join(e[1:] for e in FILE_EXTS), DOC))
    assert "main.py" in tokens and "config.py" in tokens
    missing = sorted(t for t in tokens if t not in names)
    assert not missing, f"CODING.md cites files that do not exist: {missing}"


def test_cited_identifiers_are_defined_where_the_doc_says():
    """`_ensure_assets`-style names are read from the doc and found in the file cited alongside."""
    idents = []
    naming = DOC[DOC.index("## Naming and comments"):DOC.index("## PRE-PUSH")]
    for para in re.split(r"\n\s*\n|\n(?=\s*[-\d]+[.)]?\s)", naming):
        spans = re.findall(r"`([^`\n]+)`", para)
        files = [s for s in spans if s.endswith(".py") and "/" in s]
        for s in spans:
            if s != "snake_case" and re.fullmatch(r"_?[a-z]+(?:_[a-z0-9]+)+|[A-Z][a-z]+(?:[A-Z][a-z]+)+|ConfigDict", s):
                idents.append((s, files))
    names = {i for i, _ in idents}
    assert {"_ensure_assets", "_apply_asset_monitoring", "AuthorMonitorRequest"} <= names
    for ident, files in idents:
        assert files, f"`{ident}` is cited without a file path in the same bullet"
        assert any(ident in _defined_names(REPO_ROOT / f) for f in files), f"`{ident}` not defined in {files}"


# ---------------------------------------------------------------------------
# Commands, run verbatim from the repo root
# ---------------------------------------------------------------------------

def test_make_targets_exist_and_test_body_matches_doc():
    makes = sorted({c for c in _command_lines() + _spans(DOC) if c.startswith("make ")})
    assert "make test" in makes
    for c in makes:
        r = _shell(f"{c.split('#')[0].strip().replace('make ', 'make -n ', 1)}")
        assert r.returncode == 0, f"{c}: {r.stderr}"
    body = _shell("make -n test").stdout
    quoted = [s for s in _spans(DOC) if s.startswith("cd ") and "pytest" in s]
    assert any("openreads" in q for q in quoted) and any("bookdoe" in q for q in quoted)
    for q in quoted:
        assert q in body, f"`make test` does not run `{q}`"


def test_documented_commands_run_verbatim_from_repo_root():
    cmds = [c for c in _command_lines() if c.startswith(("python -m compileall", "cd bookdoe && PYTHONPATH=. python -m pytest"))]
    assert any("compileall" in c for c in cmds) and any("pytest" in c for c in cmds)
    for c in cmds:
        r = _shell(c)
        assert r.returncode == 0, f"{c}\n{r.stdout[-800:]}{r.stderr[-800:]}"
    pyt = next(c for c in cmds if "pytest" in c)
    assert "[100%]" in _shell(pyt).stdout, "documented pytest command ran no tests"


def test_git_commands_use_real_git_subcommands():
    real = set(_shell("git --list-cmds=main,nohelpers").stdout.split())
    seen = set()
    for c in _command_lines():
        for part in c.split("&&"):
            w = shlex.split(part)
            if w and w[0] == "git":
                seen.add(w[1])
    assert {"fetch", "rebase"} <= seen
    assert seen <= real


def test_every_python_dash_m_module_is_importable():
    mods = {m for c in _command_lines() for m in re.findall(r"python -m (\w+)", c)}
    assert {"pytest", "compileall"} <= mods
    for m in mods:
        assert _shell(f"python -c 'import {m}'").returncode == 0, m


# ---------------------------------------------------------------------------
# Factual claims
# ---------------------------------------------------------------------------

def test_pytest_ini_claims():
    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "bookdoe" / "pytest.ini")
    assert cfg["pytest"]["testpaths"].strip() == "tests"
    assert any(line.strip().startswith("slow:") for line in cfg["pytest"]["markers"].splitlines())
    assert "testpaths = tests" in DOC and "markers = slow" in DOC


def test_root_modes_match_the_schema():
    modes = re.search(r"`management_mode` is (.+?);", DOC, re.S).group(1)
    documented = set(re.findall(r"`(\w+)`", modes))
    assert documented == {"observe", "adopt", "managed"}
    conn = db.connect()
    try:
        for i, mode in enumerate(sorted(documented)):
            conn.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?)", (f"r{i}", f"n{i}", f"/p{i}", "ebook", mode, "t", "t"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at)"
                " VALUES ('bad','bad','/bad','ebook','readonly','t','t')")
    finally:
        conn.close()


def test_path_guard_rejects_paths_outside_the_root(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    (root / "ok.epub").write_text("x")
    (root / "escape").symlink_to(outside)
    assert paths.resolve_safe(root, root / "ok.epub") == (root / "ok.epub").resolve()
    assert paths.resolve_safe(root, root / ".." / "other") is None
    assert paths.resolve_safe(root, root / "escape" / "f.epub") is None
    assert paths.is_inside_root(outside, root) is False


def test_only_managed_roots_are_written_by_the_importer():
    src = (REPO_ROOT / "bookdoe/app/services/importer.py").read_text()
    assert 'root["management_mode"] != "managed"' in src


def test_migrations_module_is_the_schema_owner():
    assert (REPO_ROOT / "bookdoe/app/migrations.py").is_file()
    assert "MIGRATIONS" in _defined_names(REPO_ROOT / "bookdoe/app/migrations.py") or re.search(
        r"^def migrate|^def run_migrations|^MIGRATIONS|^_MIGRATIONS", (REPO_ROOT / "bookdoe/app/migrations.py").read_text(), re.M)


def test_checklist_covers_every_defect_class():
    checklist = DOC[DOC.index("PRE-PUSH CHECKLIST"):]
    for phrase in ("Called from production code", "Names defined", "Tests drive the real entry point",
                   "Every file the issue names", "Diff within scope", "Rebased on main", "wall-clock sleeps"):
        assert phrase in checklist
    assert len(DOC.splitlines()) < 100
