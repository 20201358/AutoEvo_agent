"""intake 节点：回合入口的记账与路由。

确定性逻辑（不调用 LLM），负责：
  - 判定本轮性质（全新任务 / 追加需求 / 回应技能建议 / 后续对话）
  - 生成 task_id、提取 user_intent
  - 按轮次性质重置任务级状态
"""

from __future__ import annotations

import logging
import uuid

from langchain_core.messages import AIMessage, HumanMessage

from ..prompts import utc_now_text
from ..reducers import reset_dict
from ..state import make_task_reset

log = logging.getLogger(__name__)


def _last_human_text(messages) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, HumanMessage):
            content = msg.content
            return content if isinstance(content, str) else str(content)
    return ""


def _last_ai_text(messages) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content
    return ""


_PROPOSAL_ACCEPT = (
    "执行", "全部执行", "同意", "采纳", "可以", "好的", "好呀", "行", "apply",
    "do it", "ok", "yes", "确认",
)
_PROPOSAL_DECLINE = (
    "跳过", "不用", "不需要", "不要", "算了", "拒绝", "取消", "skip", "no",
)
_PROPOSAL_REFER = ("建议", "第", "条", "技能", "skill", "详情")

# 取消任务的明确信号（短句才算，避免误伤长需求里的"取消某个订阅"之类）
_CANCEL_WORDS = (
    "取消任务", "取消吧", "停止任务", "别做了", "不做了", "算了不做了",
    "放弃任务", "取消这个任务", "stop the task", "cancel the task",
)
# 换目标的信号："不做X了，改做Y" / "换一个目标" / "改做..."
_REDIRECT_PATTERNS = (
    "改做", "换成", "换一个", "不做这个了", "不做这个啦",
    "换个目标", "更换目标", "新的目标是", "改为", "instead",
)


def looks_like_proposal_response(text: str) -> bool:
    """判断用户这句话是否在回应技能管理建议。

    只有「短句 + 明确的接受/拒绝/指代」才算；否则视为新的任务，
    避免用户忽略建议直接派活时被误判。
    """
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()

    if any(w in low for w in _PROPOSAL_ACCEPT) or any(w in low for w in _PROPOSAL_DECLINE):
        # 仅当句子足够短，或显式提到"建议/第 N 条"时才认定
        if len(t) <= 30 or any(w in t for w in _PROPOSAL_REFER):
            return True

    if any(w in t for w in ("建议", "详情")) and len(t) <= 40:
        return True

    return False


def _looks_like_cancel(text: str) -> bool:
    """明确取消当前任务。"""
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > 20:
        return False
    return any(w in t for w in _CANCEL_WORDS)


def _looks_like_redirect(text: str) -> bool:
    """用户换了目标（而不是在原任务上调整）。"""
    t = (text or "").strip()
    if not t:
        return False
    return any(p in t for p in _REDIRECT_PATTERNS)


def classify(messages, state: dict) -> str:
    """判断本轮性质。

    可能的值：
        new_task          全新任务
        adjust            在既有任务上追加/修改需求（保留已完成步骤）
        redirect          用户换目标 → 按新任务处理
        cancel            取消当前任务
        proposal_response 用户正在回应技能管理建议
        followup          任务已结束后的后续对话
    """
    user_text = _last_human_text(messages)

    if state.get("awaiting_proposal_response") and looks_like_proposal_response(user_text):
        return "proposal_response"

    if not state.get("task_id"):
        return "new_task"
    if state.get("is_done"):
        return "followup"

    # 有活跃任务：判断是调整 / 换目标 / 取消
    if _looks_like_cancel(user_text):
        return "cancel"
    if _looks_like_redirect(user_text):
        return "redirect"
    if state.get("plan"):
        return "adjust"
    return "new_task"


def intake_node(state: dict) -> dict:
    """回合入口节点。"""
    messages = state.get("messages") or []
    user_intent = _last_human_text(messages)
    mode = classify(messages, state)

    updates: dict = {
        "task_mode": mode,
        "user_intent": user_intent,
        "awaiting_proposal_response": False,
    }
    # 用户没有回应技能建议而是直接派活时，清掉上一轮的建议，避免上下文串味
    if mode != "proposal_response":
        updates["skill_proposals"] = []

    if mode == "new_task" or mode == "redirect":
        # redirect：用户换了目标，按全新任务处理（重置计划与状态）
        updates.update(make_task_reset())
        updates["task_mode"] = "new_task" if mode == "redirect" else "new_task"
        updates["user_intent"] = user_intent
        updates["task_id"] = f"task-{uuid.uuid4().hex[:8]}"
        # 重置累计字典，避免状态无限膨胀
        updates["resolved_confirmations"] = reset_dict()
        updates["user_answers"] = reset_dict()
        if mode == "redirect":
            log.info("intake: 用户更换目标 %s | %s", updates["task_id"], user_intent[:80])
        else:
            log.info("intake: 新任务 %s | %s", updates["task_id"], user_intent[:80])

    elif mode == "cancel":
        # 用户明确取消当前任务
        updates.update(
            {
                "iteration": 0,
                "is_done": True,
                "task_success": False,
                "final_answer": "已按你的要求取消当前任务。",
                "end_reason": "user_cancelled",
                "pending_confirmation": None,
                "plan_note": "用户取消",
            }
        )
        log.info("intake: 用户取消任务 %s", state.get("task_id"))

    elif mode == "adjust":
        # 保留计划与已完成状态，只重启执行循环
        updates.update(
            {
                "iteration": 0,
                "is_done": False,
                "final_answer": None,
                "end_reason": None,
                "pending_confirmation": None,
                "task_success": False,
            }
        )
        log.info("intake: 调整任务 %s | %s", state.get("task_id"), user_intent[:80])

    elif mode == "proposal_response":
        updates.update(
            {
                "iteration": 0,
                "is_done": False,
                "final_answer": None,
                "end_reason": None,
                "pending_confirmation": None,
            }
        )
        log.info("intake: 回应技能建议 | %s", user_intent[:80])

    else:  # followup
        updates.update(make_task_reset())
        updates["task_mode"] = "followup"
        updates["user_intent"] = user_intent
        updates["task_id"] = f"task-{uuid.uuid4().hex[:8]}"
        log.info("intake: 后续对话 %s | %s", updates["task_id"], user_intent[:80])

    updates["turn_started_at"] = utc_now_text()
    return updates


__all__ = ["intake_node", "classify"]
