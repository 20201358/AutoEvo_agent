"""clarify 节点：信息不足时向用户提问。

两种触发来源：

    1. plan_node 在 plan 里附带 ``clarify_questions``（规划信息不足）
       → 用户回答作为 ``plan_feedback`` 回 plan 节点重新规划。
    2. plan_confirm 无法理解用户的自由语言回复，追问
       「执行当前计划 / 修改计划 / 取消任务」
       → 用户答"执行"类 → 直接确认当前计划（不再重复确认框）
       → 用户答"取消"类 → 结束任务
       → 其它 → 作为调整意见回 plan 重新思考。
"""

from __future__ import annotations

import logging
from typing import Optional

from langgraph.types import interrupt

from .plan_confirm import _is_no, _is_yes

log = logging.getLogger(__name__)


def clarify_node(state: dict) -> dict:
    """把规划器抛出的澄清问题抛给用户。"""
    questions = state.get("clarify_questions") or []
    if not questions:
        # 没有问题要问 → 视为已澄清，直接空操作
        return {"clarify_questions": []}

    # 每次只问一个问题（避免一次性太多中断用户体验），但把后续的挂在 context 里
    head = questions[0]
    rest = questions[1:]

    payload = {
        "kind": "clarify",
        "questions": [head],
        "context": {
            "answered_count": len(state.get("clarification_answers") or []),
            "remaining": len(questions),
        },
    }

    log.info(
        "clarify: 第 %d/%d 个问题: %s",
        payload["context"]["answered_count"] + 1,
        payload["context"]["remaining"],
        head.get("question", "")[:80],
    )

    user_answer = interrupt(payload)

    # 收集答案
    if isinstance(user_answer, dict):
        answer_text = (
            user_answer.get("answer")
            or user_answer.get("value")
            or user_answer.get("text")
            or ""
        )
    else:
        answer_text = str(user_answer or "").strip()

    # 把回答拼到澄清回答序列
    new_answers = list(state.get("clarification_answers") or []) + [answer_text]

    plan = state.get("plan") or []

    # ---- plan_confirm 的「待明确」追问：直接按用户选择处理 ----
    if plan and _is_yes(answer_text):
        # 用户明确选择执行 → 直接确认当前计划，跳过重复确认框
        return {
            "clarify_questions": [],
            "clarification_answers": new_answers,
            "plan_feedback": "",
            "plan_confirmed_revision": state.get("plan_revision"),
            "pending_confirmation": None,
        }
    if plan and _is_no(answer_text):
        log.info("clarify: 用户在追问中选择取消任务")
        return {
            "clarify_questions": [],
            "clarification_answers": new_answers,
            "plan_feedback": "",
            "is_done": True,
            "task_success": False,
            "end_reason": "user_cancelled",
            "final_answer": "已按你的要求取消任务。",
            "pending_confirmation": None,
        }

    # 还没问完 → 留下一个继续问
    if rest:
        remaining_questions = [head] + rest  # 当前回答过的问题移到最后，避免下次重复问
        # 实际策略：把 rest 留下，下轮 plan/clarify 时再问
        return {
            "clarification_answers": [answer_text],
            "clarify_questions": rest,
            "plan_feedback": (
                (state.get("plan_feedback") or "")
                + f"\n[澄清#{len(new_answers)}] {head.get('question', '')} → {answer_text}"
            ).strip(),
            "pending_confirmation": None,  # 显式清掉
        }

    # 全部答完 → 回答并入 plan_feedback，让 plan 节点据此重规划
    feedback_lines = []
    for q, a in zip(
        state.get("clarify_questions") or [], [answer_text]  # 当前答的放在最后一条
    ):
        feedback_lines.append(f"[澄清] {q.get('question', '')} → {a}")
    new_feedback = (state.get("plan_feedback") or "").strip()
    if new_feedback:
        new_feedback += "\n" + "\n".join(feedback_lines)
    else:
        new_feedback = "\n".join(feedback_lines)

    return {
        "clarification_answers": [answer_text],
        "clarify_questions": [],  # 全部清空
        "plan_feedback": new_feedback,
        "pending_confirmation": None,
    }


def has_clarify(state: dict) -> bool:
    """路由判定：plan 节点产生 clarify_questions 时路由到本节点。"""
    return bool(state.get("clarify_questions"))


__all__ = ["clarify_node", "has_clarify"]