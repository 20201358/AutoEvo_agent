"""知识库门面。

把「嵌入器 + 向量库 + 分块摄入」组装成一个可用的知识库对象，
供工具层和图的 recall 节点调用。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional, Sequence

from ..config import settings
from .embeddings import BaseEmbedder, get_embedder
from .indexer import ingest_directory, ingest_file, ingest_text
from .vector_store import BaseVectorStore, Hit, get_vector_store

log = logging.getLogger(__name__)


class KnowledgeBase:
    """一个知识库 = 一个嵌入器 + 一个向量库。"""

    def __init__(
        self,
        store: Optional[BaseVectorStore] = None,
        embedder: Optional[BaseEmbedder] = None,
    ):
        self.embedder = embedder or get_embedder()
        self.store = store or get_vector_store(dim=self.embedder.dim)

    # ---------------- 写入 ----------------

    def add_text(self, text: str, source: str = "", metadata: Optional[dict] = None) -> int:
        """写入一段文本，返回块数。"""
        return ingest_text(self.store, self.embedder, text, source=source, metadata=metadata)

    def add_file(self, path: str | Path, metadata: Optional[dict] = None) -> dict:
        """写入一个文件。"""
        return ingest_file(self.store, self.embedder, path, metadata=metadata)

    def add_directory(
        self,
        directory: str | Path,
        recursive: bool = True,
        max_files: int = 200,
    ) -> dict:
        """递归写入目录下的文本文件。"""
        return ingest_directory(
            self.store, self.embedder, directory, recursive=recursive, max_files=max_files
        )

    # ---------------- 检索 ----------------

    def search(
        self,
        query: str,
        k: Optional[int] = None,
        min_score: Optional[float] = None,
        sources: Optional[Sequence[str]] = None,
    ) -> list[Hit]:
        """检索最相关的若干块。"""
        if not query or not query.strip():
            return []
        k = k or settings.RAG_TOP_K
        min_score = settings.RAG_MIN_SCORE if min_score is None else min_score
        if self.store.count() == 0:
            return []
        vec = self.embedder.embed_query(query)
        return self.store.search(vec, k=k, min_score=min_score, sources=sources)

    def search_text(self, query: str, k: Optional[int] = None) -> str:
        """检索并渲染成给 LLM 的文本。"""
        hits = self.search(query, k=k)
        return self.store.format_hits(hits, max_chars=settings.RAG_MAX_CHARS_PER_HIT)

    # ---------------- 维护 ----------------

    def list_sources(self) -> list[dict]:
        return self.store.list_sources()

    def remove_source(self, source: str) -> int:
        return self.store.delete_source(source)

    def count(self) -> int:
        return self.store.count()

    def reset(self) -> None:
        self.store.reset()

    def stats(self) -> dict:
        return {
            "chunks": self.count(),
            "sources": len(self.list_sources()),
            "embedder": self.embedder.name,
            "dim": self.embedder.dim,
            "backend": self.store.backend,
        }

    def __repr__(self) -> str:
        return (
            f"<KnowledgeBase chunks={self.count()} "
            f"backend={self.store.backend} embedder={self.embedder.name}>"
        )


# ============================================================
# 单例
# ============================================================

_kb_lock = threading.Lock()
_kb: Optional[KnowledgeBase] = None


def get_knowledge_base(force_new: bool = False) -> KnowledgeBase:
    """获取全局知识库实例。"""
    global _kb
    with _kb_lock:
        if _kb is None or force_new:
            _kb = KnowledgeBase()
        return _kb


def reset_knowledge_base() -> None:
    """丢弃并重建全局知识库（配置变更后调用）。"""
    global _kb
    with _kb_lock:
        if _kb is not None:
            try:
                if hasattr(_kb.store, "close"):
                    _kb.store.close()
            except Exception:
                pass
        _kb = None


__all__ = ["KnowledgeBase", "get_knowledge_base", "reset_knowledge_base"]
