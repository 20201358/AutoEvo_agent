"""v3 命令行交互入口。

设计（2026-09-22 重写）：

* 不显示 graph 节点名，只显示结果。
* 混合 shell + agent REPL：

    - ``v3xxx`` 前缀 → v3 内置命令（与 shell 命令分隔，类似 mysql）
    - 看起来像 shell 命令（白名单首词或含元字符）→ 直接执行持久化 shell
    - 其它非空输入 → 创建新会话（或继续当前会话）+ agent run

* 提示符：``[<4 位 sid> <标题>] <cwd>> ``
* Agent 输出前缀：``[<4 位 sid> <标题>] v3 > ``
* 计划：每个步骤对齐 + 状态标记；步骤执行完显示对应终端输出片段
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ..bootstrap import bootstrap_all
from ..config import settings
from ..platform_utils import (
    close_shell_session,
    get_persistent_session,
    shell_session_status,
)
from ..session import AgentSession, TurnResult, list_conversations, open_session
from . import commands as v3cmd
from . import prompt as P
from . import renderer as R
from . import router

log = logging.getLogger("v3.cli")


# ============================================================
# ANSI 颜色开关
# ============================================================

def _detect_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Windows Terminal / modern terminal 支持 ANSI
        return "WT_PROFILE_ID" in os.environ or "TERM" in os.environ
    return True


_USE_COLOR = _detect_color()


def set_color(enabled: bool) -> None:
    global _USE_COLOR
    _USE_COLOR = enabled


# 颜色便捷别名（renderer 里都是函数，这里复用以少改）
bold = R.bold
dim = R.dim
red = R.red
green = R.green
yellow = R.yellow
cyan = R.cyan
magenta = R.magenta
status_mark = R.status_mark


# ============================================================
# 介绍（保持静默：启动直接进提示符，不打印 banner）
# ============================================================

def show_intro(session: Optional[AgentSession]) -> None:
    """启动提示：静默（用户要求去掉 banner / logo / 说明文字）。"""
    return


# ============================================================
# 单轮 / 一轮交互
# ============================================================

def _run_turn(session: AgentSession, text: str) -> TurnResult:
    """发送一轮输入并渲染流式事件。

    统一走 ``session.stream``：有挂起中断时它内部会自动转成 resume，
    且保持事件流（消息 / 计划步骤 / 新中断都能实时渲染）。

    返回 TurnResult 后还会检查 ``switch_to_task``：当 plan_confirm 判定用户
    想切换到新任务时，会把新任务内容透传出来；本函数就在这里自动开新会话
    并跑这个新任务，然后再次递归调用本函数（这样用户仍能继续对话）。
    """
    result: Optional[TurnResult] = None
    try:
        for ev in session.stream(text):
            _on_event(ev, session)
            if ev.get("type") == "result":
                result = ev["result"]
    except Exception as e:
        log.exception("流式执行失败")
        print(red(f"\n✘ 流式执行失败：{type(e).__name__}: {e}", _USE_COLOR))
        return None
    if result is None:
        print(red("(无返回值)", _USE_COLOR))
        return None
    _render_final(result, session)

    # ---- 任务切换 ----
    # plan_confirm 把本轮判定为 "切到新任务" 时，会通过 state._switch_to_task
    # 把新任务内容带回来。这里自动开新会话跑这个新任务，并保持续接模式。
    if result.task_switched and result.switch_to_task:
        new_text = result.switch_to_task
        new_session = AgentSession.new(title=new_text[:30])
        print()
        print(cyan(
            f"切换任务 → 新建会话 [v3_{P.short_id(new_session.conversation_id)}]: {new_text}",
            _USE_COLOR,
        ))
        # 递归：跑新任务并把结果返回
        return _run_turn(new_session, new_text)
    return result


def _on_event(ev: dict, session: AgentSession) -> None:
    """处理一条流式事件。

    不打印 graph 节点名（满足"不用显示 graph 节点的运行"）。
    """
    typ = ev.get("type")

    if typ == "node":
        # 故意吞掉 node 事件 —— 用户不关心内部节点
        return

    if typ == "message":
        msg = ev.get("message")
        if isinstance(msg, AIMessage):
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            if text.strip():
                # Agent 文字输出
                prefix = P.agent_prefix(session.conversation_id, _title_of(session))
                print()
                print(R.render_agent_text(text, prefix, _USE_COLOR))
            # 工具调用：简洁的箭头展示（参数摘要 + finish_task 特殊渲染）
            for tc in (msg.tool_calls or []):
                name = tc.get("name") or "?"
                args = tc.get("args") or {}
                print(dim(f"    → {R.render_tool_call(name, args, _USE_COLOR)}", _USE_COLOR))
        elif isinstance(msg, ToolMessage):
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            # 错误/成功标记
            is_err = (
                text.startswith("⛔")
                or "失败" in text[:80]
                or "异常" in text[:80]
                or "错误" in text[:80]
                or "✘" in text[:80]
            )
            mark = red("← ✘", _USE_COLOR) if is_err else green("← ✓", _USE_COLOR)
            # 缩进显示输出片段
            snippet = (text or "").strip()
            snippet = R.trim_output(snippet, max_lines=4, max_width=120)
            print(f"  {mark} {R.indent(snippet, '    ').lstrip() if snippet else '(空)'}")
        elif isinstance(msg, HumanMessage):
            return
        return

    if typ == "step":
        # Agent 的中间步骤说明（极轻量提示）
        text = ev.get("text") or ""
        if text:
            print(dim(f"  … {text[:200]}", _USE_COLOR))
        return

    if typ == "plan_step":
        step_id = ev.get("step_id") or ev.get("index") or "?"
        description = ev.get("description") or ""
        status = ev.get("status") or "?"
        output = ev.get("output")  # 可选：这一步的工具输出片段
        rendered = R.render_plan_step(step_id, description, status, output, _USE_COLOR)
        print(rendered)
        return

    if typ == "interrupt":
        print(R.render_interrupt(ev.get("payload") or {}, _USE_COLOR))
        return

    if typ == "error":
        print(red(f"\n  ✘ {ev.get('error')}", _USE_COLOR))
        return


def _render_final(r: TurnResult, session: AgentSession) -> None:
    """一轮结束时的总结（已在 _on_event 里实时打印，最后再给个总结 banner）。"""
    if r.error:
        print(red(f"\n✘ 出错：{r.error}", _USE_COLOR))
        return
    # final_text 已在 agent AIMessage 阶段打印过，不再重复
    if r.end_reason:
        tag = {
            "completed": green("[完成]", _USE_COLOR),
            "user_cancelled": yellow("[取消]", _USE_COLOR),
            "incomplete": yellow("[未完成]", _USE_COLOR),
            "error": red("[出错]", _USE_COLOR),
        }.get(r.end_reason, dim(f"[{r.end_reason}]", _USE_COLOR))
        print(tag)
    if r.has_proposals:
        print(magenta(f"\n  💡 {len(r.proposals)} 条技能管理建议，请用 v3proposals 查看", _USE_COLOR))


def _title_of(session: AgentSession) -> str:
    return session.conversation.title if session.conversation else ""


# ============================================================
# 直接执行 shell（不经 agent）
# ============================================================

def _run_shell_direct(cmd: str) -> None:
    """通过持久化 shell 执行一条 shell 命令，把 stdout/stderr 打印到屏幕。

    执行完后静默跑一条 ``pwd`` 同步 shell 进程的 cwd 到 session 状态，
    让 prompt 里的 cwd 显示与 shell 一致。
    """
    sess = get_persistent_session()
    if sess is None:
        print(red("✘ 持久化 shell 未启动，无法直跑命令。", _USE_COLOR))
        return
    # 裸 exit / logout 会杀掉持久 shell 子进程 → 会话假死到超时重建
    first_word = raw.split()[0].lower()
    if first_word in ("exit", "logout"):
        print(yellow(
            "已忽略：exit 会终止持久化 shell 会话。退出 REPL 请用 v3exit。",
            _USE_COLOR,
        ))
        return
    print(dim(f"$ {cmd}", _USE_COLOR))
    try:
        result = sess.execute(cmd, timeout=30)
    except Exception as e:
        print(red(f"✘ 执行失败：{e}", _USE_COLOR))
        return
    out = (result.get("stdout") or "").rstrip()
    err = (result.get("stderr") or "").rstrip()
    rc = result.get("exit_code", 0)
    if out:
        print(out)
    if err:
        print(red(err, _USE_COLOR))
    if rc not in (0, None):
        print(yellow(f"  (exit={rc})", _USE_COLOR))

    # 同步 cwd：PowerShell 用 Get-Location，bash / cmd 用 pwd
    try:
        kind = (sess.kind or "").lower()
        if "powershell" in kind or "pwsh" in kind:
            cwd_result = sess.execute("(Get-Location).Path", timeout=5)
        else:
            cwd_result = sess.execute("pwd", timeout=5)
        new_cwd = (cwd_result.get("stdout") or "").strip().splitlines()
        if new_cwd:
            sess.cwd = new_cwd[0].strip()
    except Exception:
        pass


# ============================================================
# cwd 显示：优先用持久化 shell 的 cwd（与 cd 后保持一致）
    # 切了 cwd 后更新提示符（用 P.cwd() 读最新值）


# ============================================================
# REPL 主循环
# ============================================================

def run_repl(initial: Optional[AgentSession] = None) -> int:
    """REPL 主循环。

    两种模式：

    * 默认模式（``session is None`` 或 ``continuation_mode == False``）
      —— 每个非 v3 / 非 shell 输入 = 新建会话跑一次
    * 续接模式（``v3once <sid>`` 或 ``v3new`` 之后）
      —— 自然语言 = 当前会话的 follow-up

    通过 ``v3`` 单独敲（无参数）回到默认模式。
    """
    settings.ensure_dirs()
    bootstrap_all()
    setup_logging()

    # initial 来自 --new / --open / 默认 open_session：进入续接模式
    session = initial
    continuation_mode = initial is not None

    show_intro(session if continuation_mode else None)

    while True:
        try:
            prompt_str = _build_prompt(session, continuation_mode)
            text = input(prompt_str)
        except (EOFError, KeyboardInterrupt):
            print(green("\n\n退出。", _USE_COLOR))
            close_shell_session()
            return 0
        raw = text.strip()
        if not raw:
            continue

        route = router.classify(raw)

        # ---- 挂起中断优先：除 v3 命令外，任何输入都作为对该中断的回复 ----
        # （否则用户敲 shell 命令 / exit 会绕过确认，甚至杀掉持久 shell 造成假死）
        if (
            session is not None
            and continuation_mode
            and not route.is_v3
            and getattr(session, "pending_interrupt", None) is not None
        ):
            try:
                _run_turn(session, raw)
            except Exception as e:
                log.exception("对话执行失败")
                print(red(f"✘ 对话执行失败：{e}", _USE_COLOR))
            continue

        # ---- v3 内置命令 ----
        if route.is_v3:
            cmd_name = route.v3_cmd
            args = route.v3_args

            # 仅 "v3" 一个词：显示帮助 + 列表，回到默认模式
            if not cmd_name:
                _print(v3cmd.cmd_help(_resolve_for_meta(session), "", _USE_COLOR).output)
                _print(v3cmd.cmd_list(_resolve_for_meta(session), "", _USE_COLOR).output)
                session = None
                continuation_mode = False
                continue

            canonical = router.resolve_v3_alias(cmd_name)

            # 在默认模式（非续接）下：v3once / v3new 可以进入续接
            if not continuation_mode and canonical in ("once", "new"):
                res = v3cmd.dispatch(_resolve_for_meta(session), cmd_name, args, _USE_COLOR)
                _print(res.output)
                if res.exit_requested:
                    return 0
                if res.replace_session is not None:
                    session = res.replace_session
                    continuation_mode = True
                continue

            # v3exit 总是可用
            if canonical == "exit":
                print(green("已退出。", _USE_COLOR))
                close_shell_session()
                return 0

            # 其它 v3 命令：必须有 session，否则临时打开最近一个
            target = session if session is not None else open_session()
            res = v3cmd.dispatch(target, cmd_name, args, _USE_COLOR)
            _print(res.output)
            if res.exit_requested:
                return 0
            if res.replace_session is not None:
                session = res.replace_session
                continuation_mode = True
            continue

        # ---- 直接 shell ----
        if route.is_shell:
            _run_shell_direct(raw)
            continue

        # ---- 自然语言 ----
        if continuation_mode and session is not None:
            try:
                _run_turn(session, raw)
            except Exception as e:
                log.exception("对话执行失败")
                print(red(f"✘ 对话执行失败：{e}", _USE_COLOR))
            continue

        # 默认模式：新建会话跑这一轮（跑完后 session 留着以便 v3state / v3conv 查）
        temp = AgentSession.new(title=raw[:30])
        print(green(f"新建会话 [v3_{P.short_id(temp.conversation_id)}]", _USE_COLOR))
        try:
            result = _run_turn(temp, raw)
        except Exception as e:
            log.exception("对话执行失败")
            print(red(f"✘ 对话执行失败：{e}", _USE_COLOR))
            session = temp
            continuation_mode = False
            continue
        session = temp
        # 如果有挂起中断（plan_confirm / clarify / ask），进入续接模式让用户回复
        continuation_mode = result is not None and bool(result.interrupt)


def _print(text: str) -> None:
    if text:
        print(text)


def _build_prompt(session: Optional[AgentSession], continuation_mode: bool) -> str:
    if continuation_mode and session is not None:
        return P.input_prompt(session.conversation_id, _title_of(session))
    return P.no_session_prompt()


def _resolve_for_meta(session: Optional[AgentSession]) -> AgentSession:
    """给 v3cmd.dispatch 用：拿一个 session 对象来跑 state / conv / rename / clear。

    如果当前没有 session，临时打开最近那个（不进入续接模式）。
    """
    if session is not None:
        return session
    return open_session()


def setup_logging() -> None:
    from ..config import setup_logging as _setup
    _setup(level="WARNING")


def run_once(text: str) -> int:
    """单轮模式：跑一轮就退出（用于脚本 / CI）。"""
    settings.ensure_dirs()
    bootstrap_all()
    setup_logging()
    session = open_session()
    show_intro(session)
    result = _run_turn(session, text)
    if result is None:
        return 1
    if result.end_reason == "error":
        return 1
    return 0


# ============================================================
# argparse 入口
# ============================================================

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v3.bat",
        description="v3 agent 交互 CLI（混合 shell / agent）",
    )
    parser.add_argument("--new", metavar="TITLE", help="新建会话（可带标题）")
    parser.add_argument("--open", metavar="CONV_ID", help="打开指定会话（支持 4 位短 id）")
    parser.add_argument("--list", action="store_true", help="列出最近会话并退出")
    parser.add_argument("--once", metavar="TEXT", help="单轮模式：跑一次就退出")
    parser.add_argument("--no-color", action="store_true", help="禁用颜色")
    args = parser.parse_args(argv)

    if args.no_color:
        set_color(False)

    if args.list:
        settings.ensure_dirs()
        bootstrap_all()
        setup_logging()
        # 直接调 v3cmd.cmd_list 让格式一致
        print(v3cmd.cmd_list(open_session(), "", _USE_COLOR).output)
        return 0

    if args.new is not None:
        sess = AgentSession.new(title=args.new)
        return run_repl(sess)
    if args.open:
        sess = open_session(conversation_id=args.open)
        return run_repl(sess)
    # 默认：不进入任何会话，让用户自己敲 v3 / v3list / 自然语言
    return run_repl(None)