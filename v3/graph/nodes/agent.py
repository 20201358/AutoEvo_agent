"""agent 节点：LLM 决策（调用工具 / 给出答复）。"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, SystemMessage

from ..context import trim_messages
from ..llm import bind_tools, get_llm
from ..prompts import build_system_prompt, render_plan
from ...tools import get_tools

log = logging.getLogger(__name__)


def _memory_text() -> str:
    try:
        from ...tools.task_tools import memory_file

        path = memory_file()
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    return ""


def _retrieved_text(state: dict) -> str:
    hits = state.get("retrieved_context") or []
    if not hits:
        return ""
    lines = []
    for i, h in enumerate(hits, 1):
        body = (h.get("text") or "").strip()
        if len(body) > 1200:
            body = body[:1200] + "…"
        lines.append(f"[{i}] 来源 {h.get('source') or '(内联)'} (相似度 {h.get('score', 0):.3f})\n{body}")
    return "\n\n".join(lines)


def _proposal_context(state: dict) -> str:
    """用户在回应技能建议时，把建议原文再强调一次。"""
    proposals = state.get("skill_proposals") or []
    if not proposals:
        return ""
    lines = ["你上一轮提出的技能管理建议（用户正在回应它）："]
    for i, p in enumerate(proposals, 1):
        lines.append(
            f"{i}. {p.get('action', '?')} -> {', '.join(p.get('skills') or [])}"
            f"  原因: {p.get('reason', '')}"
        )
    lines.append(
        "请根据用户的回应执行对应的技能工具调用；"
        "如果用户说执行，就用 create_skill / update_skill / delete_skill 落实；"
        "如果用户拒绝，就简单确认并跳过，不要重复提议。"
    )
    return "\n".join(lines)


def agent_node(state: dict) -> dict:
    """调用 LLM 决定下一步动作。"""
    from ...config import settings

    iteration = int(state.get("iteration") or 0) + 1

    # 环境与 shell 类型
    environment = state.get("environment") or {}
    shell_kind = "unknown"
    try:
        from ...platform_utils import get_persistent_session, shell_session_status

        st = shell_session_status()
        if st.get("active"):
            shell_kind = st.get("kind") or "unknown"
        else:
            from ...platform_utils.shell import default_shell
            from ...platform_utils.persistent_shell import _shell_kind

            shell_kind = _shell_kind(default_shell())
    except Exception:
        pass

    plan_text = render_plan(state.get("plan"), state.get("current_step_index") or 0)

    memory_text = _memory_text()
    retrieved_text = _retrieved_text(state)
    proposal_text = _proposal_context(state)

    if proposal_text:
        loaded = proposal_text + ("\n\n" + state.get("loaded_skill_text", "") if state.get("loaded_skill_text") else "")
    else:
        loaded = state.get("loaded_skill_text") or ""

    system_prompt = build_system_prompt(
        environment=environment,
        shell_kind=shell_kind,
        plan_text=plan_text,
        task_mode=state.get("task_mode") or "new_task",
        user_intent=state.get("user_intent") or "",
        skill_catalog=state.get("skill_catalog") or "",
        loaded_skills=loaded,
        retrieved_context=retrieved_text,
        memory_text=memory_text,
        with_tools=True,
    )

    history = trim_messages(state.get("messages") or [])
    summary = (state.get("context_summary") or "").strip()
    if summary:
        system_prompt = (
            system_prompt
            + "\n\n## 更早对话的摘要（长程上下文）\n"
            + summary
        )

    llm = bind_tools(get_llm(), get_tools())

    log.info("agent: 第 %d 轮，历史 %d 条消息", iteration, len(history))

    try:
        resp = llm.invoke([SystemMessage(content=system_prompt)] + history)
    except Exception as e:
        log.exception("agent: LLM 调用失败")
        resp = AIMessage(
            content=(
                "抱歉，调用模型时出错："
                f"{type(e).__name__}: {e}\n"
                "请检查网络与 OPENAI_BASE_URL / OPENAI_API_KEY 配置。"
            )
        )
        return {
            "messages": [resp],
            "iteration": iteration,
            "end_reason": "error",
            "is_done": True,
        }

    if not isinstance(resp, AIMessage):  # 保险
        resp = AIMessage(content=str(getattr(resp, "content", resp)))

    return {"messages": [resp], "iteration": iteration}


__all__ = ["agent_node"]
