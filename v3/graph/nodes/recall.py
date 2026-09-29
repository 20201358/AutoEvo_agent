"""recall 节点：RAG 检索 + 技能选择。

在规划之前先把「可能用得上的知识」和「可能适用的技能」捞出来，注入后续提示词。
这一步失败不影响主流程，只记 warning。
"""

from __future__ import annotations

import logging

from ..state import AgentState

log = logging.getLogger(__name__)


def _build_query(state: dict) -> str:
    """用用户意图 + 最近几条用户消息拼出检索 query。"""
    intent = (state.get("user_intent") or "").strip()
    if intent:
        return intent[:500]
    # 兜底：取最后一条人类消息
    for msg in reversed(state.get("messages") or []):
        if getattr(msg, "type", "") == "human":
            content = msg.content
            return (content if isinstance(content, str) else str(content))[:500]
    return ""


def recall_node(state: dict) -> dict:
    """检索知识库与技能库。"""
    from ...config import settings

    query = _build_query(state)
    updates: dict = {"retrieved_context": [], "active_skills": [], "loaded_skill_text": ""}

    if not query:
        return updates

    # ---------------- RAG ----------------
    if settings.RAG_ENABLED:
        try:
            from ...rag import get_knowledge_base

            kb = get_knowledge_base()
            if kb.count() > 0:
                hits = kb.search(query, k=settings.RAG_TOP_K)
                updates["retrieved_context"] = [h.to_dict() for h in hits]
                if hits:
                    log.info("recall: 检索到 %d 条知识", len(hits))
        except Exception as e:
            log.warning("recall: 知识库检索失败: %s", e)

    # ---------------- 技能 ----------------
    try:
        from ...skillkit import get_skill_library

        lib = get_skill_library()
        updates["skill_catalog"] = lib.render_catalog()

        matched = lib.search(query, limit=settings.SKILL_LOADER_TOP_N)
        names = [s.name for s in matched]

        # skill_manager 只在用户明确要管理技能时才自动载入（否则由 reflect 节点使用）
        if "skill_manager" in names and not _is_skill_management_request(query):
            names.remove("skill_manager")

        updates["active_skills"] = names
        if names:
            updates["loaded_skill_text"] = lib.render_full(names)
            log.info("recall: 载入技能 %s", names)
    except Exception as e:
        log.warning("recall: 技能检索失败: %s", e)

    return updates


_SKILL_KEYWORDS = (
    "技能", "skill", "保存流程", "记下来以后", "以后都这样",
    "沉淀", "复用", "创建技能", "更新技能", "删除技能", "整理技能",
)


def _is_skill_management_request(text: str) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in _SKILL_KEYWORDS)


__all__ = ["recall_node"]
