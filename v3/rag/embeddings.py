"""可插拔文本嵌入。

两种实现：

- ``OpenAIEmbedder``     调用 OpenAI 兼容的 ``/embeddings`` 接口（智谱 / OpenAI / 本地
                         兼容服务均可）。需要网络与 API Key。
- ``LocalHashingEmbedder`` 纯本地、确定性的字符 n-gram 哈希嵌入。零依赖、零网络，
                         用于离线场景或远端接口不可用时兜底。

``get_embedder()`` 按 ``settings.EMBEDDING_PROVIDER`` 选择：
    auto   -> 先探测远端，失败则回退本地
    openai -> 强制远端（失败抛异常）
    local  -> 强制本地
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from typing import Optional, Sequence

import numpy as np

from ..config import settings

log = logging.getLogger(__name__)


# ============================================================
# 抽象接口
# ============================================================

class BaseEmbedder:
    """嵌入器接口。"""

    name: str = "base"
    dim: int = 0

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name} dim={self.dim}>"


# ============================================================
# 远端嵌入（OpenAI 兼容接口）
# ============================================================

class OpenAIEmbedder(BaseEmbedder):
    """调用 OpenAI 兼容的 /embeddings 接口。"""

    name = "openai"

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        batch_size: int = 32,
    ):
        self.model = model or settings.EMBEDDING_MODEL
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.base_url = base_url or settings.EMBEDDING_BASE_URL
        self.timeout = timeout
        self.batch_size = batch_size
        self._client = None
        self.dim = 0

    # ---------------- 客户端懒加载 ----------------

    @property
    def client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("未安装 openai 包，无法使用远端嵌入") from e
            if not self.api_key:
                raise RuntimeError("缺少 EMBEDDING_API_KEY / OPENAI_API_KEY")
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    # ---------------- 探测 ----------------

    def probe(self) -> bool:
        """用一条短文本探测接口是否可用，同时确定向量维度。"""
        try:
            vec = self._call(["ping"])[0]
            self.dim = int(vec.shape[0])
            return self.dim > 0
        except Exception as e:
            log.debug("远端嵌入探测失败: %s", e)
            return False

    # ---------------- 实现 ----------------

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim or settings.EMBEDDING_DIM), dtype=np.float32)

        out: list[np.ndarray] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [t if t and t.strip() else " " for t in texts[i : i + self.batch_size]]
            out.extend(self._call(batch))

        mat = np.vstack(out).astype(np.float32)
        if self.dim == 0:
            self.dim = int(mat.shape[1])
        return mat

    def _call(self, batch: Sequence[str]) -> list[np.ndarray]:
        resp = self.client.embeddings.create(model=self.model, input=list(batch))
        data = sorted(resp.data, key=lambda d: d.index)
        return [
            np.asarray(d.embedding, dtype=np.float32)
            for d in data
        ]


# ============================================================
# 本地兜底嵌入
# ============================================================

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


class LocalHashingEmbedder(BaseEmbedder):
    """确定性字符/词 n-gram 哈希嵌入。

    思路：把文本切成 token（英文单词、单个汉字），再生成 1~2 元组；
    每个 n-gram 用 blake2b 哈希到 ``dim`` 维向量的一个桶里，
    带符号累加后做 L2 归一化。余弦相似度即近似词面重叠度。

    它不是语义嵌入，但零依赖、可复现、对中英混排都有稳定的词面召回，
    适合作为离线兜底。
    """

    name = "local-hash"

    def __init__(self, dim: int = 1024):
        self.dim = max(64, int(dim))

    # ---------------- token 化 ----------------

    def _tokens(self, text: str) -> list[str]:
        tokens = _TOKEN_RE.findall(text.lower())
        grams: list[str] = list(tokens)
        for a, b in zip(tokens, tokens[1:]):
            grams.append(a + "\u0001" + b)
        return grams

    def _bucket(self, token: str) -> tuple[int, float]:
        h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(h, "little")
        idx = raw % self.dim
        sign = 1.0 if (raw >> 63) & 1 else -1.0
        return idx, sign

    # ---------------- 实现 ----------------

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        mat = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in self._tokens(text or ""):
                idx, sign = self._bucket(token)
                mat[row, idx] += sign
            norm = float(np.linalg.norm(mat[row]))
            if norm > 0:
                mat[row] /= norm
            else:
                # 空文本给一个稳定但无信息的方向，避免全零向量
                mat[row, 0] = 1.0
        return mat

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


# ============================================================
# 工厂
# ============================================================

_embedder_lock = threading.Lock()
_embedder: Optional[BaseEmbedder] = None


def get_embedder(force_new: bool = False) -> BaseEmbedder:
    """按配置返回全局嵌入器（带缓存）。"""
    global _embedder

    with _embedder_lock:
        if _embedder is not None and not force_new:
            return _embedder

        provider = (settings.EMBEDDING_PROVIDER or "auto").lower()

        if provider == "local":
            _embedder = LocalHashingEmbedder(dim=settings.EMBEDDING_DIM)
            log.info("使用本地哈希嵌入 (dim=%d)", _embedder.dim)
            return _embedder

        if provider in ("openai", "auto"):
            remote = OpenAIEmbedder()
            ok = remote.probe()
            if ok:
                log.info(
                    "使用远端嵌入 model=%s dim=%d", remote.model, remote.dim
                )
                _embedder = remote
                return _embedder
            if provider == "openai":
                raise RuntimeError(
                    f"远端嵌入不可用（model={remote.model}, base_url={remote.base_url}）"
                )
            log.warning("远端嵌入不可用，回退到本地哈希嵌入")

        _embedder = LocalHashingEmbedder(dim=settings.EMBEDDING_DIM)
        return _embedder


def reset_embedder() -> None:
    """清空缓存（切换配置后调用）。"""
    global _embedder
    with _embedder_lock:
        _embedder = None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """两个向量的余弦相似度（已归一化时等价于点积）。"""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


__all__ = [
    "BaseEmbedder",
    "OpenAIEmbedder",
    "LocalHashingEmbedder",
    "get_embedder",
    "reset_embedder",
    "cosine_similarity",
]
