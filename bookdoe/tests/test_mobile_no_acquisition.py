"""#505 (parent #494): the iOS and Android apps never call an acquisition API path.

The paths come from the routes in app/main.py gated by require_acquisition, so a new gated
route is covered without editing this test.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "bookdoe" / "app" / "main.py"
TREES = [ROOT / "ios", ROOT / "android"]
SKIP_DIRS = {"build", ".gradle", ".build", "DerivedData", "Pods", ".git", "node_modules"}
SOURCE_SUFFIXES = {".swift", ".kt", ".kts", ".java", ".xml", ".json", ".plist", ".yml", ".yaml",
                   ".md", ".txt", ".properties", ".sh", ".gradle", ".strings", ".html"}

ROUTE = re.compile(r'@app\.\w+\(\s*"(/api/v1/[^"]+)"[^\n]*require_acquisition')


def acquisition_paths():
    return sorted(set(ROUTE.findall(MAIN.read_text())))


def path_regex(route):
    """A route like /api/v1/queue/{id} as a regex matching a path literal in client code.

    Mobile clients use a base URL, so the /api/v1 prefix is optional when the literal starts
    with the path. {param} matches any interpolation: \\(id), $id, ${id}, %s.
    """
    tail = route[len("/api/v1"):]
    parts = [r"[^/\"'\s]+" if p.startswith("{") else re.escape(p) for p in tail.split("/")]
    body = "/".join(parts)
    return re.compile(r"(?:/api/v1" + body + r"|[\"']" + body + r")(?![\w-])")


def scan(trees):
    regexes = [(r, path_regex(r)) for r in acquisition_paths()]
    hits = []
    for tree in trees:
        for f in tree.rglob("*"):
            if not f.is_file() or f.suffix not in SOURCE_SUFFIXES or SKIP_DIRS & set(f.parts):
                continue
            for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                for route, rx in regexes:
                    if rx.search(line):
                        hits.append(f"{f.relative_to(tree)}:{n}: {route}")
    return hits


def test_route_list_is_found():
    paths = acquisition_paths()
    assert "/api/v1/queue" in paths and "/api/v1/indexers" in paths and len(paths) >= 14


def test_mobile_trees_have_no_acquisition_paths():
    assert scan(TREES) == []


def test_guard_fails_on_seeded_path(tmp_path):
    (tmp_path / "Seed.kt").write_text('val a = get("/api/v1/indexers")\n')
    (tmp_path / "Seed.swift").write_text('try await post("/library/\\(id)/search/ebook")\n')
    (tmp_path / "Rel.swift").write_text('get("/releases/\\(rid)/grab")\n')
    hits = scan([tmp_path])
    assert len(hits) == 3, hits


def test_guard_ignores_unrelated_paths(tmp_path):
    (tmp_path / "Ok.kt").write_text('get("/library/w1/files/f1"); "fake://downloads/x"; path="downloads/"\n')
    assert scan([tmp_path]) == []


def test_guard_catches_seeded_file_in_real_trees():
    seed = ROOT / "android" / "core" / "SeedAcquisitionGuard.kt"
    seed.write_text('val q = "/api/v1/queue"\n')
    try:
        assert any("SeedAcquisitionGuard.kt:1" in h for h in scan(TREES))
    finally:
        seed.unlink()
