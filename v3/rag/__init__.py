"""RAG：嵌入、向量库、分块摄入。

对外主要入口：
    from v3.rag import get_knowledge_base
    kb = get_knowledge_base()
    kb.add_text("...", source="notes.md")
    hits = kb.search("怎么配置代理", k=4)
"""

from .embeddings import (
    BaseEmbedder,
    LocalHashingEmbedder,
    OpenAIEmbedder,
    get_embedder,
    reset_embedder,
)
from .indexer import chunk_text, ingest_directory, ingest_file, ingest_text
from .knowledge_base import KnowledgeBase, get_knowledge_base, reset_knowledge_base
from .vector_store import (
    BaseVectorStore,
    Hit,
    NumpyVectorStore,
    SqliteVecStore,
    get_vector_store,
    reset_vector_store,
)

__all__ = [
    # embeddings
    "BaseEmbedder",
    "OpenAIEmbedder",
    "LocalHashingEmbedder",
    "get_embedder",
    "reset_embedder",
    # vector store
    "BaseVectorStore",
    "SqliteVecStore",
    "NumpyVectorStore",
    "Hit",
    "get_vector_store",
    "reset_vector_store",
    # indexer
    "chunk_text",
    "ingest_text",
    "ingest_file",
    "ingest_directory",
    # facade
    "KnowledgeBase",
    "get_knowledge_base",
    "reset_knowledge_base",
]
