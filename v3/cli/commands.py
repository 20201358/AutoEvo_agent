"""v3 内置命令注册表。

约定：所有命令以 ``v3`` 前缀触发（在 ``router.classify`` 阶段被识别）。
命令名支持短别名（见 ``router.V3_ALIASES``）。

每个 handler 签名：

    handler(session: AgentSession, args: str, *, use_color: bool) -> CommandResult

返回 ``CommandResult``：普通 / REPLACE（新会话替换当前）/ EXIT。
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from typing import Callable, Optional

from ..session import AgentSession, list_conversations, open_session
from ..memory import get_conversation_store
from ..platform_utils import (
    close_shell_session,
    reset_shell_session,
    shell_session_status,
)
from ..skillkit import get_skill_library
from ..tools import get_registered_map
from . import renderer as R
from . import prompt as P
from .router import resolve_v3_alias


# ============================================================
# 结果
# ============================================================

@dataclass
class CommandResult:
    """一个 v3 命令的执行结果。"""

    output: str = ""        # 给用户看的输出
    replace_session: Optional[AgentSession] = None  # 非 None 表示切换会话
    exit_requested: bool = False

    @classmethod
    def text(cls, text: str) -> "CommandResult":
        return cls(output=text)

    @classmethod
    def switch(cls, session: AgentSession, msg: str = "") -> "CommandResult":
        return cls(output=msg, replace_session=session)

    @classmethod
    def quit(cls, msg: str = "") -> "CommandResult":
        return cls(output=msg, exit_requested=True)


# ============================================================
# Handler 签名
# ============================================================

Handler = Callable[[AgentSession, str, bool], CommandResult]


# ============================================================
# v3help
# ============================================================

def cmd_help(_session: AgentSession, _args: str, color: bool) -> CommandResult:
    lines = [
        "",
        R.bold("v3 内置命令（所有命令以 v3 前缀触发，与 shell 命令分隔）：", color),
        "",
        f"  {R.cyan('v3', color)}              不进入会话，显示帮助 + 会话历史",
        f"  {R.cyan('v3list', color)} (v3l)    显示会话历史（创建时间 / session_id / 项目名）",
        f"  {R.cyan('v3once <sid>', color)}    恢复指定会话（支持 4 位短 id）",
        f"  {R.cyan('v3new [title]', color)}   新建会话（可选标题）",
        f"  {R.cyan('v3rename <title>', color)} 重命名当前会话",
        f"  {R.cyan('v3clear', color)} (v3clr) 清空当前会话的消息与图状态",
        f"  {R.cyan('v3tools [cat]', color)}   列出已注册工具（可按类别过滤）",
        f"  {R.cyan('v3shell [reset|close]', color)} 查看/重置持久化 shell",
        f"  {R.cyan('v3config', color)}        显示当前配置",
        f"  {R.cyan('v3conv', color)} (v3hist) 查看当前会话历史消息",
        f"  {R.cyan('v3skills', color)} (v3sk) 列出技能库",
        f"  {R.cyan('v3proposals', color)} (v3p) 查看上一轮的技能建议",
        f"  {R.cyan('v3state', color)}        显示当前图状态（计划/任务ID/迭代次数）",
        f"  {R.cyan('v3exit', color)} (v3quit) 退出",
        "",
        R.dim("非 v3 前缀输入：", color),
        f"  · 看起来像 shell 命令（白名单首词或含元字符）→ 直接执行并显示输出",
        f"  · 否则 → 创建新会话 + 作为第一条输入发给 agent（或继续当前会话）",
        "",
        R.dim("会话恢复时状态完全保留（langgraph checkpointer）。", color),
    ]
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3list / v3ls / v3l
# ============================================================

def cmd_list(_session: AgentSession, _args: str, color: bool) -> CommandResult:
    convs = list_conversations(limit=20)
    if not convs:
        return CommandResult.text(R.warn("还没有任何会话。输入自然语言即可创建新会话。", color))
    lines = [
        "",
        R.bold(f"会话历史（共 {len(convs)} 条）：", color),
        "",
    ]
    lines.append(f"  {R.dim('创建时间', color):<22}  {R.dim('sid', color):<6}  {R.dim('项目名', color)}")
    lines.append(f"  {'─'*22}  {'─'*6}  {'─'*30}")
    for c in convs:
        sid = P.short_id(c.id)
        # 标题：没标题就用第一条用户输入
        title = c.title or _first_input_as_title(c.id)
        if not title:
            title = "(无内容)"
        if len(title) > 40:
            title = title[:38] + ".."
        created = c.created_at[:19].replace("T", " ")
        lines.append(f"  {created:<22}  {R.cyan(sid, color):<6}  {title}")
    lines.append("")
    lines.append(R.dim("  恢复会话：v3once <sid>", color))
    return CommandResult.text("\n".join(lines))


def _first_input_as_title(conv_id: str) -> str:
    """没有显式标题时，用第一条用户输入（前 30 字）。"""
    try:
        store = get_conversation_store()
        msgs = store.get_messages(conv_id, limit=10, role="user")
        if msgs:
            text = (msgs[0].content or "").strip().splitlines()[0]
            return text[:30]
    except Exception:
        pass
    return ""


# ============================================================
# v3once / v3o / v3open
# ============================================================

def cmd_once(_session: AgentSession, args: str, color: bool) -> CommandResult:
    sid = (args or "").strip()
    if not sid:
        return CommandResult.text(R.warn("用法：v3once <session_id>（支持 4 位短 id）", color))

    full_id = _resolve_short_id(sid)
    if not full_id:
        return CommandResult.text(R.err(f"找不到会话：{sid}。v3list 查看。", color))
    if len(sid) < 32 and full_id[: len(sid)].lower() != sid.lower():
        return CommandResult.text(R.err(f"短 id '{sid}' 不唯一。请用更长的前缀。", color))

    try:
        new = open_session(conversation_id=full_id)
    except Exception as e:
        return CommandResult.text(R.err(f"恢复失败：{e}", color))

    title = new.conversation.title if new.conversation else ""
    msg = R.ok(
        f"已恢复会话 [{P.short_id(full_id)}]"
        + (f" {title}" if title else ""),
        color,
    )
    return CommandResult.switch(new, msg)


def _resolve_short_id(short: str) -> Optional[str]:
    """把短 id 解析成完整 uuid。

    支持：

    * 完整 32 字符 hex → 原样返回
    * 短前缀（前 N 位）→ 唯一匹配返回；多个匹配返回最早一条；0 匹配返回 None
    """
    if not short:
        return None
    short_l = short.lower()
    if len(short_l) >= 32:
        return short_l if _exists(short_l) else None

    try:
        store = get_conversation_store()
        convs = store.list(limit=500)
    except Exception:
        return None

    matches = [c for c in convs if c.id.lower().startswith(short_l)]
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        # 多个匹配：返回最早创建的
        matches.sort(key=lambda c: c.created_at)
        return matches[0].id
    return None


def _exists(conv_id: str) -> bool:
    try:
        store = get_conversation_store()
        return store.get(conv_id) is not None
    except Exception:
        return False


# ============================================================
# v3new
# ============================================================

def cmd_new(_session: AgentSession, args: str, color: bool) -> CommandResult:
    title = args.strip()
    new = AgentSession.new(title=title)
    sid = P.short_id(new.conversation_id)
    msg = R.ok(
        f"已新建会话 [{sid}]" + (f" {title}" if title else ""),
        color,
    )
    return CommandResult.switch(new, msg)


# ============================================================
# v3rename
# ============================================================

def cmd_rename(session: AgentSession, args: str, color: bool) -> CommandResult:
    title = args.strip()
    if not title:
        return CommandResult.text(R.warn("用法：v3rename <新标题>", color))
    session.rename(title)
    return CommandResult.text(R.ok(f"已重命名为：{title}", color))


# ============================================================
# v3clear
# ============================================================

def cmd_clear(session: AgentSession, _args: str, color: bool) -> CommandResult:
    session.clear()
    return CommandResult.text(R.ok("已清空当前会话的消息与图状态", color))


# ============================================================
# v3tools
# ============================================================

def cmd_tools(_session: AgentSession, args: str, color: bool) -> CommandResult:
    cat = args.strip() or None
    reg = get_registered_map()
    items = sorted(reg.values(), key=lambda r: (r.category, r.name))
    if cat:
        items = [r for r in items if r.category == cat]
        if not items:
            return CommandResult.text(R.warn(f"没有分类为 {cat!r} 的工具", color))

    lines = ["", R.bold(f"共 {len(items)} 个工具：", color), ""]
    last_cat = None
    for r in items:
        if r.category != last_cat:
            lines.append(f"  {R.magenta('[' + r.category + ']', color)}")
            last_cat = r.category
        tag = ""
        if r.risk == "high":
            tag = R.red(" ⚠高风险", color)
        elif r.risk == "medium":
            tag = R.yellow(" ⚠中风险", color)
        if r.requires_confirm:
            tag += R.yellow("(需确认)", color)
        desc = (r.description or "").splitlines()[0][:80]
        lines.append(f"    - {R.bold(r.name, color)}{tag}: {desc}")
    lines.append("")
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3shell
# ============================================================

def cmd_shell(_session: AgentSession, args: str, color: bool) -> CommandResult:
    sub = args.strip().lower()
    if sub == "reset":
        reset_shell_session()
        return CommandResult.text(R.ok("已重置持久化 shell", color))
    if sub == "close":
        close_shell_session()
        return CommandResult.text(R.ok("已关闭持久化 shell", color))
    st = shell_session_status()
    if not st.get("active"):
        return CommandResult.text(R.warn("持久化 shell 未启动", color))
    lines = [R.bold("持久化 shell 状态：", color)]
    for k in ("shell", "kind", "alive", "cmd_count", "cwd", "started_at"):
        v = st.get(k)
        if v is not None:
            lines.append(f"  {k}: {v}")
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3config
# ============================================================

def cmd_config(_session: AgentSession, _args: str, color: bool) -> CommandResult:
    from ..config import settings
    lines = [R.bold("当前配置：", color), R.dim(settings.describe(), color), ""]
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3conv / v3history
# ============================================================

def cmd_conv(session: AgentSession, _args: str, color: bool) -> CommandResult:
    msgs = session.history(limit=40)
    if not msgs:
        return CommandResult.text(R.warn("(本会话暂无历史消息)", color))
    lines = [R.bold(f"历史 {len(msgs)} 条：", color), ""]
    for m in msgs:
        role = m.role or "?"
        text = (m.content or "").strip().splitlines()[0][:120]
        lines.append(f"  {R.bold(role, color):<10} {R.dim(m.timestamp or '', color)}")
        lines.append(f"  {text}")
        lines.append("")
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3skills
# ============================================================

def cmd_skills(_session: AgentSession, _args: str, color: bool) -> CommandResult:
    lib = get_skill_library()
    skills = lib.list()
    if not skills:
        lines = [R.warn("(技能库为空)", color)]
    else:
        lines = [R.bold(f"共 {len(skills)} 个技能：", color), ""]
        for s in skills:
            desc = " ".join((s.description or "").split())[:80]
            lines.append(f"  - {R.bold(s.name, color)} ({s.line_count} 行): {desc}")
    archived = lib.archived()
    if archived:
        lines.append("")
        lines.append(R.dim(f"已归档 {len(archived)} 个：{', '.join(archived[:10])}", color))
    lines.append("")
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3proposals
# ============================================================

def cmd_proposals(session: AgentSession, _args: str, color: bool) -> CommandResult:
    state = session.get_state()
    proposals = state.get("skill_proposals") or []
    if not proposals:
        return CommandResult.text(R.warn("(当前没有挂起的技能建议)", color))
    lines = [R.bold(f"技能管理建议 {len(proposals)} 条：", color), ""]
    for i, p in enumerate(proposals, 1):
        action = p.get("action", "?")
        skills = ", ".join(p.get("skills") or [])
        reason = p.get("reason", "")
        lines.append(f"  {i}. {R.magenta(action, color)} → {skills}")
        if reason:
            lines.append(f"     {R.dim(reason, color)}")
    lines.append("")
    lines.append(R.dim("想落实？说「执行第 1 条」或描述要做哪几条；「跳过」忽略。", color))
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3state
# ============================================================

def cmd_state(session: AgentSession, _args: str, color: bool) -> CommandResult:
    state = session.get_state()
    if not state:
        return CommandResult.text(R.warn("(无图状态)", color))
    lines = [R.bold("当前图状态：", color)]
    for k in (
        "task_id", "task_mode", "user_intent", "iteration",
        "is_done", "task_success", "end_reason", "pending_confirmation",
    ):
        v = state.get(k)
        if v is not None and v != "":
            v_repr = repr(v)
            if len(v_repr) > 100:
                v_repr = v_repr[:60] + "…"
            lines.append(f"  {k}: {v_repr}")
    plan = state.get("plan") or []
    if plan:
        lines.append(f"  plan ({len(plan)}):")
        for p in plan:
            lines.append(
                f"    {R.status_mark(p.get('status') or '?', color)} "
                f"{(p.get('description') or '')}"
            )
    return CommandResult.text("\n".join(lines))


# ============================================================
# v3exit
# ============================================================

def cmd_exit(_session: AgentSession, _args: str, color: bool) -> CommandResult:
    close_shell_session()
    return CommandResult.quit(R.ok("已退出。", color))


# ============================================================
# 注册表
# ============================================================

HANDLERS: dict[str, Handler] = {
    "help":      cmd_help,
    "list":      cmd_list,
    "once":      cmd_once,
    "new":       cmd_new,
    "rename":    cmd_rename,
    "clear":     cmd_clear,
    "tools":     cmd_tools,
    "shell":     cmd_shell,
    "config":    cmd_config,
    "conv":      cmd_conv,
    "skills":    cmd_skills,
    "proposals": cmd_proposals,
    "state":     cmd_state,
    "exit":      cmd_exit,
}


def dispatch(
    session: AgentSession,
    v3_cmd: str,
    args: str,
    use_color: bool,
) -> CommandResult:
    """分发 v3 命令。未知命令 → 提示用 v3help。"""
    canonical = resolve_v3_alias(v3_cmd)
    if not canonical:
        return CommandResult.text(
            R.warn(f"未知 v3 命令：{v3_cmd!r}。输入 v3help 查看。", use_color)
        )
    handler = HANDLERS.get(canonical)
    if not handler:
        return CommandResult.text(R.err(f"命令 {canonical!r} 尚未实现", use_color))
    return handler(session, args, use_color)