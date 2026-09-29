"""知识库（RAG）工具。

让 agent 能把资料写进向量库、并做语义检索。
"""

from __future__ import annotations

from pathlib import Path

from ..config import settings
from ..rag import get_knowledge_base
from ._base import register


@register(category="knowledge")
def kb_search(query: str, k: int = 4) -> str:
    """在本地知识库中做语义检索，返回最相关的若干文本块。

    当你不确定某个命令的用法、项目约定、历史决策时，先用它查一下再动手。

    Args:
        query: 检索问题或关键词。
        k: 返回条数。
    """
    if not settings.RAG_ENABLED:
        return "提示：当前配置已关闭 RAG（RAG_ENABLED=0）"
    kb = get_knowledge_base()
    if kb.count() == 0:
        return "(知识库为空，可先用 kb_add 写入资料)"
    hits = kb.search(query, k=int(k or settings.RAG_TOP_K))
    if not hits:
        return f"未检索到与 {query!r} 相关的内容"
    return kb.store.format_hits(hits, max_chars=settings.RAG_MAX_CHARS_PER_HIT)


@register(category="knowledge", requires_confirm=True, risk="low")
def kb_add(text: str = "", path: str = "", source: str = "") -> str:
    """把一段文本或一个文件/目录写入知识库。

    两种用法：
      - 传 text：直接写入这段文本
      - 传 path：把该文件（或目录下的文本文件）切块后写入

    Args:
        text: 要写入的文本内容。
        path: 要写入的文件或目录路径。
        source: 来源标识（用于后续检索展示与去重删除）。不传时用 path 或 "inline"。
    """
    if not text and not path:
        return "错误：text 与 path 至少要提供一个"

    kb = get_knowledge_base()

    if path:
        p = Path(path).expanduser()
        if not p.exists():
            return f"错误：路径不存在 -> {p}"
        if p.is_dir():
            res = kb.add_directory(p)
            skipped = res.get("skipped") or []
            msg = (
                f"已从目录 {p} 写入 {res['files']} 个文件、{res['chunks']} 个文本块"
            )
            if skipped:
                msg += f"（跳过 {len(skipped)} 项，如 {skipped[0].get('reason')}）"
            return msg
        res = kb.add_file(p)
        if res.get("skipped"):
            return f"错误：{res['skipped']} -> {p}"
        return f"已从文件写入 {res['chunks']} 个文本块 ({res['chars']} 字符) -> {p}"

    n = kb.add_text(text, source=source or "inline")
    return f"已写入 {n} 个文本块 -> source={source or 'inline'}"


@register(category="knowledge")
def kb_list() -> str:
    """列出知识库中已收录的来源。"""
    kb = get_knowledge_base()
    sources = kb.list_sources()
    stats = kb.stats()
    if not sources:
        return f"(知识库为空) 后端={stats['backend']} 嵌入={stats['embedder']}({stats['dim']}维)"
    lines = [
        f"知识库：{stats['chunks']} 个文本块 / {len(sources)} 个来源",
        f"后端={stats['backend']} 嵌入={stats['embedder']}({stats['dim']}维)",
        "",
    ]
    for s in sources:
        lines.append(f"- {s['source']}  ({s['chunks']} 块, {s['chars']} 字符)")
    return "\n".join(lines)


@register(category="knowledge", requires_confirm=True, risk="low")
def kb_delete(source: str) -> str:
    """从知识库中删除某个来源的全部文本块。

    Args:
        source: 来源标识（用 kb_list 查看）。
    """
    kb = get_knowledge_base()
    n = kb.remove_source(source)
    if n == 0:
        return f"错误：知识库中没有来源 {source!r}（用 kb_list 查看可用来源）"
    return f"已删除 {n} 个文本块 -> {source}"


@register(category="knowledge")
def kb_stats() -> str:
    """查看知识库统计信息。"""
    kb = get_knowledge_base()
    st = kb.stats()
    return (
        f"文本块: {st['chunks']}\n"
        f"来源数: {st['sources']}\n"
        f"嵌入器: {st['embedder']} ({st['dim']} 维)\n"
        f"向量后端: {st['backend']}"
    )


def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


KB_TOOLS = [
    "kb_search",
    "kb_add",
    "kb_list",
    "kb_delete",
    "kb_stats",
]

__all__ = [
    "kb_search",
    "kb_add",
    "kb_list",
    "kb_delete",
    "kb_stats",
    "KB_TOOLS",
]