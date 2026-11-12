"""Asset naming helpers."""
from __future__ import annotations

import re
from pathlib import Path

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def asset_name_from_path(path: str | Path) -> str:
    """'my tree_01.png' -> 'MyTree01', 'panda.jpg' -> 'Panda'."""
    stem = Path(path).stem
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", stem) if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "Model"
    if name[0].isdigit():
        name = "Model" + name
    if name.upper() in _RESERVED:
        name += "Model"
    return name[:60]


def unique_path(path: Path) -> Path:
    """Return path, or path with _2, _3 ... appended to the stem if it exists."""
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def unique_asset_name(name: str, roots: list[Path]) -> str:
    """Pick a name not used as a folder or file prefix in any of the roots."""
    def taken(n: str) -> bool:
        for root in roots:
            if (root / n).exists() or any(root.glob(f"{n}.*")):
                return True
        return False

    if not taken(name):
        return name
    i = 2
    while taken(f"{name}_{i}"):
        i += 1
    return f"{name}_{i}"
