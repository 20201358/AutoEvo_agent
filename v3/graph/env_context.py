"""运行环境上下文：给 planner / agent 提供规划前必备的本机信息。

用户反馈的核心问题：planner 连问「什么操作系统？」「启动方式是什么？」「安装路径在哪？」
这类**机器自己就能查到**的问题。此模块在每次规划前收集：

* 操作系统 / shell 类型
* 当前工作目录 + 目录概览（顶层 DIR/FILE 清单，识别 .bat/.cmd/.exe 启动器）
* 关键环境变量（USERNAME / USERPROFILE / COMSPEC / PATH 条数 等）

规划器拿到这些信息后，环境类问题一律不许再问用户。
"""

from __future__ import annotations

import logging
import os
import platform

log = logging.getLogger(__name__)

# 目录概览最多展示的条目数
_MAX_DIR_ENTRIES = 15
# 视为"可执行启动器"的后缀（用户在目录里放这些 = 有可用程序）
_LAUNCHER_SUFFIXES = (".bat", ".cmd", ".ps1", ".exe", ".lnk")
# 值得展示的环境变量（短、稳定、有信息量）
_KEY_ENV_VARS = (
    "USERNAME",
    "USERPROFILE",
    "COMPUTERNAME",
    "COMSPEC",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "TERM",
    "LANG",
)


def _current_cwd() -> str:
    """优先用持久化 shell 的 cwd（agent cd 过的目录），否则用进程 cwd。"""
    try:
        from ..platform_utils import shell_session_status

        st = shell_session_status()
        if st.get("active"):
            return st.get("cwd") or os.getcwd()
    except Exception:
        pass
    return os.getcwd()


def _shell_desc() -> str:
    """当前持久化 shell 描述（类型 + 可执行路径）。"""
    try:
        from ..platform_utils import shell_session_status

        st = shell_session_status()
        if st.get("active"):
            return f"{st.get('kind') or 'unknown'} ({st.get('shell')})"
    except Exception:
        pass
    try:
        from ..platform_utils import default_shell

        return f"(默认) {default_shell()}"
    except Exception:
        return "(未知)"


def _dir_overview(cwd: str, max_entries: int = _MAX_DIR_ENTRIES) -> str:
    """当前目录顶层清单：DIR/FILE 标记，launcher 加注。"""
    try:
        entries = sorted(os.scandir(cwd), key=lambda e: (not e.is_dir(), e.name.lower()))
    except Exception as e:
        return f"(目录读取失败: {type(e).__name__})"
    if not entries:
        return "(空目录)"

    lines = []
    for ent in entries[:max_entries]:
        try:
            if ent.is_dir():
                lines.append(f"DIR  {ent.name}/")
            else:
                name = ent.name
                note = ""
                if name.lower().endswith(_LAUNCHER_SUFFIXES):
                    note = "  <- 可执行/启动器"
                lines.append(f"FILE {name}{note}")
        except OSError:
            continue
    rest = len(entries) - max_entries
    if rest > 0:
        lines.append(f"... (+{rest} 项省略，可用 list_dir 查看)")
    return "\n".join(lines)


def _env_summary() -> str:
    """关键环境变量摘要。"""
    lines = []
    for key in _KEY_ENV_VARS:
        val = os.environ.get(key)
        if val:
            if len(val) > 80:
                val = val[:77] + "..."
            lines.append(f"{key}={val}")
    path = os.environ.get("PATH") or ""
    n_path = len([p for p in path.split(os.pathsep) if p.strip()])
    lines.append(f"PATH=({n_path} 个目录)")
    return "\n".join(lines)


def build_env_context_text() -> str:
    """渲染完整的运行环境块（给 planner 的用户提示词用）。"""
    cwd = _current_cwd()
    try:
        os_name = platform.system() + " " + (platform.release() or "")
        os_name += " / " + platform.machine()
    except Exception:
        os_name = platform.platform()

    parts = [
        f"- 操作系统: {os_name}",
        f"- shell: {_shell_desc()}（run_shell 在此持久会话里执行）",
        f"- 工作目录: {cwd}",
        "目录概览:",
        _dir_overview(cwd),
        "关键环境变量:",
        _env_summary(),
    ]
    return "\n".join(parts)


def env_context_for_planner() -> str:
    """带头的完整块（异常时返回空串，不影响规划）。"""
    try:
        return "### 运行环境（规划前必读；这里能查到的信息不要问用户）\n" + build_env_context_text()
    except Exception as e:
        log.warning("env_context: 收集环境信息失败: %s", e)
        return ""


__all__ = [
    "build_env_context_text",
    "env_context_for_planner",
    "_current_cwd",
    "_dir_overview",
    "_env_summary",
]
