"""plan 节点：生成 / 修订执行计划。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..llm import extract_json, get_planner_llm, parse_steps
from ..env_context import env_context_for_planner
from ..prompts import PLANNER_SYSTEM, build_planner_user, render_execution_digest

log = logging.getLogger(__name__)


def _norm(text: str) -> str:
    """归一化用于步骤匹配。"""
    text = (text or "").lower()
    text = re.sub(r"[\s，。、,.;:：；！!？?（）()\[\]【】\"'`]+", "", text)
    return text


def _parse_clarify_questions(raw: Any) -> list[dict]:
    """从模型输出里抽出 clarify_questions 列表。

    接受 JSON 数组或对象中的 ``clarify_questions`` 字段，宽松兜底。
    """
    if not raw:
        return []
    # raw 可能是 dict 也可能是字符串（已被 extract_json 解过的）
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []

    if isinstance(raw, dict):
        items = raw.get("clarify_questions") or raw.get("questions") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    out: list[dict] = []
    if not isinstance(items, list):
        return []
    for it in items:
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        if not q:
            continue
        opts_raw = it.get("options") or []
        opts = []
        if isinstance(opts_raw, list):
            opts = [str(o).strip() for o in opts_raw if str(o).strip()]
        out.append(
            {
                "question": q,
                "options": opts,
                "context": str(it.get("context") or "").strip(),
            }
        )
        if len(out) >= 5:
            break
    return out


def _reapply_status(new_steps: list[str], old_plan: list[dict]) -> list[dict]:
    """调整计划时，尽量保留旧步骤的完成/失败状态。"""
    done_map: dict[str, str] = {}
    for step in old_plan or []:
        status = step.get("status")
        if status in ("done", "failed", "skipped"):
            key = _norm(step.get("description", ""))
            if key:
                done_map[key] = status

    plan: list[dict] = []
    for i, desc in enumerate(new_steps):
        status = "pending"
        key = _norm(desc)
        if key and key in done_map:
            status = done_map[key]
        else:
            # 允许轻微改写：互相包含也算同一件事
            for old_key, old_status in done_map.items():
                if key and (key in old_key or old_key in key):
                    status = old_status
                    break
        plan.append(
            {
                "id": f"step-{i + 1}",
                "description": desc,
                "status": status,
                "result": None,
                "error": None,
            }
        )
    return plan


def _recent_context(messages, limit: int = 8) -> str:
    """取最近若干条对话，拼成规划参考。"""
    lines: list[str] = []
    for msg in (messages or [])[-limit:]:
        mtype = getattr(msg, "type", "")
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        content = " ".join(content.split())
        if not content:
            continue
        if mtype == "human":
            lines.append(f"用户: {content[:300]}")
        elif mtype == "ai":
            lines.append(f"助手: {content[:300]}")
    return "\n".join(lines)


def _last_error_block(state: dict) -> str:
    """取最近一次工具调用失败的摘要，规划器据此重规划。"""
    le = state.get("last_error") or {}
    if not le:
        return ""
    tool = le.get("tool_name") or "?"
    err = (le.get("error") or "").strip()[:400]
    step = le.get("step_id") or "?"
    return (
        f"### 上一步失败\n"
        f"工具: {tool}\n"
        f"步骤: {step}\n"
        f"错误: {err}\n"
        f"请据此调整计划（同一步骤不要再用相同参数重试）。"
    )


def plan_node(state: dict) -> dict:
    """生成或修订计划。"""
    from ...config import settings

    intent = (state.get("user_intent") or "").strip()
    if not intent:
        return {"plan": [], "plan_revision": int(state.get("plan_revision") or 0) + 1}

    mode = state.get("task_mode", "new_task")

    # 用户在回应技能建议：不需要重新规划，保留原计划即可
    if mode == "proposal_response":
        return {
            "plan": state.get("plan") or [],
            "plan_revision": int(state.get("plan_revision") or 0),
            "plan_note": "落实技能管理建议",
        }

    # plan_confirm 阶段用户给了调整意见：把它并入 intent 重新规划
    feedback = (state.get("plan_feedback") or "").strip()
    effective_intent = intent
    if feedback:
        effective_intent = f"{intent}\n（用户对上一版计划的调整意见：{feedback}）"
        mode = "adjust" if mode != "new_task" else mode

    old_plan = (state.get("plan") or []) if mode == "adjust" else []
    revision = int(state.get("plan_revision") or 0) + 1

    retrieved = state.get("retrieved_context") or []
    retrieved_text = "\n".join(
        f"- ({h.get('score', 0):.2f}) {h.get('text', '')[:400]}" for h in retrieved[:4]
    )

    # 上一次失败：拼进 prompt 让 LLM 重规划时知道要改什么
    last_err = _last_error_block(state)
    base_user_prompt = build_planner_user(
        user_intent=effective_intent,
        task_mode=mode,
        existing_plan=old_plan or None,
        recent_context=_recent_context(state.get("messages")),
        retrieved_context=retrieved_text,
        memory_text=_memory_text(),
        env_context=env_context_for_planner(),
    )
    if last_err:
        user_prompt = base_user_prompt + "\n\n" + last_err
    else:
        user_prompt = base_user_prompt

    try:
        llm = get_planner_llm()
        resp = llm.invoke(
            [SystemMessage(content=PLANNER_SYSTEM), HumanMessage(content=user_prompt)]
        )
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        parsed = extract_json(raw) or {}
        if not isinstance(parsed, dict):
            # 兜底：退化为按行解析
            parsed = {"steps": parse_steps(raw, max_steps=settings.MAX_PLAN_STEPS)}
        steps_raw = parsed.get("steps") or []
        # 方案描述与风险说明（plan_confirm 展示给用户）
        plan_summary = str(parsed.get("summary") or "").strip()
        plan_risks_raw = parsed.get("risks") or []
        if isinstance(plan_risks_raw, list):
            plan_risks = [str(r).strip() for r in plan_risks_raw if str(r).strip()]
        else:
            plan_risks = []
        # 如果 LLM 同时输出 clarify_questions，说明它判定信息不足 → steps 字段
        # 经常是空或残缺，必须强制忽略，避免把 JSON 字面量当步骤解析
        if parsed.get("clarify_questions"):
            steps: list[str] = []
        else:
            steps = []
            for it in steps_raw:
                if isinstance(it, str):
                    s = it.strip()
                elif isinstance(it, dict):
                    s = str(it.get("description") or it.get("step") or "").strip()
                else:
                    continue
                if s:
                    steps.append(s)
            if not steps:
                steps = parse_steps(raw, max_steps=settings.MAX_PLAN_STEPS)
        clarify = _parse_clarify_questions(parsed)
    except Exception as e:
        log.warning("plan: 规划失败，退化为单步计划: %s", e)
        steps = []
        clarify = []
        plan_summary = ""
        plan_risks = []

    # 信息不足 → 不强造步骤，避免把 JSON 字面量当步骤
    if clarify and not steps:
        plan = []
    elif not steps:
        steps = [effective_intent[:200]]

    # 构造计划（保留旧计划的完成状态）
    if old_plan:
        plan = _reapply_status(steps, old_plan)
    else:
        plan = [
            {
                "id": f"step-{i + 1}",
                "description": desc,
                "status": "pending",
                "result": None,
                "error": None,
            }
            for i, desc in enumerate(steps)
        ]

    # 找出第一个未完成步骤作为当前下标
    current = 0
    for i, step in enumerate(plan):
        if step.get("status") == "pending":
            current = i
            break
    else:
        current = len(plan)

    log.info(
        "plan: 修订 #%d，共 %d 步，澄清问题 %d 条",
        revision, len(plan), len(clarify),
    )

    # 把澄清问题挂到 state（让 builder 路由到 clarify 节点）
    out: dict = {
        "plan": plan,
        "current_step_index": current,
        "plan_revision": revision,
        "plan_note": "计划已更新" if old_plan else "计划已生成",
        "plan_summary": plan_summary,
        "plan_risks": plan_risks,
        "plan_feedback": "",  # 已消费，避免下轮重复并入
        "clarify_questions": clarify,
        "step_attempts": {},  # 重规划时清空步骤重试计数
    }
    return out


def _memory_text() -> str:
    """读取长期记忆（失败返回空）。"""
    try:
        from ...tools.task_tools import memory_file

        path = memory_file()
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    return ""


__all__ = ["plan_node"]
