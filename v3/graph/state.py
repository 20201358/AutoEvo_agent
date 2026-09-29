"""图状态定义。

字段按生命周期分三层：

1. **会话级**：跨回合保留，靠 checkpointer 持久化（messages / environment / safety）
2. **任务级**：每个新任务重置（plan / iteration / is_done ...）
3. **审计级**：只追加，永不重置（execution_history / reflections）
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from .reducers import keep_last, merge_dict


# ============================================================
# 主状态
# ============================================================

class AgentState(TypedDict, total=False):
    """交互式计算机操作助手的完整状态。"""

    # ---------------- 会话级 ----------------

    messages: Annotated[list[AnyMessage], add_messages]
    """完整对话历史（Human / AI / Tool / System），跨回合累积。"""

    conversation_id: str
    """会话（线程）标识，同时作为 checkpointer 的 thread_id。"""

    user_id: str

    environment: dict
    """机器环境信息，启动时注入一次，图内只读。"""

    safety: Annotated[dict, merge_dict]
    """安全策略；safety 相关节点可增量更新。"""

    context_summary: str
    """对较早历史的摘要（上下文工程用）。"""

    summarized_upto: int
    """已经被摘要覆盖到的消息下标。"""

    # ---------------- 任务级 ----------------

    task_id: str
    user_intent: str
    task_mode: Literal[
        "new_task", "adjust", "redirect", "cancel",
        "proposal_response", "followup",
    ]
    """intake 判定的本轮性质：
        new_task          全新任务（含用户换目标 redirect，重置后进入）
        adjust            在既有任务上追加/修改需求
        redirect          用户换了目标（intake 内会按 new_task 重置）
        cancel            用户取消当前任务
        proposal_response 用户正在回应技能管理建议
        followup          任务已结束后的后续对话
    """

    plan: list[dict]
    """当前计划，每项 {id, description, status, result, error}。"""

    current_step_index: int
    plan_revision: int
    plan_note: str

    plan_summary: str
    """计划的一句话概述（planner LLM 产出，plan_confirm 展示给用户）。"""

    plan_risks: list[str]
    """计划的风险说明列表（planner LLM 产出，plan_confirm 展示给用户）。"""

    plan_confirmed_revision: Optional[int]
    """已确认过的计划版本号；plan_confirm 节点用它防止重复确认。"""

    plan_feedback: str
    """用户对计划的最新调整意见（plan_confirm 收到非同意/取消的回复时写入）。"""

    clarify_questions: list[dict]
    """规划器发现信息不足时弹出的澄清问题，由 clarify 节点 interrupt 呈现。
    每项：{question, options, context}"""

    clarification_answers: Annotated[list[str], operator.add]
    """用户对澄清问题的回答（按顺序追加）。"""

    iteration: int
    """本回合 agent⇄tools 循环已执行轮数。"""

    end_reason: Optional[str]
    """结束原因：completed | incomplete | max_iterations | rejected | error"""

    # ---------------- 检索与技能 ----------------

    retrieved_context: list[dict]
    """RAG 检索到的知识块 [{text, score, source}]。"""

    active_skills: list[str]
    """本回合载入的技能名。"""

    skill_catalog: str
    """技能库目录文本（名称 + 描述），供 agent 判断是否需要加载。"""

    loaded_skill_text: str
    """active_skills 对应的技能正文（渐进披露）。"""

    # ---------------- 执行 ----------------

    pending_confirmation: Optional[dict]
    """待用户确认/回答的交互。非 None 时图会跳到 confirm 节点并 interrupt。"""

    resolved_confirmations: Annotated[dict, merge_dict]
    """已确认的工具调用：{tool_call_id: bool}。"""

    user_answers: Annotated[dict, merge_dict]
    """ask_user 的回答：{tool_call_id: str}。"""

    last_tool_output: Optional[str]
    execution_history: Annotated[list[dict], operator.add]
    """工具调用审计记录，跨任务累积。"""

    error_state: Annotated[dict, merge_dict]

    last_error: Optional[dict]
    """最近一次工具调用失败的摘要 {tool_name, error, step_id, at}，
    路由回 plan 时供重新规划使用。"""

    shell_error_confirm: bool
    """上一条 run_shell 命令失败过 → 下一条 shell 命令必须先经用户批准。
    命令成功后清除（失败则再次置位）。"""

    step_attempts: Annotated[dict, merge_dict]
    """当前计划每个步骤的失败次数；超过阈值时路由回 plan 重规划。"""

    # ---------------- 完成 ----------------

    is_done: bool
    task_success: bool
    final_answer: Optional[str]

    # ---------------- 任务切换 ----------------

    _switch_to_task: Optional[str]
    """plan_confirm 判定用户想切换到新任务时填入；CLI 收到后开新会话跑。
    新任务开始时清空。"""

    # ---------------- 自进化 ----------------

    skill_proposals: list[dict]
    """反思节点产出的技能管理建议。"""

    awaiting_proposal_response: bool
    """"为 True 时，下一轮用户输入将被视为对建议的回应。"""

    reflections: Annotated[list[dict], operator.add]
    """历史反思记录（审计用）。"""

    reflected_history_len: int
    """上次反思时 execution_history 的长度，避免同一批动作重复反思。"""

    turn_started_at: str
    """本回合开始时间（ISO 文本）。"""

    declined_proposals: Annotated[list[str], operator.add]
    """用户明确拒绝过的建议标识，避免重复推销。"""


# ============================================================
# 字段分类
# ============================================================

TASK_SCOPED_FIELDS: tuple[str, ...] = (
    "task_id",
    "user_intent",
    "task_mode",
    "plan",
    "current_step_index",
    "plan_revision",
    "plan_note",
    "plan_summary",
    "plan_risks",
    "plan_confirmed_revision",
    "plan_feedback",
    "clarify_questions",
    "iteration",
    "end_reason",
    "pending_confirmation",
    "last_tool_output",
    "is_done",
    "task_success",
    "final_answer",
    "retrieved_context",
    "active_skills",
    "loaded_skill_text",
    "_switch_to_task",
    "shell_error_confirm",
)
"""新任务开始时需要重置的字段。"""

SESSION_SCOPED_FIELDS: tuple[str, ...] = (
    "messages",
    "conversation_id",
    "user_id",
    "environment",
    "safety",
    "context_summary",
    "summarized_upto",
)
"""跨回合保留的字段。"""

AUDIT_FIELDS: tuple[str, ...] = (
    "execution_history",
    "reflections",
    "declined_proposals",
)
"""只追加不重置的字段。"""


# ============================================================
# 工厂
# ============================================================

def make_initial_state(
    user_input: str,
    conversation_id: str,
    environment: dict,
    safety: dict,
    user_id: str = "local",
) -> dict:
    """构造首次 invoke 的初始状态（只含会话级字段）。"""
    from langchain_core.messages import HumanMessage

    return {
        "messages": [HumanMessage(content=user_input)],
        "conversation_id": conversation_id,
        "user_id": user_id,
        "environment": environment,
        "safety": safety,
        "context_summary": "",
        "summarized_upto": 0,
    }


def make_task_reset() -> dict:
    """新任务的任务级字段重置字典。"""
    return {
        "plan": [],
        "current_step_index": 0,
        "plan_revision": 0,
        "plan_note": "",
        "plan_summary": "",
        "plan_risks": [],
        "plan_confirmed_revision": None,
        "plan_feedback": "",
        "clarify_questions": [],
        "iteration": 0,
        "end_reason": None,
        "pending_confirmation": None,
    "last_tool_output": None,
    "is_done": False,
    "task_success": True,
    "final_answer": None,
    "retrieved_context": [],
    "active_skills": [],
    "loaded_skill_text": "",
    "error_state": {
        "last_error": None,
        "error_count": 0,
        "consecutive_failures": 0,
        "strategy": None,
    },
    "last_error": None,
    "shell_error_confirm": False,
    "step_attempts": {},
        "_switch_to_task": None,
    }


def empty_confirmation() -> dict:
    """返回一个空的交互载荷。"""
    return {"kind": "confirm", "items": [], "question": "", "options": []}


__all__ = [
    "AgentState",
    "TASK_SCOPED_FIELDS",
    "SESSION_SCOPED_FIELDS",
    "AUDIT_FIELDS",
    "make_initial_state",
    "make_task_reset",
    "empty_confirmation",
]
