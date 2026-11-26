"""File-operation plans (Part XXIV): PLAN and PREVIEW only.

This module is read-only by design. It records what *would* happen to a library file
and why a plan is (not) executable; it never renames, moves, writes or deletes
anything. Execution, verification and audit belong to the P5-11 executor, which must
re-validate every step (root, mutability, drift against ``expected_json``) because a
stored plan is never trusted. ``tests/test_file_plans.py`` scans this file to keep it
that way.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .. import db
from . import files as files_service
from . import importer
from . import paths as path_guard

PLAN_TTL_SECONDS = 24 * 3600
CANCELLABLE = ("draft", "previewed", "approved")
ROOT_MODE_LABEL = {"adopt": "adopted root", "observe": "observe root", "managed": "managed root"}


class PlanError(ValueError):
    """The request itself is malformed (bad kind or params): HTTP 400, not a refusal."""


class PlanNotFound(LookupError):
    """Unknown plan or file id: HTTP 404."""


class PlanConflict(RuntimeError):
    """The plan is in a state that does not allow the action: HTTP 409."""


# ---------------------------------------------------------------------------
# rename_move builder
# ---------------------------------------------------------------------------

RENAME_MOVE_PARAMS = {"file_id", "dest_relative", "use_naming_template", "dest_root_id", "retag"}


def _validated_rename_move_params(params: dict[str, Any]) -> dict[str, Any]:
    unknown = set(params) - RENAME_MOVE_PARAMS
    if unknown:
        raise PlanError(f"Unknown rename_move parameter(s): {', '.join(sorted(unknown))}")
    file_id = params.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise PlanError("file_id is required")
    dest_relative = params.get("dest_relative")
    template = params.get("use_naming_template", False)
    if not isinstance(template, bool):
        raise PlanError("use_naming_template must be a boolean")
    if bool(dest_relative) == template:
        raise PlanError("Give exactly one of dest_relative or use_naming_template: true")
    if dest_relative is not None and not isinstance(dest_relative, str):
        raise PlanError("dest_relative must be a string")
    dest_root_id = params.get("dest_root_id")
    if dest_root_id is not None and not isinstance(dest_root_id, str):
        raise PlanError("dest_root_id must be a string")
    retag = params.get("retag", False)
    if not isinstance(retag, bool):
        raise PlanError("retag must be a boolean")
    out: dict[str, Any] = {"file_id": file_id, "retag": retag}
    if dest_relative:
        out["dest_relative"] = dest_relative
    if template:
        out["use_naming_template"] = True
    if dest_root_id:
        out["dest_root_id"] = dest_root_id
    return out


def _template_relative(file_row: dict[str, Any], kind: str) -> str:
    """Folder/file for *file_row* from the naming template, as ``preview_name`` renders it."""
    template = dict(db.one("SELECT * FROM naming_templates WHERE media_kind=?", (kind,)) or {})
    if not template:
        raise ValueError(f"No naming template for media kind '{kind}'")
    ctx = importer._work_context(file_row["work_id"])
    opts = importer._template_opts(template)
    ext = Path(file_row["path"]).suffix.lower().lstrip(".")
    siblings = db.one("SELECT COUNT(*) AS n FROM media_files WHERE asset_id=?", (file_row["asset_id"],))["n"]
    part = file_row["part_number"] if siblings > 1 else None
    file_tpl = template["multipart_template"] if part and template.get("multipart_template") else template["file_template"]
    folder = importer._render(template["folder_template"], ctx, ext="", part=None, **opts)
    name = importer._render(file_tpl, ctx, ext=ext, part=part, **opts)
    return str(Path(folder) / name)


def _first_missing_ancestor_blocker(dest: Path, root: Path) -> bool:
    """True if an existing ancestor of *dest* (inside the root) is not a directory."""
    for parent in dest.parents:
        if parent == root or root not in parent.parents:
            return False
        if parent.exists() or parent.is_symlink():
            return not parent.is_dir()
    return False


def _build_rename_move(params: dict[str, Any]) -> dict[str, Any]:
    params = _validated_rename_move_params(params)
    file_row = db.one(
        """SELECT f.*, a.kind AS asset_kind, a.work_id AS work_id
           FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE f.id=?""",
        (params["file_id"],),
    )
    if not file_row:
        raise PlanNotFound("File not found")

    refusals: list[str] = []
    warnings: list[str] = []
    source = Path(file_row["path"])

    # Source: the containing root row is the authority, never media_files.managed.
    src_root_res = files_service._resolve_root(file_row["path"])
    mutable, reason = files_service.is_mutable(file_row)
    if not mutable:
        refusals.append(f"Source is not mutable: {reason}")
    src_resolved: Path | None = None
    expected: dict[str, Any] = {}
    if src_root_res.root is not None:
        src_resolved = path_guard.resolve_safe(Path(src_root_res.root["path"]), source)
    if src_resolved is None or not src_resolved.is_file():
        if mutable:
            refusals.append("Source file is missing on disk")
        src_resolved = None
    else:
        st = src_resolved.stat()
        expected = {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "fingerprint": file_row["fingerprint"],
            "root_id": src_root_res.root["id"],
        }
        if not file_row["verified_at"]:
            warnings.append("Source file has not been verified since it was discovered")

    # Destination root: explicit, or the root that already contains the source.
    dest_root: dict[str, Any] | None
    if params.get("dest_root_id"):
        dest_root = db.one("SELECT * FROM root_folders WHERE id=?", (params["dest_root_id"],))
        if dest_root is None:
            refusals.append("Destination root does not exist")
    else:
        # _resolve_root returns a narrow row; the media_kind check needs the full one.
        dest_root = db.one("SELECT * FROM root_folders WHERE id=?", (src_root_res.root["id"],)) if src_root_res.root else None
        if dest_root is None:
            refusals.append("Destination root cannot be determined: the source is outside every root")
    if dest_root is not None:
        if not dest_root["enabled"]:
            refusals.append("Destination root is disabled")
        elif dest_root["management_mode"] != "managed":
            label = ROOT_MODE_LABEL.get(dest_root["management_mode"], dest_root["management_mode"])
            refusals.append(f"Destination root is not managed ({label})")
        if dest_root["media_kind"] not in (file_row["asset_kind"], "mixed"):
            refusals.append(
                f"Destination root holds {dest_root['media_kind']} files, but this file is an {file_row['asset_kind']}"
            )

    # Destination path.
    rel: str | None = params.get("dest_relative")
    if params.get("use_naming_template"):
        try:
            rel = _template_relative(file_row, file_row["asset_kind"])
        except ValueError as exc:
            refusals.append(f"Naming template could not be rendered: {exc}")
            rel = None
    dest_abs: Path | None = None
    if rel is not None and dest_root is not None:
        root_path = Path(dest_root["path"])
        if Path(rel).is_absolute() or not rel.strip():
            refusals.append("Destination escapes the root: it must be a relative path")
        else:
            # No lexical normpath first: resolve_safe resolves on disk, so "link/../x" lands
            # where the filesystem would put it (next to the link's target), not next to the link.
            dest_abs = path_guard.resolve_safe(root_path, root_path / rel)
            if dest_abs is None or dest_abs == root_path.resolve():
                refusals.append("Destination escapes the root")
                dest_abs = None
            elif src_resolved is not None and dest_abs == src_resolved:
                refusals.append("Destination equals the source")
            elif dest_abs.exists() or dest_abs.is_symlink():
                refusals.append("Destination already exists")
            elif _first_missing_ancestor_blocker(dest_abs, root_path.resolve()):
                refusals.append("A parent of the destination is not a directory")
            elif not dest_abs.parent.exists():
                warnings.append("Destination folder does not exist yet and would be created")
            if dest_abs is not None and src_resolved is not None and dest_abs.suffix.lower() != src_resolved.suffix.lower():
                warnings.append("Destination changes the file extension")

    effects: list[str] = []
    if dest_abs is not None:
        same_dir = src_resolved is not None and dest_abs.parent == src_resolved.parent
        effects.append("rename" if same_dir else "move")
    else:
        effects.append("rename/move")
    effects.append("database update")
    if params["retag"]:
        effects.append("retag")

    step = {
        "op": "move" if "move" in effects else "rename",
        "file_id": file_row["id"],
        "source_path": file_row["path"],
        "dest_path": str(dest_abs) if dest_abs is not None else None,
        "expected": {**expected, "dest_root_id": dest_root["id"] if dest_root else None} if expected else {},
        "effects": {"effects": effects, "warnings": warnings},
    }
    after = str(dest_abs) if dest_abs is not None else "an unresolved destination"
    return {
        "params": params,
        "steps": [step],
        "refusals": list(dict.fromkeys(refusals)),
        "summary": f"{effects[0].capitalize()} {file_row['path']} -> {after}",
    }


# kind -> builder(params) -> {"params", "steps", "refusals", "summary"}. Builders only
# read; a new plan kind is added by registering it here.
BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "rename_move": _build_rename_move,
}


# ---------------------------------------------------------------------------
# Plan storage and API
# ---------------------------------------------------------------------------

def _serialize(plan: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    refusals = db.json_load(plan["refusals_json"], [])
    expired = plan["expires_at"] <= db.now()
    out = {
        "id": plan["id"],
        "kind": plan["kind"],
        "status": plan["status"],
        "params": db.json_load(plan["params_json"], {}),
        "summary": plan["summary"],
        "refusals": refusals,
        "created_by": plan["created_by"],
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
        "expires_at": plan["expires_at"],
        "expired": expired,
        # A hint for the UI only: the executor re-validates everything regardless.
        "executable": plan["status"] in ("previewed", "approved") and not refusals and not expired,
        "steps": [
            {
                "id": s["id"], "ord": s["ord"], "op": s["op"], "file_id": s["file_id"],
                "source_path": s["source_path"], "dest_path": s["dest_path"],
                "expected": db.json_load(s["expected_json"], {}),
                "effects": db.json_load(s["effects_json"], {}),
                "status": s["status"],
            }
            for s in steps
        ],
    }
    out["preview"] = preview(out)
    return out


def preview(plan: dict[str, Any]) -> dict[str, Any]:
    """Before/after paths, expected effects, warnings and refusals for a serialized plan."""
    items = []
    for s in plan["steps"]:
        info = s.get("effects") or {}
        items.append({
            "file_id": s["file_id"],
            "before": s["source_path"],
            "after": s["dest_path"],
            "effects": info.get("effects", []),
            "warnings": info.get("warnings", []),
        })
    return {
        "items": items,
        "effects": [e for i in items for e in i["effects"]],
        "warnings": [w for i in items for w in i["warnings"]],
        "refusals": plan["refusals"],
    }


def get_plan(plan_id: str) -> dict[str, Any]:
    plan = db.one("SELECT * FROM file_operation_plans WHERE id=?", (plan_id,))
    if not plan:
        raise PlanNotFound("Plan not found")
    steps = db.all("SELECT * FROM file_operation_steps WHERE plan_id=? ORDER BY ord", (plan_id,))
    return _serialize(plan, steps)


def list_plans(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    where, args = ("WHERE status=?", (status,)) if status else ("", ())
    rows = db.all(
        f"SELECT id FROM file_operation_plans {where} ORDER BY created_at DESC LIMIT ?",  # nosec B608
        (*args, max(1, min(limit, 500))),
    )
    return [get_plan(r["id"]) for r in rows]


def create_plan(kind: str, params: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    """Build, store and preview a plan. Refusals are stored on the plan, not raised."""
    builder = BUILDERS.get(kind)
    if builder is None:
        raise PlanError(f"Unknown plan kind '{kind}'")
    built = builder(params)
    plan_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO file_operation_plans(id,kind,status,params_json,summary,refusals_json,created_by,created_at,updated_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (plan_id, kind, "previewed", db.json_dump(built["params"]), built["summary"],
             db.json_dump(built["refusals"]), (user or {}).get("id"), t, t, t + PLAN_TTL_SECONDS),
        )
        for i, s in enumerate(built["steps"]):
            conn.execute(
                "INSERT INTO file_operation_steps(id,plan_id,ord,op,file_id,source_path,dest_path,expected_json,effects_json,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                (db.uid(), plan_id, i, s["op"], s["file_id"], s["source_path"], s["dest_path"],
                 db.json_dump(s["expected"]), db.json_dump(s["effects"])),
            )
    return get_plan(plan_id)


def cancel_plan(plan_id: str) -> dict[str, Any]:
    """Mark a not-yet-running plan cancelled. Touches only the plan row."""
    with db.tx() as conn:
        row = conn.execute("SELECT status FROM file_operation_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise PlanNotFound("Plan not found")
        if row["status"] not in CANCELLABLE:
            raise PlanConflict(f"A plan that is {row['status']} cannot be cancelled")
        conn.execute("UPDATE file_operation_plans SET status='cancelled',updated_at=? WHERE id=?", (db.now(), plan_id))
    return get_plan(plan_id)
