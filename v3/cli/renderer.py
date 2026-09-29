"""渲染层：把 agent 事件 / plan / 步骤状态漂亮地画到终端。

设计目标：

* 不显示 graph 节点名（intake/recall/plan_confirm/...）—— 用户只关心结果。
* plan：每个步骤一行，左对齐，状态符号。
* 步骤执行：▶ running → ✓ done / ✘ failed；终端输出缩进放在下面。
* agent 输出：单行 ``[sid] v3 > <text>`` 前缀（多行也只前缀第一行）。
* 中断：plan_confirm / clarify / ask —— 用醒目方块包围。
"""

from __future__ import annotations

import shutil
from typing import Any, Iterable, Optional

# ANSI 颜色
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_MAGENTA = "\033[35m"
_CYAN = "\033[36m"


def _c(code: str, text: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str, color: bool = True) -> str:
    return _c("1", t, color)


def dim(t: str, color: bool = True) -> str:
    return _c("2", t, color)


def red(t: str, color: bool = True) -> str:
    return _c("31", t, color)


def green(t: str, color: bool = True) -> str:
    return _c("32", t, color)


def yellow(t: str, color: bool = True) -> str:
    return _c("33", t, color)


def cyan(t: str, color: bool = True) -> str:
    return _c("36", t, color)


def magenta(t: str, color: bool = True) -> str:
    return _c("35", t, color)


# ============================================================
# 状态标记
# ============================================================

_STATUS_MARK = {
    "pending":  ("○", _DIM,  "○"),
    "running":  ("▶", _CYAN,  "▶"),
    "done":     ("✓", _GREEN, "✓"),
    "failed":   ("✘", _RED,   "✘"),
    "skipped":  ("⏭", _YELLOW, "⏭"),
    "blocked":  ("⊘", _YELLOW, "⊘"),
}


def status_mark(status: str, use_color: bool = True) -> str:
    """返回状态对应的彩色符号串。"""
    sym, code, plain = _STATUS_MARK.get(status, ("?", "2", "?"))
    return _c(code, sym, use_color)


# ============================================================
# 参数 / 描述格式化（统一所有输出结构的摘要规则）
# ============================================================

# 这些 key 的值往往是大段文本（文件内容、总结、查询），只做摘要展示
_BLOB_ARG_KEYS = {"content", "text", "summary", "body", "code", "doc", "prompt", "query"}
# finish_task 等工具的布尔型参数不直接展示
_SKIP_ARG_KEYS = {"success"}


def _one_line(s: Any) -> str:
    """压成单行（换行/连续空白 → 单空格）。"""
    return " ".join(str(s).split())


def fmt_arg(key: str, value: Any, max_len: int = 48) -> str:
    """单个参数值的摘要：长文本截断，blob 类标注总长度。"""
    s = _one_line(value)
    if len(s) > max_len:
        if key in _BLOB_ARG_KEYS:
            return f"{s[:max_len]}…（共 {len(s)} 字）"
        return s[:max_len] + "…"
    return s


def fmt_args(args: dict, max_len: int = 48, max_total: int = 110) -> str:
    """参数字典 → ``k=v, k=v`` 摘要串（总长超限再截）。"""
    parts = []
    for k, v in (args or {}).items():
        if k in _SKIP_ARG_KEYS:
            continue
        parts.append(f"{k}={fmt_arg(k, v, max_len)}")
    s = ", ".join(parts)
    if len(s) > max_total:
        s = s[:max_total] + "…"
    return s


def short_desc(desc: Any, max_len: int = 72) -> str:
    """步骤描述截断（写文件内容等长文本只保留开头）。"""
    s = _one_line(desc)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def render_tool_call(name: str, args: dict, use_color: bool = True) -> str:
    """工具调用一行摘要（箭头后的内容）。

    * ``finish_task``：不暴露 ``success=True`` 这类裸参数，
      渲染为 ``finish_task(✓ 摘要)``。
    * 其它工具：``name(k=v, k=v)``，blob 参数摘要。
    """
    if name == "finish_task":
        summary = _one_line((args or {}).get("summary") or "")
        ok_flag = bool((args or {}).get("success", True))
        mark = green("✓", use_color) if ok_flag else red("✗", use_color)
        if len(summary) > 60:
            summary = summary[:60] + "…"
        return f"finish_task({mark} {summary})"
    return f"{name}({fmt_args(args)})"


# ============================================================
# Plan / 步骤渲染
# ============================================================

def render_plan(plan: list[dict], use_color: bool = True) -> str:
    """把 plan 列表渲染成对齐文字描述（统一 ``[step-N]`` 前缀）。"""
    if not plan:
        return ""
    lines = []
    lines.append(bold("Plan:", use_color))
    width = max(len(f"[step-{i+1}]") for i in range(len(plan)))
    for i, p in enumerate(plan):
        idx = f"[step-{i+1}]".ljust(width)
        status = p.get("status") or "pending"
        desc = short_desc(p.get("description") or p.get("action") or "?")
        mark = status_mark(status, use_color)
        lines.append(f"  {idx} {mark} {desc}")
    return "\n".join(lines)


def render_plan_step(
    step_id: int,
    description: str,
    status: str,
    output: Optional[str] = None,
    use_color: bool = True,
) -> str:
    """单步状态变化事件渲染。

    ``output`` 是这一步产生的 stdout 片段（可空）。
    """
    # step_id 可能是 1 也可能已是 "step-1"（session 事件里两种都有），避免 [step-step-1]
    sid = str(step_id)
    idx = f"[{sid}]" if sid.startswith("step-") else f"[step-{sid}]"
    mark = status_mark(status, use_color)
    head = f"  {idx} {mark} {short_desc(description)}"
    if not output:
        return head
    body = indent(output, "    ")
    return f"{head}\n{body}"


def render_plan_summary(plan: list[dict], use_color: bool = True) -> str:
    """一行总结 plan 的执行情况（用于对话结束后）。"""
    if not plan:
        return ""
    parts = []
    for i, p in enumerate(plan):
        s = p.get("status") or "?"
        d = (p.get("description") or "")[:30]
        parts.append(f"{status_mark(s, use_color)}[step-{i+1}] {d}")
    return "  " + "  ".join(parts)


# ============================================================
# 终端输出片段缩进
# ============================================================

def indent(text: str, prefix: str = "    ") -> str:
    """给 text 每行加 prefix。"""
    if not text:
        return ""
    out_lines = []
    for line in text.splitlines():
        if line.strip():
            out_lines.append(prefix + line)
        else:
            out_lines.append("")
    return "\n".join(out_lines)


def trim_output(text: str, max_lines: int = 8, max_width: int = 100) -> str:
    """把 stdout 截短，避免长输出撑爆屏幕。"""
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        kept.append(f"... (+{len(lines) - max_lines} 行省略)")
        text = "\n".join(kept)
    # 行宽截断（保留可读性）
    out = []
    for line in text.splitlines():
        if len(line) > max_width:
            out.append(line[: max_width - 1] + "…")
        else:
            out.append(line)
    return "\n".join(out)


# ============================================================
# Agent 输出
# ============================================================

def render_agent_text(
    text: str,
    prefix: str,
    use_color: bool = True,
) -> str:
    """Agent 的纯文本响应渲染。

    ``prefix`` 一般是 ``[4dc7 项目A] v3 >``。
    多行时只在第一行加前缀，后续行不缩进（更像终端对话风格）。
    """
    text = (text or "").rstrip()
    if not text:
        return ""
    lines = text.splitlines()
    # 跳过开头的空行（agent 输出经常以 \n 开头）
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ""
    out = []
    out.append(_c("36", prefix, use_color) + " " + lines[0])
    for line in lines[1:]:
        out.append(line)
    return "\n".join(out)


def _display_width(s: str) -> int:
    """粗略估算显示宽度（中文按 2）。"""
    w = 0
    for ch in s:
        if ord(ch) > 0x2E80:  # CJK / 全角
            w += 2
        else:
            w += 1
    return w


# ============================================================
# 中断渲染
# ============================================================

def render_plan_confirm(payload: dict, use_color: bool = True) -> str:
    """``plan_confirm`` 中断：计划只显示一遍 + 方案描述 + 风险说明。

    格式：
    ┌─ 已生成方案 ─ ⚠ 高风险
    │ [step-1] ○ 描述
    │ [step-2] ○ 描述
    │ ────────────────────
    │ 概述：一句话说明
    │ 风险：
    │   • 风险1
    │   • 风险2
    │
    │ 告诉我你的想法 —— 确认 / 调整 / 取消 / 或直接说要做别的
    └────────────────────────────────────────
    """
    high_risk = payload.get("high_risk", False)
    summary = (payload.get("summary") or "").strip()
    risks = payload.get("risks") or []
    if isinstance(risks, str):
        risks = [risks]

    out = []
    out.append("")
    title = "已生成方案"
    if high_risk:
        title += " ─ ⚠ 高风险"
    out.append(cyan(f"┌─ {title} ", use_color))

    # 计划步骤（只显示一遍）
    plan = payload.get("plan") or []
    if plan:
        for i, p in enumerate(plan):
            desc = short_desc(p.get("description") or p.get("action") or "?")
            out.append(
                f"│ {yellow(f'[step-{i + 1}]', use_color)} "
                f"{status_mark(p.get('status') or 'pending', use_color)} {desc}"
            )
    else:
        plan_text = payload.get("plan_text") or ""
        for line in plan_text.splitlines():
            if line.strip():
                out.append("│ " + line)

    # 分隔线 + 描述部分
    has_desc = bool(summary) or bool(risks)
    if has_desc:
        out.append("│ " + dim("─" * 38, use_color))
        if summary:
            out.append("│ " + bold("概述：", use_color) + summary)
        if risks:
            out.append("│ " + bold("风险：", use_color))
            for r in risks:
                out.append("│   " + yellow(f"• {r}", use_color))

    out.append("│")
    out.append("│ " + dim("告诉我你的想法 —— 确认 / 调整 / 取消 / 或直接说要做别的", use_color))
    out.append(cyan("└" + "─" * 40, use_color))
    return "\n".join(out)


def render_clarify(payload: dict, use_color: bool = True) -> str:
    """clarify 中断：列出问题（不显示选项）—— 让用户自由回答。

    DeepSeek 风格：把 agent 当对话伙伴，agent 说"我理解的是…"，用户自由补充。
    """
    questions = payload.get("questions") or []
    out = []
    out.append("")
    out.append(cyan("┌─ 信息澄清 ─", use_color))
    out.append("│")
    for i, q in enumerate(questions, 1):
        text = q.get("question") or q.get("text") or "?"
        out.append(f"│ {yellow(f'[{i}]', use_color)} {text}")
    out.append("│")
    out.append("│ " + dim("直接告诉我你的想法即可", use_color))
    out.append(cyan("└" + "─" * 40, use_color))
    return "\n".join(out)


def render_ask(payload: dict, use_color: bool = True) -> str:
    """confirm / ask 中断渲染。

    * **confirm**（危险工具调用确认）：显示 工具名(参数摘要)，提示语按
      项数自适应 —— 单项 ``y / n / 自由文本``，多项才有"数字"。
    * **ask**（agent 向用户提问）：问题已在 ``question`` 里展示，
      不再重复 dump 工具参数；选项只作参考不强制。
    """
    items = payload.get("items") or []
    kind = payload.get("kind") or "confirm"
    question = payload.get("question") or ""
    out = []
    out.append("")
    title = "信息确认" if kind == "ask" else "操作确认"
    out.append(cyan(f"┌─ {title} ─", use_color))
    out.append("│")
    if question:
        out.append("│ " + yellow(question, use_color))
        out.append("│")
    if kind == "ask":
        # 问题本身就是内容；只把选项作为参考提示
        opts = next((it.get("options") or [] for it in items if it.get("options")), [])
        if opts:
            out.append("│ " + dim(f"可参考：{' / '.join(str(o) for o in opts)}", use_color))
            out.append("│")
    else:
        for i, it in enumerate(items, 1):
            name = it.get("tool_name") or it.get("tool") or it.get("name") or "?"
            args = it.get("args") or {}
            if len(items) == 1:
                out.append(f"│ {bold(name, use_color)}({fmt_args(args)})")
            else:
                out.append(f"│ {yellow(f'[{i}]', use_color)} {bold(name, use_color)}({fmt_args(args)})")
        out.append("│")
        # 提示语自适应：单项用不到"数字"
        if len(items) > 1:
            hint = "输入：y 全部 / n 全部 / 数字（如 1 3 挑选）/ 自由文本"
        else:
            hint = "输入：y / n / 自由文本"
        out.append("│ " + dim(hint, use_color))
    if kind == "ask":
        out.append("│ " + dim("直接输入你的回答", use_color))
    out.append(cyan("└" + "─" * 40, use_color))
    return "\n".join(out)


def render_interrupt(payload: dict, use_color: bool = True) -> str:
    """通用 interrupt 分发。"""
    kind = payload.get("kind") or "ask"
    if kind == "plan_confirm":
        return render_plan_confirm(payload, use_color)
    if kind == "clarify":
        return render_clarify(payload, use_color)
    return render_ask(payload, use_color)


def _option_pairs(options: list[str]) -> Iterable[tuple[str, str]]:
    """``["执行", "调整", "取消"]`` → ``[(1, 执行), (2, 调整), (3, 取消)]``"""
    for i, opt in enumerate(options, 1):
        yield str(i), opt


# ============================================================
# 信息 / 错误块
# ============================================================

def banner(text: str, char: str = "─", color: bool = True) -> str:
    """横线 banner。"""
    width = min(shutil.get_terminal_size((100, 20)).columns, 80)
    bar = char * width
    return _c("36", bar, color) + "\n" + bold(text, color) + "\n" + _c("36", bar, color)


def info(text: str, color: bool = True) -> str:
    return cyan("ℹ ", color) + text


def ok(text: str, color: bool = True) -> str:
    return green("✓ ", color) + text


def warn(text: str, color: bool = True) -> str:
    return yellow("⚠ ", color) + text


def err(text: str, color: bool = True) -> str:
    return red("✘ ", color) + text