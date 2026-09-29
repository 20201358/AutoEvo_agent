"""reflect 节点：任务结束后的技能反思（自进化的核心）。

任务**完成**或**未完成但已结束**（达到迭代上限 / 用户中止 / 出错）时都会进入这里，
判断是否需要新增、更新、合并或清理技能，并把建议呈现给用户。

设计要点：
    - 只提议，不擅自改动技能库（改动必须由用户确认后经技能工具执行）
    - 用 ``reflected_history_len`` 去重，同一批动作不重复反思
    - 区分「用户正在回应上一轮建议」的场景，避免自我循环
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..llm import get_planner_llm
from ..prompts import REFLECT_SYSTEM, build_reflect_user, render_execution_digest, render_plan

log = logging.getLogger(__name__)

NO_PROPOSAL_TOKEN = "NO_PROPOSAL"

_DECLINE_WORDS = (
    "跳过", "不用", "不需要", "不要", "算了", "否", "拒绝", "skip", "no", "取消",
)


# ============================================================
# 辅助
# ============================================================

def _last_human_text(messages) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, HumanMessage):
            content = msg.content
            return content if isinstance(content, str) else str(content)
    return ""


def _task_summary(state: dict) -> str:
    plan = state.get("plan") or []
    lines = [
        f"- 用户目标: {state.get('user_intent') or '(未知)'}",
        f"- 结束原因: {state.get('end_reason') or '(未知)'}",
        f"- 任务是否达成: {'是' if state.get('task_success') else '否'}",
        f"- 执行轮数: {state.get('iteration') or 0}",
        f"- 计划修订次数: {state.get('plan_revision') or 0}",
        f"- 本回合载入的技能: {', '.join(state.get('active_skills') or []) or '(无)'}",
    ]
    if plan:
        lines.append("- 计划与完成情况:\n" + render_plan(plan, state.get("current_step_index") or 0))
    answer = (state.get("final_answer") or "").strip()
    if answer:
        lines.append(f"- 最终答复: {answer[:1200]}")
    errors = state.get("error_state") or {}
    if errors.get("last_error"):
        lines.append(f"- 最后的错误: {errors['last_error']}")
    if errors.get("error_count"):
        lines.append(f"- 本回合错误次数: {errors['error_count']}")
    return "\n".join(lines)


def _skill_manager_body() -> str:
    """读取 skill_manager 技能的正文。"""
    try:
        from ...skillkit import get_skill_library

        skill = get_skill_library().load("skill_manager")
        if skill:
            return skill.body
    except Exception as e:
        log.debug("读取 skill_manager 失败: %s", e)
    return "(未找到 skill_manager 技能，按通用原则判断即可)"


_BLOCK_RE = re.compile(r"^\s*(\d+)[.、)]\s*", re.MULTILINE)
_FIELD_RE = {
    "action": re.compile(r"建议动作[：:]\s*(.+)"),
    "skills": re.compile(r"涉及技能[：:]\s*(.+)"),
    "keep": re.compile(r"保留技能[：:]\s*(.+)"),
    "archive": re.compile(r"归档技能[：:]\s*(.+)"),
    "reason": re.compile(r"原因[：:]\s*(.+)"),
    "change": re.compile(r"具体改动[：:]\s*(.+)"),
}


def parse_proposals(text: str) -> list[dict]:
    """从反思输出里解析出结构化的建议列表。"""
    if not text or NO_PROPOSAL_TOKEN in text:
        return []

    # 按 "1. 建议动作：" 切块
    starts = [m.start() for m in _BLOCK_RE.finditer(text)]
    blocks: list[str] = []
    if starts:
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else len(text)
            blocks.append(text[s:e])
    else:
        blocks = [text]

    proposals: list[dict] = []
    for i, block in enumerate(blocks, 1):
        action = None
        for m in _FIELD_RE["action"].finditer(block):
            action = m.group(1).strip()
            break
        if not action and "建议动作" not in block:
            continue

        item: dict = {"index": i, "raw": block.strip()}
        for key, rx in _FIELD_RE.items():
            m = rx.search(block)
            if m:
                item[key] = m.group(1).strip()
        # 规范化技能名列表
        for key in ("skills", "archive"):
            if item.get(key):
                item[key] = [
                    s.strip().strip("`'\"*")
                    for s in re.split(r"[,，、/]| and ", item[key])
                    if s.strip()
                ]
        proposals.append(item)

    return proposals[:5]


def _extract_declines(user_text: str, proposals: list[dict]) -> list[str]:
    """判断用户是否拒绝了建议，返回被拒绝项的标识。"""
    low = (user_text or "").lower()
    if not any(w in low for w in _DECLINE_WORDS):
        return []
    out: list[str] = []
    for p in proposals:
        skills = p.get("skills") or []
        label = f"{p.get('action', '?')}:{','.join(skills) or 'unknown'}"
        out.append(label)
    return out


def _has_pruned_proposal_reply(user_text: str, proposals: list[dict]) -> bool:
    """用户明确说执行某几条。"""
    low = (user_text or "").lower()
    return any(w in low for w in ("执行", "同意", "采纳", "可以", "好的", "apply", "ok", "do it"))


# ============================================================
# 主节点
# ============================================================

def reflect_node(state: dict) -> dict:
    """任务结束后的反思。"""
    from ...config import settings

    history = state.get("execution_history") or []
    base_updates: dict = {"reflected_history_len": len(history)}
    mode = state.get("task_mode")

    # ---------------- 用户正在回应建议：只记录，不再提议 ----------------
    if mode == "proposal_response":
        proposals = state.get("skill_proposals") or []
        user_text = _last_human_text(state.get("messages") or [])
        declined = _extract_declines(user_text, proposals)
        updates = dict(base_updates)
        updates.update(
            {
                "skill_proposals": [],
                "awaiting_proposal_response": False,
                "reflections": [
                    {
                        "task_id": state.get("task_id"),
                        "kind": "proposal_response",
                        "user_text": user_text[:400],
                        "declined": declined,
                    }
                ],
            }
        )
        if declined:
            updates["declined_proposals"] = declined
            log.info("reflect: 用户拒绝 %s", declined)
        else:
            log.info("reflect: 用户回应技能建议，视为已处理")
        return updates

    # ---------------- 常规反思 ----------------
    if not settings.SKILL_AUTO_REFLECT:
        return base_updates

    if len(history) < settings.SKILL_REFLECT_MIN_TOOL_CALLS:
        log.info("reflect: 本回合工具调用过少，跳过反思")
        return dict(base_updates, skill_proposals=[], awaiting_proposal_response=False)

    if state.get("reflected_history_len") is not None and len(history) <= int(
        state.get("reflected_history_len") or 0
    ):
        log.info("reflect: 无新增动作，跳过反思")
        return base_updates

    # 跳过纯闲聊式回合（无工具调用已在上面过滤；这里再挡一次空计划+无目标）
    if not (state.get("user_intent") or "").strip():
        return base_updates

    try:
        from ...skillkit import get_skill_library

        catalog = get_skill_library().render_catalog()
    except Exception:
        catalog = ""

    user_prompt = build_reflect_user(
        skill_manager_skill=_skill_manager_body(),
        task_summary=_task_summary(state),
        execution_digest=render_execution_digest(history),
        skill_catalog=catalog,
        declined=state.get("declined_proposals") or None,
    )

    proposal_text = ""
    try:
        llm = get_planner_llm()
        resp = llm.invoke(
            [SystemMessage(content=REFLECT_SYSTEM), HumanMessage(content=user_prompt)]
        )
        proposal_text = resp.content if isinstance(resp.content, str) else str(resp.content)
        proposal_text = (proposal_text or "").strip()
    except Exception as e:
        log.warning("reflect: 反思调用失败: %s", e)
        return base_updates

    # 无建议
    if not proposal_text or proposal_text.upper().startswith(NO_PROPOSAL_TOKEN) or (
        NO_PROPOSAL_TOKEN in proposal_text and len(proposal_text) < 80
    ):
        log.info("reflect: 判断为无需管理技能")
        return dict(
            base_updates,
            skill_proposals=[],
            awaiting_proposal_response=False,
            reflections=[
                {
                    "task_id": state.get("task_id"),
                    "kind": "no_proposal",
                    "end_reason": state.get("end_reason"),
                    "tool_calls": len(history),
                }
            ],
        )

    proposals = parse_proposals(proposal_text)

    banner = (
        "【技能管理建议】\n"
        if "【技能管理建议】" not in proposal_text
        else ""
    )

    log.info("reflect: 产出 %d 条技能建议", len(proposals))

    return dict(
        base_updates,
        messages=[AIMessage(content=(banner + proposal_text).strip(), additional_kwargs={"reflection": True})],
        skill_proposals=proposals,
        awaiting_proposal_response=True,
        reflections=[
            {
                "task_id": state.get("task_id"),
                "kind": "proposal",
                "end_reason": state.get("end_reason"),
                "tool_calls": len(history),
                "proposals": proposals,
            }
        ],
    )


__all__ = ["reflect_node", "parse_proposals", "NO_PROPOSAL_TOKEN"]
