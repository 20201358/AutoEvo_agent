"""向量库后端。

两个实现，统一接口：

- ``SqliteVecStore``   基于 sqlite-vec 扩展的嵌入式向量库。持久化、支持 KNN、
                        支持按 source 过滤与删除。默认后端。
- ``NumpyVectorStore`` 纯 numpy 暴力检索。sqlite-vec 不可用时的兜底。

统一接口：
    add(texts, vectors, source, metadatas) -> int
    search(vector, k, min_score, sources)  -> list[Hit]
    delete_source(source) -> int
    list_sources() -> list[dict]
    count() -> int
    reset() -> None
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

from ..config import settings

log = logging.getLogger(__name__)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class Hit:
    """一条检索命中。"""

    text: str
    score: float
    source: str = ""
    chunk_index: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "score": round(self.score, 4),
            "source": self.source,
            "chunk_index": self.chunk_index,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        return f"<Hit score={self.score:.3f} source={self.source!r} chars={len(self.text)}>"


# ============================================================
# 抽象接口
# ============================================================

class BaseVectorStore:
    """向量库接口。"""

    backend: str = "base"

    def add(
        self,
        texts: Sequence[str],
        vectors: np.ndarray,
        source: str = "",
        metadatas: Optional[Sequence[dict]] = None,
    ) -> int:
        """写入若干分块，返回写入条数。"""
        raise NotImplementedError

    def search(
        self,
        vector: np.ndarray,
        k: int = 4,
        min_score: float = 0.0,
        sources: Optional[Sequence[str]] = None,
    ) -> list[Hit]:
        raise NotImplementedError

    def delete_source(self, source: str) -> int:
        raise NotImplementedError

    def list_sources(self) -> list[dict]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def format_hits(self, hits: Sequence[Hit], max_chars: int = 1600) -> str:
        """把命中渲染成给 LLM 看的文本块。"""
        if not hits:
            return "(无相关知识)"
        parts: list[str] = []
        for i, h in enumerate(hits, 1):
            body = h.text if len(h.text) <= max_chars else h.text[:max_chars] + "…"
            parts.append(
                f"[{i}] 来源: {h.source or '(内联)'} | 相似度: {h.score:.3f}\n{body}"
            )
        return "\n\n".join(parts)


# ============================================================
# sqlite-vec 后端
# ============================================================

class SqliteVecStore(BaseVectorStore):
    """SQLite + sqlite-vec 嵌入式向量库。"""

    backend = "sqlite_vec"

    def __init__(self, db_path: str | Path, dim: int, table_prefix: str = "kb"):
        self.db_path = Path(db_path)
        self.dim = int(dim)
        self.prefix = table_prefix
        self._lock = threading.RLock()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._load_extension()
        self._ensure_schema()

    # ---------------- 初始化 ----------------

    def _load_extension(self) -> None:
        import sqlite_vec

        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        version = self._conn.execute("select vec_version()").fetchone()[0]
        log.debug("sqlite-vec 已加载: %s", version)

    @property
    def _chunk_table(self) -> str:
        return f"{self.prefix}_chunks"

    @property
    def _vec_table(self) -> str:
        return f"{self.prefix}_vec"

    def _ensure_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._chunk_table} (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    source      TEXT NOT NULL DEFAULT '',
                    chunk_index INTEGER NOT NULL DEFAULT 0,
                    text        TEXT NOT NULL,
                    metadata    TEXT NOT NULL DEFAULT '{{}}',
                    dim         INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.prefix}_source "
                f"ON {self._chunk_table}(source)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS {p}_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                )
                """.format(p=self.prefix)
            )
            self._conn.commit()

            stored_dim = self._get_meta("dim")
            if stored_dim and int(stored_dim) != self.dim:
                log.warning(
                    "向量维度从 %s 变为 %d，重建索引", stored_dim, self.dim
                )
                cur.execute(f"DROP TABLE IF EXISTS {self._vec_table}")
                cur.execute(f"DELETE FROM {self._chunk_table}")
                self._conn.commit()

            cur.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {self._vec_table}
                USING vec0(embedding float[{self.dim}])
                """
            )
            self._set_meta("dim", str(self.dim))
            self._conn.commit()

    def _get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            f"SELECT value FROM {self.prefix}_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            f"INSERT INTO {self.prefix}_meta(key, value) VALUES(?, ?) "
            f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------------- 写入 ----------------

    def add(
        self,
        texts: Sequence[str],
        vectors: np.ndarray,
        source: str = "",
        metadatas: Optional[Sequence[dict]] = None,
    ) -> int:
        if len(texts) == 0:
            return 0
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape[1] != self.dim:
            raise ValueError(
                f"向量维度不匹配：期望 {self.dim}，实际 {vectors.shape[1]}"
            )

        metas = list(metadatas) if metadatas else [{} for _ in texts]
        written = 0

        with self._lock:
            cur = self._conn.cursor()
            for i, (text, vec) in enumerate(zip(texts, vectors)):
                meta = dict(metas[i] or {})
                cur.execute(
                    f"INSERT INTO {self._chunk_table}"
                    f"(source, chunk_index, text, metadata, dim) VALUES(?,?,?,?,?)",
                    (
                        source,
                        int(meta.get("chunk_index", i)),
                        text,
                        json.dumps(meta, ensure_ascii=False),
                        self.dim,
                    ),
                )
                rowid = cur.lastrowid
                nvec = _normalize(vec)
                cur.execute(
                    f"INSERT INTO {self._vec_table}(rowid, embedding) VALUES(?, ?)",
                    (rowid, nvec.tobytes()),
                )
                written += 1
            self._conn.commit()

        return written

    # ---------------- 检索 ----------------

    def search(
        self,
        vector: np.ndarray,
        k: int = 4,
        min_score: float = 0.0,
        sources: Optional[Sequence[str]] = None,
    ) -> list[Hit]:
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        if vec.shape[0] != self.dim:
            raise ValueError(
                f"查询向量维度不匹配：期望 {self.dim}，实际 {vec.shape[0]}"
            )
        nvec = _normalize(vec)

        # 多取一些候选，便于按 source 过滤后仍有足够结果
        fetch_k = max(k * 4, k) if sources else k
        limit_clause = "AND v.rowid IN (SELECT id FROM {t} WHERE source IN ({ph}))".format(
            t=self._chunk_table,
            ph=",".join("?" * len(sources)),
        ) if sources else ""

        sql = f"""
            SELECT c.id, c.source, c.chunk_index, c.text, c.metadata, v.distance
            FROM {self._vec_table} v
            JOIN {self._chunk_table} c ON c.id = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            {limit_clause}
            ORDER BY v.distance
        """
        params: list = [nvec.tobytes(), fetch_k]
        if sources:
            params.extend(sources)

        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # 老版本不支持 k = ? 约束，退回 LIMIT 写法
            sql = f"""
                SELECT c.id, c.source, c.chunk_index, c.text, c.metadata, v.distance
                FROM {self._vec_table} v
                JOIN {self._chunk_table} c ON c.id = v.rowid
                WHERE v.embedding MATCH ?
                {limit_clause}
                ORDER BY v.distance LIMIT ?
            """
            params = [nvec.tobytes()]
            if sources:
                params.extend(sources)
            params.append(fetch_k)
            rows = self._conn.execute(sql, params).fetchall()

        hits: list[Hit] = []
        for r in rows:
            # 向量已 L2 归一化，L2 距离 d 与余弦相似度满足 cos = 1 - d²/2
            d = float(r["distance"])
            score = 1.0 - (d * d) / 2.0
            if score < min_score:
                continue
            try:
                meta = json.loads(r["metadata"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            hits.append(
                Hit(
                    text=r["text"],
                    score=score,
                    source=r["source"],
                    chunk_index=int(r["chunk_index"]),
                    metadata=meta,
                )
            )
            if len(hits) >= k:
                break
        return hits

    # ---------------- 维护 ----------------

    def delete_source(self, source: str) -> int:
        with self._lock:
            cur = self._conn.cursor()
            ids = [
                r["id"]
                for r in cur.execute(
                    f"SELECT id FROM {self._chunk_table} WHERE source = ?", (source,)
                ).fetchall()
            ]
            for rid in ids:
                cur.execute(f"DELETE FROM {self._vec_table} WHERE rowid = ?", (rid,))
            cur.execute(f"DELETE FROM {self._chunk_table} WHERE source = ?", (source,))
            self._conn.commit()
            return len(ids)

    def list_sources(self) -> list[dict]:
        rows = self._conn.execute(
            f"""
            SELECT source, COUNT(*) AS n, SUM(LENGTH(text)) AS chars
            FROM {self._chunk_table}
            GROUP BY source ORDER BY source
            """
        ).fetchall()
        return [
            {"source": r["source"] or "(内联)", "chunks": r["n"], "chars": r["chars"] or 0}
            for r in rows
        ]

    def count(self) -> int:
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM {self._chunk_table}"
        ).fetchone()
        return int(row["n"])

    def reset(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"DELETE FROM {self._vec_table}")
            cur.execute(f"DELETE FROM {self._chunk_table}")
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ============================================================
# numpy 兜底后端
# ============================================================

class NumpyVectorStore(BaseVectorStore):
    """纯 numpy 暴力检索，数据落盘为 json + npy。"""

    backend = "numpy"

    def __init__(self, db_path: str | Path, dim: int):
        self.db_path = Path(db_path)
        if self.db_path.suffix:
            self.base = self.db_path.with_suffix("")
        else:
            self.base = self.db_path
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.base.with_suffix(".json")
        self.vec_path = self.base.with_suffix(".npy")
        self.dim = int(dim)
        self._lock = threading.RLock()

        self._meta: list[dict] = []
        self._vectors: np.ndarray = np.zeros((0, self.dim), dtype=np.float32)
        self._load()

    # ---------------- 持久化 ----------------

    def _load(self) -> None:
        if self.meta_path.exists():
            try:
                self._meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                self._meta = []
        if self.vec_path.exists():
            try:
                self._vectors = np.load(self.vec_path).astype(np.float32)
            except Exception:
                self._vectors = np.zeros((0, self.dim), dtype=np.float32)
        if self._vectors.shape[1:2] != (self.dim,):
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._meta = []

    def _flush(self) -> None:
        self.meta_path.write_text(
            json.dumps(self._meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        np.save(self.vec_path, self._vectors)

    # ---------------- 写入 ----------------

    def add(
        self,
        texts: Sequence[str],
        vectors: np.ndarray,
        source: str = "",
        metadatas: Optional[Sequence[dict]] = None,
    ) -> int:
        if len(texts) == 0:
            return 0
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape[1] != self.dim:
            raise ValueError(
                f"向量维度不匹配：期望 {self.dim}，实际 {vectors.shape[1]}"
            )
        metas = list(metadatas) if metadatas else [{} for _ in texts]
        normed = _normalize_rows(vectors)

        with self._lock:
            self._vectors = np.vstack([self._vectors, normed]) if len(self._meta) else normed
            for i, text in enumerate(texts):
                meta = dict(metas[i] or {})
                self._meta.append(
                    {
                        "text": text,
                        "source": source,
                        "chunk_index": int(meta.get("chunk_index", i)),
                        "metadata": meta,
                    }
                )
            self._flush()
        return len(texts)

    # ---------------- 检索 ----------------

    def search(
        self,
        vector: np.ndarray,
        k: int = 4,
        min_score: float = 0.0,
        sources: Optional[Sequence[str]] = None,
    ) -> list[Hit]:
        with self._lock:
            if not self._meta:
                return []
            q = _normalize(np.asarray(vector, dtype=np.float32).reshape(-1))
            if q.shape[0] != self._vectors.shape[1]:
                raise ValueError(
                    f"查询向量维度不匹配：期望 {self._vectors.shape[1]}，实际 {q.shape[0]}"
                )
            sims = self._vectors @ q
            order = np.argsort(-sims)

            hits: list[Hit] = []
            for idx in order:
                if len(hits) >= k:
                    break
                score = float(sims[idx])
                if score < min_score:
                    continue
                rec = self._meta[int(idx)]
                if sources and rec.get("source") not in sources:
                    continue
                hits.append(
                    Hit(
                        text=rec["text"],
                        score=score,
                        source=rec.get("source", ""),
                        chunk_index=int(rec.get("chunk_index", 0)),
                        metadata=rec.get("metadata", {}),
                    )
                )
            return hits

    # ---------------- 维护 ----------------

    def delete_source(self, source: str) -> int:
        with self._lock:
            keep = [i for i, r in enumerate(self._meta) if r.get("source") != source]
            removed = len(self._meta) - len(keep)
            if removed:
                self._meta = [self._meta[i] for i in keep]
                self._vectors = (
                    self._vectors[keep] if keep else np.zeros((0, self.dim), np.float32)
                )
                self._flush()
            return removed

    def list_sources(self) -> list[dict]:
        agg: dict[str, dict] = {}
        for r in self._meta:
            s = r.get("source") or "(内联)"
            slot = agg.setdefault(s, {"source": s, "chunks": 0, "chars": 0})
            slot["chunks"] += 1
            slot["chars"] += len(r.get("text", ""))
        return [agg[s] for s in sorted(agg)]

    def count(self) -> int:
        return len(self._meta)

    def reset(self) -> None:
        with self._lock:
            self._meta = []
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._flush()


# ============================================================
# 数值工具
# ============================================================

def _normalize(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _normalize_rows(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


# ============================================================
# 工厂
# ============================================================

def get_vector_store(
    db_path: Optional[str | Path] = None,
    dim: Optional[int] = None,
) -> BaseVectorStore:
    """按配置创建向量库后端。

    ``settings.VECTOR_BACKEND``:
        auto        -> 能用 sqlite-vec 就用，否则 numpy
        sqlite_vec  -> 强制（失败抛异常）
        numpy       -> 强制
    """
    db_path = Path(db_path or (settings.DATA_DIR / "knowledge.sqlite"))
    dim = int(dim or settings.EMBEDDING_DIM)
    backend = (settings.VECTOR_BACKEND or "auto").lower()

    if backend in ("auto", "sqlite_vec"):
        try:
            return SqliteVecStore(db_path, dim=dim)
        except Exception as e:
            if backend == "sqlite_vec":
                raise
            log.warning("sqlite-vec 不可用（%s），回退到 numpy 向量库", e)

    return NumpyVectorStore(db_path.with_suffix(".vec"), dim=dim)


def reset_vector_store(store: BaseVectorStore) -> None:
    """清空向量库内容。"""
    store.reset()


__all__ = [
    "BaseVectorStore",
    "SqliteVecStore",
    "NumpyVectorStore",
    "Hit",
    "get_vector_store",
    "reset_vector_store",
]
