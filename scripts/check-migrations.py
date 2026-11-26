"""Fail when two registered migration steps share a number or a name.

Reads bookdoe/app/migrations.py and every bookdoe/app/migration_steps/*.py (or the app
directory given as argument) without importing them, so a collision is reported as such rather
than as an import error. A step's name is its function name without the `_migrate_` prefix, the
same name recorded in migration_steps.
Run from anywhere: python scripts/check-migrations.py [APP_DIR]
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "bookdoe" / "app"


def steps(source: str, label: str = "") -> list[tuple[int, str, str]]:
    """(number, name, "file:line") for every function decorated with @register(N)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "register"
                    and len(dec.args) == 1 and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, int)):
                found.append((dec.args[0].value, node.name.removeprefix("_migrate_"), f"{label}:{node.lineno}"))
    return sorted(found)


def sources(app_dir: Path) -> dict[str, str]:
    """label -> source for migrations.py and each migration_steps module."""
    files = [app_dir / "migrations.py", *sorted((app_dir / "migration_steps").glob("*.py"))]
    return {f.relative_to(app_dir).as_posix(): f.read_text(encoding="utf-8") for f in files if f.is_file()}


def collisions(found: list[tuple[int, str, str]]) -> list[str]:
    """One message per number or name used by more than one step."""
    by_number, by_name = defaultdict(list), defaultdict(list)
    for number, name, where in found:
        by_number[number].append(f"{name} ({where})")
        by_name[name].append(f"step {number} ({where})")
    out = [f"step number {n} is registered more than once: {', '.join(v)}" for n, v in by_number.items() if len(v) > 1]
    out += [f"step name '{n}' is used more than once: {', '.join(v)}" for n, v in by_name.items() if len(v) > 1]
    return out


def main(argv: list[str]) -> int:
    app_dir = Path(argv[0]) if argv else DEFAULT
    found = [s for label, src in sources(app_dir).items() for s in steps(src, label)]
    problems = collisions(found)
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print("Take the next free number at merge time (see CONTRIBUTING.md, Migration numbers).", file=sys.stderr)
        return 1
    print(f"ok   {len(found)} migration steps, numbers and names unique")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
