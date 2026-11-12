"""Copies downloaded assets into a Unity project's Assets folder.

Files are first assembled in a hidden '.incoming' folder (Unity ignores names
starting with '.') and then renamed into place, so Unity never imports a
half-copied asset. No scene modifications are made.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path, PurePosixPath

from utils.naming import unique_path

UNITY_EXTENSIONS = {".fbx", ".obj", ".mtl", ".glb"}


class UnityError(Exception):
    pass


def validate_unity_project(project: str | Path) -> str | None:
    """Return an error message, or None if this looks like a Unity project."""
    if not project:
        return "No Unity project selected."
    p = Path(project)
    if not p.is_dir():
        return f"Folder does not exist: {p}"
    if not (p / "Assets").is_dir() or not (p / "ProjectSettings").is_dir():
        return "Not a Unity project (expected 'Assets' and 'ProjectSettings' folders)."
    return None


def resolve_destination(project: str | Path, dest_rel: str) -> Path:
    rel = PurePosixPath(dest_rel.replace("\\", "/").strip("/"))
    if not rel.parts or rel.parts[0] != "Assets" or ".." in rel.parts:
        raise UnityError("Destination folder must be inside 'Assets' (e.g. Assets/Art/Models).")
    return Path(project).joinpath(*rel.parts)


def _rename_dir(src: Path, dest: Path) -> None:
    """Rename a folder, tolerating brief locks from antivirus/indexers on Windows.
    Falls back to moving files one by one."""
    for attempt in range(6):
        try:
            src.rename(dest)
            return
        except PermissionError:
            time.sleep(0.25 * (attempt + 1))
    for f in sorted(src.rglob("*")):
        if f.is_file():
            target = dest / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(target))


def _collect(staging: Path) -> list[Path]:
    files = [f for f in staging.iterdir() if f.is_file() and f.suffix.lower() in UNITY_EXTENSIONS]
    tex = staging / "Textures"
    if tex.is_dir():
        files += [f for f in tex.iterdir() if f.is_file() and not f.name.endswith(".part")]
    return files


def install_to_unity(staging: Path, name: str, project: str | Path, dest_rel: str = "Assets/Art/Models",
                     per_model_folder: bool = True) -> Path:
    err = validate_unity_project(project)
    if err:
        raise UnityError(err)
    root = resolve_destination(project, dest_rel)
    root.mkdir(parents=True, exist_ok=True)
    files = _collect(staging)
    if not any(f.suffix.lower() in (".fbx", ".obj", ".glb") for f in files):
        raise UnityError("No Unity-compatible model file (FBX/OBJ/GLB) was downloaded.")

    incoming = root / f".{name}.incoming"
    if incoming.exists():
        shutil.rmtree(incoming)
    for f in files:
        target = incoming / f.relative_to(staging)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)

    try:
        if per_model_folder:
            final = unique_path(root / name)
            _rename_dir(incoming, final)
            return final
        # Flat layout: models directly in root, textures in root/Textures.
        for f in sorted(incoming.rglob("*")):
            if f.is_file():
                target = unique_path(root / f.relative_to(incoming))
                target.parent.mkdir(parents=True, exist_ok=True)
                f.replace(target)
        return root
    finally:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
