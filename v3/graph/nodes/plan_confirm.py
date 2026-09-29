"""plan_confirm 节点：计划生成后先给用户审核。

核心契约（来自用户需求）：

    「计划输出后等待用户输入，用户可以输入各种话语（比如可以执行、没问题、
     算了吧、换一个更安全一点的策略），由 agent 根据这些输入智能判断
     是继续执行、修改计划，还是取消任务。
     如果 agent 无法理解，则让 agent 输出他理解到的意思，
     让用户明确表达，再根据用户的输入去重新思考。」

实现方式：

    - ``plan`` 节点产出新计划后，先到这里 interrupt 暂停
    - 用户回复自由语言，按三层判定：
        1. 明确同意（"好/执行/没问题/ok"）→ 标记已确认，回 agent 执行
        2. 明确取消（"算了/别做了/取消"）→ finish（end_reason=user_cancelled）
        3. 其它自由语言 → 交给 LLM 分类：
           * execute    → 同意执行
           * cancel     → 取消任务
           * adjust     → 视为调整意见，回 plan 节点重新规划（带上用户原话）
           * new_task   → 用户输入与当前任务无关，要切换到新任务
                          （end_reason=task_switched + _switch_to_task，
                           由 CLI 开新会话处理）
           * unclear    → agent 输出自己的理解，转 clarify 节点请用户明确表达，
                          用户答完后再回 plan 重新思考
    - 通过 ``plan_confirmed_revision`` 状态字段防止重复确认同一版本计划
    - 简单任务（plan 只有 1 步且无高风险）自动跳过确认，避免打断体验
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import interrupt

from ..prompts import render_plan

log = logging.getLogger(__name__)


_YES = {
    "y", "yes", "ok", "okay", "好", "好的", "可以", "行", "同意", "执行",
    "继续", "go", "approved", "确认", "准了", "就这么办", "开始",
    "没问题", "没毛病", "没问题吧", "开始吧", "执行吧", "就这么干", "走起",
    "可以啊", "行吧", "妥", "run", "start",
}
_NO = {
    "n", "no", "nope", "不", "不要", "算了", "算了吧", "取消", "跳过", "cancel",
    "stop", "放弃", "不做了", "停止", "别做了", "不搞了", "别搞了", "不了",
    "不要了", "拉倒",
}
# 包含即视为取消的子串（"算了吧" / "还是算了" 等）
_NO_CONTAINS = ("算了", "别做了", "不搞了", "别搞了", "不做了", "放弃", "拉倒")
# 包含即视为同意的子串
_YES_CONTAINS = ("没问题", "没毛病", "开始吧", "执行吧", "就这么办", "就这么干", "可以执行", "同意执行")
# 包含即视为"切到新任务"的子串（关键词快通道，避免每次都调 LLM）
_NEW_TASK_CONTAINS = (
    "新任务", "换成", "切换", "换个任务", "改做", "做点别的", "去做别的",
    "先做", "接下来做", "现在做", "我要做", "换成做",
)


def _is_yes(text: str) -> bool:
    low = (text or "").strip().lower()
    if low in _YES:
        return True
    return any(k in low for k in _YES_CONTAINS)


def _is_no(text: str) -> bool:
    low = (text or "").strip().lower()
    if low in _NO:
        return True
    if any(k in low for k in _NO_CONTAINS):
        return True
    return any(low.startswith(w) for w in ("取消", "别", "不要"))


def _is_new_task_hint(text: str) -> bool:
    """关键词快通道：包含「我要做 / 接下来做 / 换做」等强烈提示切任务的子串。"""
    low = (text or "").strip()
    if not low:
        return False
    return any(k in low for k in _NEW_TASK_CONTAINS)


# ============================================================
# 自由语言 → LLM 意图分类
# ============================================================

_CLASSIFY_SYSTEM = """你是"计划确认"环节的意图理解器。
用户刚看完一个执行计划，给出了自由语言回复。请判断其意图并输出 JSON：

{"decision": "execute|cancel|adjust|new_task|unclear", "understanding": "一句话中文说明你对用户回复的理解"}

判定规则：
- execute：用户认可计划，让开始执行（如：执行 / 没问题 / 开始吧 / 就这么干）
- cancel：用户想放弃整个当前任务（如：算了 / 不做了 / 别搞了）
- adjust：用户想修改当前计划、提出顾虑或新要求（如：换一个更安全的方式 / 第2步先跳过 / 先别删）
- new_task：用户输入与当前任务无关，要切换到新任务
  - 特征：用户描述了一个全新的目标（如"我想做别的：删除 xxx"）
  - 或者明确表达"先做 A 再做 B"中的"B"如果是全新任务（但如果是当前任务的子步骤，仍是 adjust）
- unclear：语义含糊、与计划无关但又没明显新目标

只输出 JSON，不要输出其它内容。"""


def _classify_reply(text: str, plan: list[dict]) -> tuple[str, str]:
    """把用户的自由语言回复分类为 execute / cancel / adjust / new_task / unclear。

    返回 ``(decision, understanding)``。LLM 调用失败时回退为 adjust
    （把原话交给 planner，planner 本身也是 LLM，能再判断一次）。
    """
    try:
        from ..llm import extract_json, get_planner_llm

        plan_desc = "\n".join(
            f"{i + 1}. {(p.get('description') or '').strip()}" for i, p in enumerate(plan or [])
        )
        user_prompt = f"执行计划：\n{plan_desc or '（空）'}\n\n用户回复：{text}"
        llm = get_planner_llm()
        resp = llm.invoke(
            [SystemMessage(content=_CLASSIFY_SYSTEM), HumanMessage(content=user_prompt)]
        )
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        parsed = extract_json(raw) or {}
        if isinstance(parsed, dict):
            decision = str(parsed.get("decision") or "").strip().lower()
            understanding = str(parsed.get("understanding") or "").strip()
            if decision in ("execute", "cancel", "adjust", "new_task", "unclear"):
                return decision, understanding or text[:100]
    except Exception as e:
        log.warning("plan_confirm: 意图分类失败，回退为调整意见：%s", e)
    return "adjust", text[:100]


def _plan_has_high_risk(plan: list[dict]) -> bool:
    """粗略判断计划里是否有高风险动作（安装 / 删除 / 系统级改动等）。"""
    high_risk_patterns = (
        # 通用：安装 / 卸载 / 删除 / 移除
        r"\b安装\b", r"\binstall\b", r"\b卸载\b", r"\buninstall\b",
        r"删除", r"清空", r"清掉", r"清除", r"移除", r"\bdelete\b", r"\bremove\b", r"\brm\s+-rf\b",
        # 系统级
        r"\b注册表\b", r"\bregistry\b", r"\bPATH\b", r"环境变量",
        r"\b管理员\b", r"\bsudo\b", r"\b重启\b", r"\breboot\b",
        r"\b关机\b", r"\bshutdown\b", r"格式化", r"\bformat\b",
        r"\bmsi\b", r"\b\.exe\b", r"写入系统",
        # Windows 危险命令
        r"\bdel\s+/[fFsSqQ]\b", r"\brmdir\s+/s\b", r"\brd\s+/s\b",
        r"\bRemove-Item\b", r"\bFormat-Volume\b",
        r"\bdiskpart\b", r"\bbcdedit\b", r"\bicacls\b",
        r"\bvssadmin\b", r"\bStop-Computer\b", r"\bRestart-Computer\b",
        # agent 内部的高风险工具名
        r"\bdelete_path\b", r"\brun_installer\b", r"\bmove_path\b",
    )
    for step in plan or []:
        desc = (step.get("description") or "").lower()
        for pat in high_risk_patterns:
            if re.search(pat, desc, re.IGNORECASE):
                return True
    return False


def plan_confirm_node(state: dict) -> dict:
    """把计划展示给用户审核。"""
    plan = state.get("plan") or []
    revision = int(state.get("plan_revision") or 0)
    confirmed_rev = state.get("plan_confirmed_revision")

    # 1) 空计划 → 直接过
    if not plan:
        return {"plan_confirmed_revision": revision, "plan_feedback": ""}

    # 2) 该版本已确认过 → 不再重复确认
    if confirmed_rev is not None and int(confirmed_rev) >= revision:
        return {}

    # 3) 简单任务（单步且无高风险）自动放行
    if len(plan) <= 1 and not _plan_has_high_risk(plan):
        return {"plan_confirmed_revision": revision, "plan_feedback": ""}

    # 4) 需要确认：interrupt 暂停（CLI 只渲染结构化 plan + 选项）
    plan_text = render_plan(plan, state.get("current_step_index") or 0)
    high_risk = _plan_has_high_risk(plan)

    payload = {
        "kind": "plan_confirm",
        "plan": plan,
        "plan_text": plan_text,
        "revision": revision,
        "high_risk": high_risk,
        "summary": state.get("plan_summary") or "",
        "risks": state.get("plan_risks") or [],
        "question": "是否执行以上计划？",
        "options": ["执行", "调整", "取消"],
    }

    answer: Any = interrupt(payload)
    text = str(answer).strip() if answer is not None else ""

    # ---- 第一层：关键词快速通道 ----
    # 取消 → 结束任务
    if _is_no(text):
        log.info("plan_confirm: 用户取消任务")
        return _cancel_return(state, revision)

    # 同意 → 标记已确认，继续执行
    if _is_yes(text):
        log.info("plan_confirm: 用户同意计划 v%d", revision)
        return {"plan_confirmed_revision": revision, "plan_feedback": ""}

    # ---- 第二层：自由语言 → LLM 智能判断 ----
    decision, understanding = _classify_reply(text, plan)
    log.info("plan_confirm: 自由语言分类=%s 理解=%s", decision, understanding[:80])

    if decision == "cancel":
        return _cancel_return(state, revision)

    if decision == "execute":
        return {"plan_confirmed_revision": revision, "plan_feedback": ""}

    if decision == "new_task":
        # 用户想切换到新任务：标记 task_switched + 携带新任务内容
        # CLI 收到后开新会话跑
        log.info("plan_confirm: 用户切换到新任务：%s", text[:80])
        return _switch_to_new_task_return(state, revision, text, understanding)

    if decision == "unclear":
        # agent 输出自己的理解，请用户明确表达；用户答完 → clarify → plan 重新思考
        question = (
            f"我不太确定你的意思。我目前的理解是：{understanding}。"
            "—— 我该怎么继续？"
        )
        return {
            "clarify_questions": [
                {
                    "question": question,
                    "context": f"用户原话：{text[:200]}",
                }
            ],
            "plan_feedback": f"[用户回复（待明确）] {text}",
            "pending_confirmation": None,
        }

    # adjust → 视为调整意见，回到 plan 节点重新规划
    log.info("plan_confirm: 用户要求调整计划：%s", text[:80])
    return {
        "plan_feedback": text,
        "plan_confirmed_revision": None,
        # 把用户的调整意见作为最新输入注入，plan 节点会把它并进重新规划
        "messages": [HumanMessage(content=f"[计划调整意见] {text}")],
    }


def _cancel_return(state: dict, revision: int) -> dict:
    return {
        "is_done": True,
        "task_success": False,
        "end_reason": "user_cancelled",
        "final_answer": f"已按你的要求取消任务。原始目标：{(state.get('user_intent') or '')[:120]}",
        "plan_confirmed_revision": revision,
        "plan_feedback": "",
        "messages": [AIMessage(content="已取消任务（用户在计划确认时选择放弃）。")],
    }


def _switch_to_new_task_return(state: dict, revision: int, text: str, understanding: str) -> dict:
    """用户输入与当前任务无关，要切换到新任务。

    返回 ``_switch_to_task`` 让 CLI 检测到并开新会话跑。
    """
    return {
        "is_done": True,
        "task_success": True,
        "end_reason": "task_switched",
        "final_answer": f"好的，开始新任务：{text[:120]}",
        "plan_confirmed_revision": revision,
        "plan_feedback": "",
        "_switch_to_task": text,
        "messages": [AIMessage(content=f"已切换任务（agent 理解：{understanding[:120]}）。")],
    }


__all__ = ["plan_confirm_node"]
