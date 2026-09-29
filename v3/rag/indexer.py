"""文本分块与知识摄入。

``chunk_text`` 先在段落边界切分，再按窗口大小打包并保留重叠，尽量不切断语义。

``ingest_*`` 系列把内容写进向量库；对一个 source 重复摄入会自动先去重。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..config import settings

log = logging.getLogger(__name__)


# ============================================================
# 分块
# ============================================================

def chunk_text(
    text: str,
    size: int = 800,
    overlap: int = 120,
) -> list[str]:
    """把长文本切成带重叠的块。

    Args:
        text: 原始文本。
        size: 单块目标字符数。
        overlap: 相邻块之间的重叠字符数（0 表示不重叠）。

    Returns:
        块列表；空输入返回空列表。
    """
    if not text or not text.strip():
        return []

    size = max(120, int(size))
    overlap = max(0, min(int(overlap), size // 2))

    # 段落切分（兼容 \r\n）
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p.strip() for p in normalized.split("\n\n")]
    paragraphs = [p for p in paragraphs if p]

    chunks: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for para in paragraphs:
        # 单段本身超长：先冲刷已有缓冲，再硬切
        if len(para) > size:
            flush()
            start = 0
            step = size - overlap if size > overlap else size
            while start < len(para):
                chunks.append(para[start : start + size].strip())
                if start + size >= len(para):
                    break
                start += step
            continue

        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= size:
            buf = f"{buf}\n\n{para}"
        else:
            tail = buf[-overlap:] if overlap else ""
            flush()
            buf = f"{tail}\n\n{para}" if tail else para

    flush()

    # 过滤空白块
    return [c for c in chunks if c.strip()]


# ============================================================
# 摄入
# ============================================================

def ingest_text(
    store,
    embedder,
    text: str,
    source: str = "",
    metadata: Optional[dict] = None,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    replace_source: bool = True,
) -> int:
    """把一段文本切块、向量化、写入向量库。

    Returns:
        写入的块数。
    """
    if not text or not text.strip():
        return 0

    chunks = chunk_text(
        text,
        size=chunk_size or settings.RAG_CHUNK_SIZE,
        overlap=chunk_overlap if chunk_overlap is not None else settings.RAG_CHUNK_OVERLAP,
    )
    if not chunks:
        return 0

    if replace_source and source:
        removed = store.delete_source(source)
        if removed:
            log.debug("摄入前清理 source=%s 的 %d 个旧块", source, removed)

    vectors = embedder.embed_documents(chunks)
    base_meta = dict(metadata or {})
    base_meta.setdefault("source", source)
    base_meta.setdefault("sha1", hashlib.sha1(text.encode("utf-8")).hexdigest())

    metadatas = []
    for i, chunk in enumerate(chunks):
        m = dict(base_meta)
        m["chunk_index"] = i
        m["chunk_total"] = len(chunks)
        m["preview"] = chunk[:120]
        metadatas.append(m)

    return store.add(chunks, vectors, source=source, metadatas=metadatas)


_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".html", ".htm", ".xml", ".csv", ".tsv", ".log", ".sql", ".sh", ".bash",
    ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs",
    ".rb", ".php", ".css", ".scss", ".vue", ".svelte",
}


def is_text_file(path: Path) -> bool:
    """按扩展名快速判断是否为可读文本。"""
    return path.suffix.lower() in _TEXT_SUFFIXES


def ingest_file(
    store,
    embedder,
    path: str | Path,
    metadata: Optional[dict] = None,
    max_bytes: int = 2_000_000,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
) -> dict:
    """摄入单个文件。

    Returns:
        {"source", "chunks", "chars", "skipped"}
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {"source": str(p), "chunks": 0, "chars": 0, "skipped": "不存在或不是文件"}

    try:
        size = p.stat().st_size
    except OSError as e:
        return {"source": str(p), "chunks": 0, "chars": 0, "skipped": f"无法读取大小: {e}"}

    if size > max_bytes:
        return {
            "source": str(p),
            "chunks": 0,
            "chars": 0,
            "skipped": f"文件过大（{size} > {max_bytes} 字节）",
        }

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"source": str(p), "chunks": 0, "chars": 0, "skipped": f"读取失败: {e}"}

    meta = dict(metadata or {})
    meta.setdefault("path", str(p.resolve()))
    meta.setdefault("filename", p.name)
    meta.setdefault("suffix", p.suffix.lower())

    n = ingest_text(
        store,
        embedder,
        text,
        source=str(p.resolve()),
        metadata=meta,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return {"source": str(p.resolve()), "chunks": n, "chars": len(text), "skipped": None}


def ingest_directory(
    store,
    embedder,
    directory: str | Path,
    recursive: bool = True,
    max_files: int = 200,
    max_bytes: int = 2_000_000,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    extra_suffixes: Optional[Iterable[str]] = None,
) -> dict:
    """摄入目录下的文本文件。

    Returns:
        {"files": n, "chunks": n, "skipped": [{"source","reason"}], "results": [...]}
    """
    root = Path(directory)
    if not root.exists() or not root.is_dir():
        return {"files": 0, "chunks": 0, "skipped": [{"source": str(root), "reason": "目录不存在"}], "results": []}

    allowed = set(_TEXT_SUFFIXES)
    if extra_suffixes:
        allowed |= {s.lower() for s in extra_suffixes}

    iterator = root.rglob("*") if recursive else root.glob("*")
    results: list[dict] = []
    skipped: list[dict] = []
    total_chunks = 0
    file_count = 0

    for path in iterator:
        if file_count >= max_files:
            skipped.append({"source": str(root), "reason": f"达到文件数上限 {max_files}"})
            break
        if not path.is_file():
            continue
        if path.suffix.lower() not in allowed:
            continue
        # 跳过常见噪音目录
        parts = {p.lower() for p in path.parts}
        if parts & {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}:
            continue

        res = ingest_file(
            store,
            embedder,
            path,
            max_bytes=max_bytes,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        if res["skipped"]:
            skipped.append({"source": res["source"], "reason": res["skipped"]})
            continue

        file_count += 1
        total_chunks += res["chunks"]
        results.append(res)

    return {
        "files": file_count,
        "chunks": total_chunks,
        "skipped": skipped,
        "results": results,
    }


__all__ = [
    "chunk_text",
    "ingest_text",
    "ingest_file",
    "ingest_directory",
    "is_text_file",
]
