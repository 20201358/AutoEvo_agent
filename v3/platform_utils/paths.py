"""路径工具。"""

from __future__ import annotations

from pathlib import Path


def normalize(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def is_subpath(child: str, parent: str) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def to_posix(path: str) -> str:
    return Path(path).as_posix()


__all__ = ["normalize", "is_subpath", "to_posix"]