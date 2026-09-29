"""finish 节点：收尾（确定最终答复、标记计划状态、判定结束原因）。"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_core.messages import AIMessage, ToolMessage

log = logging.getLogger(__name__)


def _last_ai_text(messages) -> Optional[str]:
    for msg in reversed(messages or []):
        if isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                continue
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                text = " ".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict)
                ).strip()
                if text:
                    return text
    return None


def _last_tool_text(messages) -> Optional[str]:
    for msg in reversed(messages or []):
        if isinstance(msg, ToolMessage):
            content = msg.content
            return content if isinstance(content, str) else str(content)
    return None


def finish_node(state: dict) -> dict:
    """生成最终答复并落定任务状态。"""
    from ...config import settings

    messages = state.get("messages") or []
    iteration = int(state.get("iteration") or 0)

    # 结束原因
    end_reason = state.get("end_reason")
    if not end_reason:
        if state.get("is_done"):
            end_reason = "completed" if state.get("task_success", True) else "incomplete"
        elif iteration >= settings.MAX_ITERATIONS:
            end_reason = "max_iterations"
        else:
            end_reason = "completed"

    # 最终答复
    final_answer = (state.get("final_answer") or "").strip()
    if not final_answer:
        final_answer = _last_ai_text(messages) or ""
    if not final_answer:
        tail = _last_tool_text(messages)
        if tail:
            final_answer = (
                f"任务执行到第 {iteration} 轮结束，未得到总结性答复。最近一次工具输出：\n{tail[:2000]}"
            )

    if end_reason == "max_iterations" and not final_answer:
        final_answer = (
            f"已达到本轮最大迭代次数（{settings.MAX_ITERATIONS}），任务可能尚未完成。"
            "你可以继续让我接着做，或调整要求。"
        )

    # 计划状态收口
    plan = [dict(s) for s in (state.get("plan") or [])]
    if plan and end_reason == "completed":
        for step in plan:
            if step.get("status") in ("pending", "running"):
                step["status"] = "done"
    elif plan and end_reason == "max_iterations":
        for step in plan:
            if step.get("status") == "running":
                step["status"] = "pending"

    success = end_reason == "completed" and bool(state.get("task_success", True))

    log.info("finish: 结束原因=%s 迭代=%d", end_reason, iteration)

    updates: dict = {
        "is_done": True,
        "end_reason": end_reason,
        "task_success": success,
        "final_answer": final_answer,
        "plan": plan,
    }
    # 若模型没有输出任何面向用户的文本，补一条 AI 消息，保证会话记录完整
    if final_answer and not _last_ai_text(messages):
        updates["messages"] = [AIMessage(content=final_answer)]

    return updates


__all__ = ["finish_node"]
