"""图构建。

拓扑：

    START → intake → recall → plan → clarify → plan_confirm → agent
    clarify   ──(无问题)──→ plan_confirm
    clarify   ──(用户答完)──→ plan
    plan_confirm ──(用户取消)──→ finish
    plan_confirm ──(反馈)──→ plan
    agent ──(有工具调用)──→ tools
    agent ──(没有工具调用)──→ finish
    tools ──(需确认/需提问)──→ confirm → tools
    tools ──(连续失败≥阈值)──→ plan（重新规划）
    tools ──(完成)──→ finish
    tools ──(否则)──→ agent
    finish → reflect → END

``plan_confirm`` 只在以下情况暂停（interrupt）：
    - 计划多于 1 步
    - 或计划包含高风险动作（安装 / 删除 / 系统级改动）
    - 且该版本计划尚未被确认过
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from .nodes.agent import agent_node
from .nodes.clarify import clarify_node, has_clarify
from .nodes.confirm import confirm_node
from .nodes.finish import finish_node
from .nodes.intake import intake_node
from .nodes.plan import plan_node
from .nodes.plan_confirm import plan_confirm_node
from .nodes.recall import recall_node
from .nodes.reflect import reflect_node
from .nodes.tools import tools_node
from .state import AgentState

log = logging.getLogger(__name__)


# ============================================================
# 路由
# ============================================================

def route_after_intake(state: dict) -> str:
    """intake 之后：用户取消 → finish；否则 → recall。"""
    if state.get("is_done") and state.get("end_reason") == "user_cancelled":
        return "finish"
    return "recall"


def route_after_agent(state: dict) -> str:
    """agent 之后：继续调工具，还是收尾。"""
    from ..config import settings

    if state.get("is_done") or state.get("end_reason") == "error":
        return "finish"

    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    has_calls = isinstance(last, AIMessage) and bool(getattr(last, "tool_calls", None))

    if not has_calls:
        return "finish"

    if int(state.get("iteration") or 0) >= settings.MAX_ITERATIONS:
        log.warning("达到最大迭代次数 %d，强制收尾", settings.MAX_ITERATIONS)
        return "finish"

    return "tools"


def route_after_plan(state: dict) -> str:
    """plan 之后：有澄清问题 → clarify；否则 → plan_confirm。"""
    if has_clarify(state):
        return "clarify"
    return "plan_confirm"


def route_after_clarify(state: dict) -> str:
    """clarify 之后：用户取消 → finish；还有未答的 → 再来一轮 clarify；
    用户已明确表达（plan_feedback 非空）→ 回 plan 重新思考；
    否则 → plan_confirm。
    """
    if state.get("is_done") or state.get("end_reason") == "user_cancelled":
        return "finish"
    if has_clarify(state):
        return "clarify"
    # 用户答完 → 带着答案回 plan 重新规划（而不是拿旧计划直接确认）
    if (state.get("plan_feedback") or "").strip():
        return "plan"
    return "plan_confirm"


def route_after_plan_confirm(state: dict) -> str:
    """plan_confirm 之后：取消 → finish；无法理解 → clarify；调整 → plan；否则 → agent。"""
    if state.get("is_done") or state.get("end_reason") == "user_cancelled":
        return "finish"
    # agent 无法理解用户回复 → 转澄清追问
    if has_clarify(state):
        return "clarify"
    # 用户给了调整意见 → 回 plan 重新规划
    feedback = (state.get("plan_feedback") or "").strip()
    if feedback and state.get("plan_confirmed_revision") is None:
        return "plan"
    return "agent"


def route_after_tools(state: dict) -> str:
    """tools 之后：需要用户交互 / 任务已结束 / 回到 agent / 跳回 plan 重新规划。"""
    from ..config import settings

    if state.get("pending_confirmation"):
        return "confirm"
    if state.get("is_done"):
        return "finish"
    # 连续失败 → 自动跳回 plan 重新规划（route_after_plan 把 last_error 拼进 prompt）
    if state.get("needs_replan"):
        return "plan"
    if int(state.get("iteration") or 0) >= settings.MAX_ITERATIONS:
        return "finish"
    return "agent"


# ============================================================
# 构建
# ============================================================

def build_graph(
    checkpointer=None,
    with_reflection: bool = True,
    with_plan_confirm: bool = True,
    with_clarify: bool = True,
):
    """构建并编译状态图。"""
    builder = StateGraph(AgentState)

    builder.add_node("intake", intake_node)
    builder.add_node("recall", recall_node)
    builder.add_node("plan", plan_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("confirm", confirm_node)
    builder.add_node("finish", finish_node)

    builder.add_edge(START, "intake")
    builder.add_conditional_edges(
        "intake",
        route_after_intake,
        {"recall": "recall", "finish": "finish"},
    )

    if with_plan_confirm:
        if with_clarify:
            builder.add_node("clarify", clarify_node)
        builder.add_node("plan_confirm", plan_confirm_node)
        builder.add_edge("recall", "plan")
        builder.add_conditional_edges(
            "plan",
            route_after_plan,
            {"clarify": "clarify", "plan_confirm": "plan_confirm"},
        )
        if with_clarify:
            builder.add_conditional_edges(
                "clarify",
                route_after_clarify,
                {"clarify": "clarify", "plan": "plan", "plan_confirm": "plan_confirm", "finish": "finish"},
            )
        builder.add_conditional_edges(
            "plan_confirm",
            route_after_plan_confirm,
            {"plan": "plan", "agent": "agent", "finish": "finish", "clarify": "clarify"},
        )
    else:
        builder.add_edge("recall", "plan")
        builder.add_edge("plan", "agent")

    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "finish": "finish"},
    )
    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        {"confirm": "confirm", "agent": "agent", "plan": "plan", "finish": "finish"},
    )
    builder.add_edge("confirm", "tools")

    if with_reflection:
        builder.add_node("reflect", reflect_node)
        builder.add_edge("finish", "reflect")
        builder.add_edge("reflect", END)
    else:
        builder.add_edge("finish", END)

    return builder.compile(checkpointer=checkpointer)


__all__ = [
    "build_graph",
    "route_after_agent",
    "route_after_tools",
    "route_after_plan",
    "route_after_clarify",
    "route_after_plan_confirm",
]