"""tools 节点：工具执行 + 安全检查 + 审计。

流程：

1. 取出最后一条 AIMessage 的 tool_calls
2. 逐个判定：允许 / 需确认 / 拒绝 / 需提问
3. 若存在「需确认」或「需提问」→ 只设置 ``pending_confirmation``，不写任何 ToolMessage，
   由条件边跳转到 confirm 节点。这样保证节点幂等：确认完成后重新进来会一次性执行全部调用。
4. 否则执行全部调用，写入 ToolMessage 与审计记录。

越权与异常都不会中断整轮：错误以 ToolMessage 的形式回给模型，让它自己修正。

【文件操作预检】
对 ``delete_path / move_path / copy_path / write_file`` 调用前自动跑存在性检查：
- 目标路径不存在时，把"原计划要删/建的文件不存在"这一事实直接写入 ToolMessage，
  便于 agent 看到真实状态而不是只能从工具报错里猜。
- 不强制拒绝（幂等删除是合理的）。

【错误回滚】
连续失败 ≥ ``REPLAN_AFTER_FAILURES``（默认 2）次同一步骤失败，会把
``needs_replan=True`` 写进 state，``route_after_tools`` 据此把路由指向 plan
节点重新规划，并把 ``last_error`` 拼进 prompt。
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from langchain_core.messages import AIMessage, ToolMessage

from ...safety import Verdict, check_command, check_path
from ...tools import INTERCEPTED_TOOLS, get_registered_map, get_tool_map

log = logging.getLogger(__name__)

_EXIT_RE = re.compile(r"exit_code=(-?\d+)")

REPLAN_AFTER_FAILURES = 2
"""同一计划步骤连续失败多少次后自动跳回 plan 节点重新规划。"""


# ============================================================
# 辅助
# ============================================================

def _last_ai_with_calls(messages) -> Optional[AIMessage]:
    for msg in reversed(messages or []):
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            return msg
    return None


def _summarize_step(plan: list[dict], index: int) -> str:
    if not plan or index >= len(plan):
        return ""
    return plan[index].get("description", "")


def _verdict_for_call(name: str, args: dict, safety: dict):
    """对单次工具调用做安全检查。

    检查顺序：
      1. 注册表元数据（requires_confirm / risk）—— 高风险工具先标记
      2. 具体语义检查（run_shell 的命令、文件工具的路径）
      3. 两层结果取更严格者
    """
    registered = get_registered_map().get(name)

    if name == "run_shell":
        v = check_command(str(args.get("command") or ""), safety)
    elif name in ("write_file", "edit_file", "delete_path", "move_path", "copy_path"):
        target = args.get("path") or args.get("destination") or ""
        v = check_path(str(target), safety, "write")
    elif name in ("read_file", "list_dir", "search_files", "glob_files"):
        v = check_path(str(args.get("path") or "."), safety, "read")
    elif name == "download_url":
        # 下载本身写盘；路径安全检查作用于 dest
        dest = args.get("dest") or ""
        v = check_path(str(dest), safety, "write") if dest else _allow()
    elif name in ("extract_archive", "run_installer", "add_to_path"):
        v = _allow()
    elif name == "kb_add":
        path = args.get("path")
        if path:
            v = check_path(str(path), safety, "read")
        else:
            v = _allow()
    elif name in ("consolidate_skills", "refine_skill",
                  "create_skill_from_history", "create_skill",
                  "update_skill", "delete_skill", "restore_skill"):
        # 技能写操作：依赖注册表的 requires_confirm（refine/consolidate 大改更敏感）
        v = _allow()
    else:
        v = _allow()

    # 注册表的 requires_confirm 标志再叠加一层
    if registered and registered.requires_confirm and v.decision == "allow":
        risk_note = {
            "high": "高风险操作（可能不可逆）",
            "medium": "会产生副作用的操作",
            "low": "常规操作",
        }.get(registered.risk, "操作")
        v = Verdict(
            decision="confirm",
            reason=f"「{name}」{risk_note}，需要你确认",
            rule=f"tool:{name}",
        )

    return v


def _allow() -> Verdict:
    return Verdict("allow", "无需额外检查")


def _parse_shell_meta(output: str) -> tuple[Optional[int], str, str]:
    """从 run_shell 的格式化输出里解析 exit_code / stdout / stderr。"""
    exit_code: Optional[int] = None
    m = _EXIT_RE.search(output or "")
    if m:
        try:
            exit_code = int(m.group(1))
        except ValueError:
            exit_code = None

    stdout, stderr = "", ""
    if "stdout:" in output:
        rest = output.split("stdout:", 1)[1]
        if "stderr:" in rest:
            stdout, stderr = rest.split("stderr:", 1)
        else:
            stdout = rest
    else:
        stdout = output
    return exit_code, stdout.strip(), stderr.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# 路径预检（文件不存在也要把事实告诉 agent）
# ============================================================

def _precheck_file_operation(name: str, args: dict) -> Optional[str]:
    """对文件操作工具做存在性预检；返回一段说明文字（写入 ToolMessage 前缀）。"""
    if name in ("delete_path", "move_path", "copy_path"):
        # source / path / destination 三选一
        target = (
            args.get("path")
            or args.get("source")
            or args.get("destination")
            or ""
        )
        if not target:
            return None
        try:
            p = Path(str(target)).expanduser()
            exists = p.exists()
            is_dir = p.is_dir() if exists else None
            kind = "目录" if is_dir else ("文件" if exists else None)
            if not exists:
                return (
                    f"⚠️ 路径预检：{target!r} 当前不存在。\n"
                    "  • 如果用户希望幂等删除不存在的文件，可继续执行（返回「无操作」）；\n"
                    "  • 如果用户希望删除特定文件，请先 glob_files / search_files 找到精确路径，\n"
                    "    可能是路径写错、或文件在别的目录（比如桌面、Downloads）。"
                )
            return (
                f"ℹ️ 路径预检：{target!r} 是已存在的{kind or '路径'}，将执行 {name}。"
            )
        except Exception as e:
            return f"[路径预检失败: {type(e).__name__}: {e}]"
    if name == "write_file":
        target = str(args.get("path") or "")
        if not target:
            return None
        try:
            p = Path(target).expanduser()
            parent = p.parent
            exists = p.exists()
            parent_exists = parent.exists() if parent else True
            if not exists and parent_exists:
                return (
                    f"ℹ️ 路径预检：{target!r} 尚不存在，父目录存在，将创建新文件。"
                )
            if not exists and not parent_exists:
                return (
                    f"⚠️ 路径预检：{target!r} 的父目录 {str(parent)!r} 不存在。\n"
                    "  写入会失败。建议先创建目录，或确认完整路径是否正确。"
                )
            return None
        except Exception as e:
            return f"[路径预检失败: {type(e).__name__}: {e}]"
    return None


# ============================================================
# 主节点
# ============================================================

def tools_node(state: dict) -> dict:
    from ...config import settings

    messages = state.get("messages") or []
    last_ai = _last_ai_with_calls(messages)
    if last_ai is None:
        return {}

    calls = list(last_ai.tool_calls)
    resolved: dict = dict(state.get("resolved_confirmations") or {})
    answers: dict = dict(state.get("user_answers") or {})
    safety = state.get("safety") or {}

    tool_map = get_tool_map()
    task_id = state.get("task_id") or "task-unknown"

    exec_plan: list[dict] = []
    pending: list[dict] = []

    for call in calls:
        name = call.get("name") or ""
        cid = call.get("id") or ""
        args = call.get("args") or {}

        # ---- 需要用户回答的提问 ----
        if name == "ask_user":
            if cid in answers:
                exec_plan.append({"call": call, "mode": "answer", "answer": answers[cid]})
            else:
                pending.append(
                    {
                        "tool_call_id": cid,
                        "tool_name": name,
                        "args": args,
                        "kind": "ask",
                        "reason": "需要用户提供信息",
                        "question": args.get("question") or "",
                        "options": [
                            o.strip()
                            for o in str(args.get("options") or "").split("|")
                            if o.strip()
                        ],
                        "context": args.get("context") or "",
                    }
                )
            continue

        # ---- 安全检查 ----
        verdict = _verdict_for_call(name, args, safety)
        # _verdict_for_call 内部已综合注册表 requires_confirm + 语义检查

        # ---- 命令失败后的重试需用户批准 ----
        # 上一条 run_shell 失败过：本条 shell 命令必须先经用户确认才执行
        if (
            name == "run_shell"
            and state.get("shell_error_confirm")
            and verdict.decision == "allow"
        ):
            verdict = Verdict(
                decision="confirm",
                reason="上一条 shell 命令执行失败；执行新的 shell 命令前需要你批准",
                rule="shell:after_error",
            )

        if verdict.decision == "deny":
            exec_plan.append({"call": call, "mode": "deny", "verdict": verdict})
            continue

        if verdict.decision == "confirm":
            if cid not in resolved:
                pending.append(
                    {
                        "tool_call_id": cid,
                        "tool_name": name,
                        "args": args,
                        "kind": "confirm",
                        "reason": verdict.reason,
                        "rule": verdict.rule,
                    }
                )
                continue
            if cid in resolved and resolved[cid] is False:
                exec_plan.append({"call": call, "mode": "user_denied", "verdict": verdict})
                continue

        exec_plan.append({"call": call, "mode": "run", "verdict": verdict})

    # ---------------- 有挂起项：交给 confirm 节点 ----------------
    if pending:
        has_ask = any(p["kind"] == "ask" for p in pending)
        kind = "ask" if has_ask else "confirm"
        payload = {
            "kind": kind,
            "items": pending,
            "question": next((p.get("question") for p in pending if p["kind"] == "ask"), ""),
            "options": next((p.get("options") for p in pending if p["kind"] == "ask"), []),
        }
        log.info("tools: 等待用户交互 (%s)，共 %d 项", kind, len(pending))
        return {"pending_confirmation": payload}

    # ---------------- 执行 ----------------
    tool_messages: list[ToolMessage] = []
    audit: list[dict] = []
    plan = [dict(s) for s in (state.get("plan") or [])]
    current_index = int(state.get("current_step_index") or 0)
    step_id = None
    if plan and current_index < len(plan):
        step_id = plan[current_index].get("id")

    state_updates: dict = {}

    # 每个工具调用前的"路径预检"说明（写进 ToolMessage 前缀）
    precheck_notes: dict[str, str] = {}
    for call in calls:
        _name = call.get("name") or ""
        _args = call.get("args") or {}
        note = _precheck_file_operation(_name, _args)
        if note:
            precheck_notes[call.get("id") or ""] = note

    # 本轮最后一条 run_shell 是否失败（None = 本轮没跑过 shell）
    shell_last_failed: Optional[bool] = None

    for item in exec_plan:
        call = item["call"]
        name = call.get("name") or ""
        cid = call.get("id") or ""
        args = call.get("args") or {}
        mode = item["mode"]

        started = _now()
        t0 = time.perf_counter()

        # ---- 拒绝 / 用户否决 / 直接回答 ----
        if mode == "deny":
            content = f"⛔ 操作被安全策略拒绝，未执行。原因：{item['verdict'].reason}"
            tool_messages.append(ToolMessage(content=content, tool_call_id=cid, name=name))
            audit.append(_audit(task_id, step_id, name, args, cid, started, t0, blocked=True, stdout="", stderr=item["verdict"].reason))
            continue

        if mode == "user_denied":
            content = "🙅 用户拒绝执行该操作，未执行。请改用其他方式，或询问用户希望怎么做。"
            tool_messages.append(ToolMessage(content=content, tool_call_id=cid, name=name))
            audit.append(_audit(task_id, step_id, name, args, cid, started, t0, blocked=True, stdout="", stderr="用户拒绝"))
            continue

        if mode == "answer":
            content = f"用户回答：{item['answer']}"
            tool_messages.append(ToolMessage(content=content, tool_call_id=cid, name=name))
            audit.append(_audit(task_id, step_id, name, args, cid, started, t0, blocked=False, stdout=str(item["answer"]), stderr=""))
            continue

        # ---- 由图接管的工具 ----
        if name in INTERCEPTED_TOOLS:
            content, extra = _handle_intercepted(name, args, plan, current_index)
            state_updates.update(extra)
            tool_messages.append(ToolMessage(content=content, tool_call_id=cid, name=name))
            audit.append(_audit(task_id, step_id, name, args, cid, started, t0, blocked=False, stdout=content, stderr=""))
            continue

        # ---- 正常执行 ----
        tool = tool_map.get(name)
        if tool is None:
            content = f"错误：未知工具 {name!r}。请只使用给定工具清单中的工具。"
            tool_messages.append(ToolMessage(content=content, tool_call_id=cid, name=name))
            audit.append(_audit(task_id, step_id, name, args, cid, started, t0, blocked=False, stdout="", stderr="未知工具"))
            continue

        try:
            raw = tool.invoke(args)
            content = raw if isinstance(raw, str) else str(raw)
            error = None
        except Exception as e:
            content = (
                f"工具执行异常：{type(e).__name__}: {e}\n"
                "请检查参数格式（必填项、类型）后重试。"
            )
            error = str(e)
            log.warning("tools: %s 执行失败: %s", name, e)

        # 路径预检说明（如果存在）拼到 ToolMessage 顶部
        precheck = precheck_notes.get(cid)
        if precheck:
            content = precheck + "\n\n── 工具返回 ──\n" + content

        tool_messages.append(ToolMessage(content=content, tool_call_id=cid, name=name))

        exit_code, stdout, stderr = (None, "", "")
        if name == "run_shell":
            exit_code, stdout, stderr = _parse_shell_meta(content)
            # 记录 shell 失败状态：失败 → 下一条 shell 命令需用户批准；成功 → 解除。
            # exit_code 解析不到（语法拦截/异常，命令未执行）→ 不改变现有状态。
            if exit_code is None:
                shell_last_failed = None
            else:
                shell_last_failed = exit_code != 0
        else:
            stdout = content
            if error:
                stderr = error
                exit_code = 1

        # 「事实反馈」类工具的失败不算错误（路径不存在是预期可能结果）
        is_informational_failure = (
            name in ("delete_path", "move_path", "copy_path")
            and exit_code not in (0, None)
            and "不存在" in (stderr or "")
        )

        audit.append(
            _audit(
                task_id, step_id, name, args, cid, started, t0,
                blocked=False, stdout=stdout, stderr=stderr, exit_code=exit_code,
            )
        )

    # ---------------- 更新计划状态 ----------------
    if shell_last_failed is not None:
        state_updates["shell_error_confirm"] = bool(shell_last_failed)
    if plan and current_index < len(plan):
        step = plan[current_index]
        if step.get("status") in ("pending", None):
            step["status"] = "running"
        # 把最近一次工具结果挂到当前步骤上，便于展示
        if audit:
            last = audit[-1]
            if last.get("stderr") and last.get("exit_code") not in (0, None):
                step["error"] = (last.get("stderr") or "")[:300]
            else:
                step["result"] = (last.get("stdout") or "")[:300]
        state_updates["plan"] = plan

    state_updates["messages"] = tool_messages
    state_updates["execution_history"] = audit
    state_updates["last_tool_output"] = tool_messages[-1].content if tool_messages else None

    # ---------------- 失败检测 + 自动重规划 ----------------
    # 「事实反馈」类失败（路径不存在等）不算触发回滚；只算 shell/工具真正出错
    real_failures = [
        a for a in audit
        if a.get("exit_code") not in (0, None) and not a.get("blocked")
    ]
    if real_failures:
        last_fail = real_failures[-1]
        state_updates["last_error"] = {
            "tool_name": last_fail.get("tool_name"),
            "tool_call_id": last_fail.get("tool_call_id"),
            "step_id": last_fail.get("step_id"),
            "error": (last_fail.get("stderr") or "")[:500],
            "at": last_fail.get("started_at"),
        }
        # 步骤失败计数
        sid = last_fail.get("step_id") or "step-?"
        prev_attempts = dict(state.get("step_attempts") or {})
        prev_attempts[sid] = int(prev_attempts.get(sid) or 0) + 1
        state_updates["step_attempts"] = prev_attempts
        # 连续失败次数（跨步骤累计）
        prev_es = dict(state.get("error_state") or {})
        prev_cf = int(prev_es.get("consecutive_failures") or 0)
        state_updates["error_state"] = {
            "last_error": (last_fail.get("stderr") or "")[:300],
            "error_count": int(prev_es.get("error_count") or 0) + len(real_failures),
            "consecutive_failures": prev_cf + 1,
            "strategy": "retry" if prev_cf < REPLAN_AFTER_FAILURES - 1 else "replan",
        }
        # 触发回滚：同一 plan_step 连续失败 ≥ 阈值，跳回 plan
        sid_count = prev_attempts.get(sid, 0)
        if sid_count >= REPLAN_AFTER_FAILURES:
            state_updates["needs_replan"] = True
            state_updates["plan_note"] = (
                f"步骤 {sid} 连续失败 {sid_count} 次，自动回滚到 plan 节点重新规划"
            )
            # 把当前步骤状态置为 failed（让重规划时不会再保留它）
            for s in plan:
                if s.get("id") == sid:
                    s["status"] = "failed"
                    s["error"] = (last_fail.get("stderr") or "")[:200]
            state_updates["plan"] = plan
            log.warning(
                "tools: 步骤 %s 连续失败 %d 次，触发自动重规划",
                sid, sid_count,
            )
    else:
        # 本轮没有失败：清零连续失败计数（但保留 total error_count）
        if (state.get("error_state") or {}).get("consecutive_failures"):
            state_updates["error_state"] = {
                "consecutive_failures": 0,
                "strategy": "ok",
            }

    log.info("tools: 执行 %d 个工具调用", len(tool_messages))
    return state_updates


# ============================================================
# 审计与接管工具
# ============================================================

def _audit(
    task_id, step_id, name, args, cid, started, t0,
    blocked: bool, stdout: str, stderr: str, exit_code=None,
) -> dict:
    return {
        "task_id": task_id,
        "step_id": step_id,
        "tool_name": name,
        "tool_input": args,
        "tool_call_id": cid,
        "exit_code": exit_code,
        "stdout": (stdout or "")[:4000],
        "stderr": (stderr or "")[:2000],
        "started_at": started,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "blocked": blocked,
    }


def _handle_intercepted(name: str, args: dict, plan: list[dict], current_index: int):
    """处理 update_plan / finish_task 这类会改动状态的工具。"""
    if name == "update_plan":
        raw = str(args.get("steps") or "")
        reason = str(args.get("reason") or "")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return "错误：steps 为空，未更新计划。", {}
        new_plan = [
            {
                "id": f"step-{i + 1}",
                "description": re.sub(r"^(?:[-*•]|\d+[.、)])\s*", "", ln),
                "status": "pending",
                "result": None,
                "error": None,
            }
            for i, ln in enumerate(lines)
        ]
        return (
            f"计划已更新为 {len(new_plan)} 步。原因：{reason or '(未说明)'}",
            {"plan": new_plan, "current_step_index": 0, "plan_note": reason or "计划已更新"},
        )

    if name == "finish_task":
        summary = str(args.get("summary") or "").strip()
        success = bool(args.get("success", True))
        plan_done = [dict(s) for s in plan]
        for step in plan_done:
            if step.get("status") in ("pending", "running"):
                step["status"] = "done" if success else "skipped"
        return (
            f"已结束任务（{'成功' if success else '未完成'}）。",
            {
                "is_done": True,
                "task_success": success,
                "final_answer": summary,
                "end_reason": "completed" if success else "incomplete",
                "plan": plan_done,
            },
        )

    return f"(工具 {name} 未实现)", {}


__all__ = ["tools_node"]
