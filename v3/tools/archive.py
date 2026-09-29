"""压缩 / 解压工具。

支持的格式：zip / tar / tar.gz / tar.bz2 / tar.zst / rar（需要外部 7z）。

默认 ``requires_confirm=True``（会向磁盘写入大量文件）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from ..platform_utils.tools_detect import find
from ._base import register

_SUPPORTED = {".zip", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2",
              ".txz", ".tar.xz", ".tzst", ".tar.zst", ".7z", ".rar"}


def _7z_binary() -> str | None:
    for cand in ("7z.exe" if os.name == "nt" else "7z", "7z"):
        p = find(cand)
        if p:
            return p
    return None


def _detect_kind(path: Path) -> str:
    """按后缀判断压缩类型。"""
    name = path.name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if name.endswith(ext):
            return ext
    if name.endswith(".zip") or name.endswith(".zipx"):
        return "zip"
    if name.endswith(".7z"):
        return "7z"
    if name.endswith(".rar"):
        return "rar"
    if name.endswith(".tar"):
        return "tar"
    if name.endswith(".tgz"):
        return "tgz"
    return "unknown"


@register(category="archive", requires_confirm=True, risk="medium")
def extract_archive(
    path: str,
    dest_dir: str = "",
    overwrite: bool = False,
) -> str:
    """解压一个压缩包到指定目录。

    支持格式：.zip / .tar / .tar.gz / .tar.bz2 / .tar.xz / .tar.zst / .7z / .rar。
    解压时会做基础的「防 zip slip」检查（条目路径超出目标目录会被拒绝）。

    **会向磁盘写入大量文件**，默认要求用户确认。

    Args:
        path: 压缩包路径。
        dest_dir: 解压到哪（默认与压缩包同名目录，置于压缩包同级）。
        overwrite: 目标目录已存在时是否覆盖。
    """
    src = Path(path).expanduser().resolve()
    if not src.exists():
        return f"错误：压缩包不存在 -> {src}"
    if not src.is_file():
        return f"错误：不是文件 -> {src}"

    kind = _detect_kind(src)
    if kind not in _SUPPORTED and kind != "tgz":
        return f"错误：不支持的压缩类型 {kind}"

    dest = Path(dest_dir).expanduser().resolve() if dest_dir else src.parent / (src.stem + "_extracted")
    if dest.exists() and any(dest.iterdir()):
        if not overwrite:
            return f"错误：目标目录已存在且非空 -> {dest}（如需覆盖请设 overwrite=true）"
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        if kind == "zip":
            n = _extract_zip(src, dest)
        elif kind in ("tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "tgz"):
            n = _extract_tar(src, dest)
        elif kind in ("7z", "rar"):
            n = _extract_7z(src, dest, kind)
        else:
            return f"错误：未实现的格式 {kind}"
    except Exception as e:
        return f"错误：解压失败 -> {type(e).__name__}: {e}"

    return f"已解压 {n} 个条目 -> {dest}"


def _safe_join(base: Path, name: str) -> Path:
    """防止 zip slip：拒绝逃出 base 的条目。"""
    target = (base / name).resolve()
    base_resolved = base.resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"条目路径逃出目标目录: {name}")
    return target


def _extract_zip(src: Path, dest: Path) -> int:
    count = 0
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            name = info.filename
            # 跳过目录条目
            if name.endswith("/"):
                _safe_join(dest, name).mkdir(parents=True, exist_ok=True)
                continue
            target = _safe_join(dest, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_f, open(target, "wb") as out_f:
                shutil.copyfileobj(src_f, out_f)
            count += 1
    return count


def _extract_tar(src: Path, dest: Path) -> int:
    count = 0
    mode = (
        "r:*"  # Python 3.12+ 自动选
        if hasattr(tarfile, "TarFile") else "r:*"
    )
    with tarfile.open(str(src), mode=mode) as tf:
        for member in tf.getmembers():
            name = member.name
            target = _safe_join(dest, name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            with extracted, open(target, "wb") as out_f:
                shutil.copyfileobj(extracted, out_f)
            count += 1
    return count


def _extract_7z(src: Path, dest: Path, kind: str) -> int:
    """用 7z 处理 7z / rar。"""
    bin_ = _7z_binary()
    if not bin_:
        raise RuntimeError(
            "本机未找到 7z 可执行文件。请先安装 7-Zip"
            "（winget install 7zip.7zip 或 https://www.7-zip.org/）。"
        )
    if kind == "7z":
        cmd = [bin_, "x", str(src), f"-o{dest}", "-y", "-bso0", "-bsp0"]
    else:
        cmd = [bin_, "x", str(src), f"-o{dest}", "-y", "-bso0", "-bsp0"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"7z 返回 {proc.returncode}：{proc.stdout}\n{proc.stderr}")
    # 数文件数（粗略）
    return sum(1 for _ in dest.rglob("*"))


@register(category="archive")
def list_archive(path: str) -> str:
    """列出压缩包内容（不解压）。

    Args:
        path: 压缩包路径。
    """
    src = Path(path).expanduser().resolve()
    if not src.exists():
        return f"错误：文件不存在 -> {src}"
    kind = _detect_kind(src)
    if kind in ("zip",):
        with zipfile.ZipFile(src) as zf:
            names = zf.namelist()
        return f"{len(names)} 项（前 50）：\n" + "\n".join(names[:50])
    if kind in ("tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "tgz"):
        with tarfile.open(str(src), mode="r:*") as tf:
            names = tf.getnames()
        return f"{len(names)} 项（前 50）：\n" + "\n".join(names[:50])
    if kind in ("7z", "rar"):
        bin_ = _7z_binary()
        if not bin_:
            return "错误：本机没有 7z，无法列出 7z/rar 内容"
        proc = subprocess.run([bin_, "l", str(src)], capture_output=True, text=True, timeout=60)
        return (proc.stdout or proc.stderr)[:2000]
    return f"错误：不支持的格式 {kind}"


def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["extract_archive", "list_archive"]