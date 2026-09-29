"""文件工具：读、写、精确编辑、列目录、搜索、复制移动删除。

所有工具都用 ``@register`` 注册（取代 ``@tool``）。新增文件操作工具，只要在本文件追加即可。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path

from langchain_core.tools import tool

from ..config import settings
from ._base import register

MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000
DEFAULT_SEARCH_RESULTS = 80

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tox",
    ".data", ".next", "target", ".gradle",
}


# ============================================================
# 内部工具
# ============================================================

def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _clip_line(line: str, limit: int = MAX_LINE_CHARS) -> str:
    if len(line) <= limit:
        return line
    return line[:limit] + f"… (+{len(line) - limit} 字符)"


def _iter_files(root: Path, recursive: bool, max_files: int = 20000):
    """遍历文件，跳过噪音目录。"""
    count = 0
    if not recursive:
        for child in sorted(root.iterdir()):
            if child.is_file():
                yield child
                count += 1
                if count >= max_files:
                    return
        return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for name in sorted(filenames):
            yield Path(dirpath) / name
            count += 1
            if count >= max_files:
                return


# ============================================================
# 读取
# ============================================================

@register(category="files")
def read_file(path: str, offset: int = 1, limit: int = 500) -> str:
    """读取文本文件内容，返回带行号的文本。

    Args:
        path: 文件路径（绝对路径或相对当前目录）。
        offset: 起始行号，从 1 开始。
        limit: 最多读取的行数。
    """
    p = _resolve(path)
    if not p.exists():
        return f"错误：文件不存在 -> {p}"
    if p.is_dir():
        return f"错误：这是一个目录，请用 list_dir -> {p}"

    limit = max(1, min(int(limit or 500), MAX_READ_LINES))
    offset = max(1, int(offset or 1))

    try:
        lines: list[str] = []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i < offset:
                    continue
                if len(lines) >= limit:
                    break
                lines.append(f"{i:>6}\t{_clip_line(line.rstrip(chr(10)).rstrip(chr(13)))}")
    except Exception as e:
        return f"错误：读取失败 -> {type(e).__name__}: {e}"

    if not lines:
        return f"(文件为空，或 offset={offset} 超出文件末尾) -> {p}"

    total = ""
    try:
        with open(p, "rb") as f:
            total_bytes = f.seek(0, 2)
        total = f"（文件大小 {total_bytes} 字节，显示第 {offset}-{offset + len(lines) - 1} 行）\n"
    except Exception:
        pass

    return f"{total}{p}\n" + "\n".join(lines)


# ============================================================
# 写入
# ============================================================

@register(category="files", requires_confirm=True, risk="medium")
def write_file(path: str, content: str, append: bool = False) -> str:
    """写入文本文件（会自动创建父目录）。

    Args:
        path: 目标文件路径。
        content: 要写入的内容。
        append: True 时追加到文件末尾，False 时覆盖。
    """
    p = _resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8", newline="") as f:
            f.write(content)
        action = "追加" if append else "写入"
        return f"已{action} {len(content)} 字符 -> {p}"
    except Exception as e:
        return f"错误：写入失败 -> {type(e).__name__}: {e}"


@register(category="files", requires_confirm=True, risk="medium")
def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """对文件做精确字符串替换（比整文件重写更安全）。

    用法要点：
      - old_string 必须与文件内容完全一致（含缩进与换行）
      - 默认要求 old_string 在文件中唯一；匹配到多处时会报错，此时请扩大上下文
        或把 replace_all 设为 true

    Args:
        path: 目标文件路径。
        old_string: 要被替换的原文。
        new_string: 替换后的新文本。
        replace_all: True 时替换所有匹配项。
    """
    p = _resolve(path)
    if not p.exists():
        return f"错误：文件不存在 -> {p}"

    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"错误：读取失败 -> {type(e).__name__}: {e}"

    if not old_string:
        return "错误：old_string 不能为空"

    count = text.count(old_string)
    if count == 0:
        return "错误：未找到 old_string。请先 read_file 确认原文（注意缩进与换行需完全一致）。"
    if count > 1 and not replace_all:
        return (
            f"错误：old_string 在文件中出现了 {count} 次，无法确定替换哪一处。"
            "请扩大上下文使其唯一，或把 replace_all 设为 true。"
        )

    updated = text.replace(old_string, new_string) if replace_all else text.replace(
        old_string, new_string, 1
    )
    try:
        p.write_text(updated, encoding="utf-8")
    except Exception as e:
        return f"错误：写入失败 -> {type(e).__name__}: {e}"

    replaced = count if replace_all else 1
    return f"已替换 {replaced} 处 -> {p}"


# ============================================================
# 目录
# ============================================================

@register(category="files")
def list_dir(path: str = ".", recursive: bool = False, max_items: int = 200) -> str:
    """列出目录内容。

    Args:
        path: 目录路径。
        recursive: True 时递归列出（会跳过 .git / node_modules 等噪音目录）。
        max_items: 最多列出多少项。
    """
    p = _resolve(path)
    if not p.exists():
        return f"错误：目录不存在 -> {p}"
    if not p.is_dir():
        return f"错误：不是目录 -> {p}"

    max_items = max(1, min(int(max_items or 200), 2000))
    lines: list[str] = []

    try:
        if recursive:
            root = p
            for f in _iter_files(root, recursive=True, max_files=max_items * 4):
                rel = f.relative_to(root)
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"FILE {rel.as_posix():<60} {size}B")
                if len(lines) >= max_items:
                    lines.append("... (已截断)")
                    break
        else:
            entries = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
            for child in entries[:max_items]:
                if child.is_dir():
                    lines.append(f"DIR  {child.name}/")
                else:
                    try:
                        size = child.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"FILE {child.name:<60} {size}B")
            if len(entries) > max_items:
                lines.append(f"... 另有 {len(entries) - max_items} 项")
    except Exception as e:
        return f"错误：列出失败 -> {type(e).__name__}: {e}"

    header = f"目录 {p}\n"
    return header + ("\n".join(lines) if lines else "(空目录)")


# ============================================================
# 搜索
# ============================================================

@register(category="files")
def search_files(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    max_results: int = DEFAULT_SEARCH_RESULTS,
    case_sensitive: bool = False,
) -> str:
    """在目录中按正则搜索文件内容。

    Args:
        pattern: 正则表达式（Python re 语法）。
        path: 搜索根目录。
        glob: 文件名过滤，如 "*.py"、"*.md"、"*"。
        max_results: 最多返回多少条命中。
        case_sensitive: 是否区分大小写。
    """
    root = _resolve(path)
    if not root.exists() or not root.is_dir():
        return f"错误：目录不存在 -> {root}"

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"错误：正则表达式非法 -> {e}"

    max_results = max(1, min(int(max_results or DEFAULT_SEARCH_RESULTS), 500))
    results: list[str] = []
    scanned = 0

    for f in _iter_files(root, recursive=True):
        scanned += 1
        if not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rel = f.relative_to(root).as_posix()
                results.append(f"{rel}:{lineno}: {_clip_line(line.strip(), 240)}")
                if len(results) >= max_results:
                    results.append(f"... (已达上限 {max_results})")
                    break
        if len(results) >= max_results:
            break

    head = f"在 {root} 下搜索 {pattern!r} (glob={glob}, 扫描 {scanned} 个文件)\n"
    return head + ("\n".join(results) if results else "(无匹配)")


@register(category="files")
def glob_files(pattern: str, path: str = ".") -> str:
    """按 glob 模式查找文件路径。

    Args:
        pattern: glob 模式，如 "**/*.py"、"src/*.ts"、"*.md"。
        path: 搜索根目录。
    """
    root = _resolve(path)
    if not root.exists():
        return f"错误：目录不存在 -> {root}"
    try:
        matches = sorted(root.glob(pattern))
    except Exception as e:
        return f"错误：glob 模式非法 -> {e}"

    if not matches:
        return f"(无匹配) -> {root} / {pattern}"

    lines = []
    for m in matches[:300]:
        try:
            rel = m.relative_to(root).as_posix()
        except ValueError:
            rel = str(m)
        lines.append(f"{'DIR ' if m.is_dir() else 'FILE'} {rel}")
    if len(matches) > 300:
        lines.append(f"... 另有 {len(matches) - 300} 项")
    return f"共 {len(matches)} 项\n" + "\n".join(lines)


# ============================================================
# 变更（高风险：默认要求用户确认）
# ============================================================

@register(category="files", requires_confirm=True, risk="high")
def move_path(source: str, destination: str, overwrite: bool = False) -> str:
    """移动或重命名文件 / 目录。

    Args:
        source: 源路径。
        destination: 目标路径。
        overwrite: 目标已存在时是否覆盖。
    """
    src, dst = _resolve(source), _resolve(destination)
    if not src.exists():
        return f"错误：源路径不存在 -> {src}"
    if dst.exists() and not overwrite:
        return f"错误：目标已存在（如需覆盖请把 overwrite 设为 true）-> {dst}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))
        return f"已移动 -> {dst}"
    except Exception as e:
        return f"错误：移动失败 -> {type(e).__name__}: {e}"


@register(category="files", requires_confirm=True, risk="medium")
def copy_path(source: str, destination: str, overwrite: bool = False) -> str:
    """复制文件或目录。

    Args:
        source: 源路径。
        destination: 目标路径。
        overwrite: 目标已存在时是否覆盖。
    """
    src, dst = _resolve(source), _resolve(destination)
    if not src.exists():
        return f"错误：源路径不存在 -> {src}"
    if dst.exists() and not overwrite:
        return f"错误：目标已存在 -> {dst}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        return f"已复制 -> {dst}"
    except Exception as e:
        return f"错误：复制失败 -> {type(e).__name__}: {e}"


@register(category="files", requires_confirm=True, risk="high")
def delete_path(path: str, recursive: bool = False) -> str:
    """删除文件或目录。

    注意：这是不可逆操作。删除目录必须显式把 recursive 设为 true。

    Args:
        path: 要删除的路径。
        recursive: 删除目录时是否连同内容一起删除。
    """
    p = _resolve(path)
    if not p.exists():
        return f"错误：路径不存在 -> {p}"

    try:
        if p.is_dir():
            if not recursive:
                return (
                    f"错误：{p} 是目录。如果确实要连同内容一起删除，"
                    "请把 recursive 设为 true。"
                )
            shutil.rmtree(p)
            return f"已删除目录 -> {p}"
        p.unlink()
        return f"已删除文件 -> {p}"
    except Exception as e:
        return f"错误：删除失败 -> {type(e).__name__}: {e}"


# 兼容旧导入（让 ``from v3.tools.file_tools import read_file`` 仍可用）
def __getattr__(name: str):
    """通过包内 import 重定向到 _base 注册表里的同名函数。"""
    from ._base import get as _get

    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


FILE_TOOLS = [
    "read_file", "write_file", "edit_file", "list_dir",
    "search_files", "glob_files", "move_path", "copy_path", "delete_path",
]

__all__ = [
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "search_files",
    "glob_files",
    "move_path",
    "copy_path",
    "delete_path",
    "FILE_TOOLS",
]