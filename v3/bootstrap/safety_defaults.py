"""启动时默认安全策略。

bootstrap_safety() 根据平台生成一组默认策略。
用户可在运行时通过修改 state["safety"] 覆盖，或传入自定义策略。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


# ============================================================
# 平台相关的危险命令黑名单
# ============================================================

WINDOWS_DENIED_PATTERNS: list[str] = [
    r"\bformat\s+[a-zA-Z]:",                                # format C:
    r"\bdiskpart\b",                                        # 磁盘分区工具
    r"\bdel\s+/[fFsSqQ].*\s+[a-zA-Z]:\\?\s*$",              # del /f /s /q C:\
    r"\brd\s+/s\s+/q\s+[a-zA-Z]:\\?\s*$",                   # rd /s /q C:\
    r"\brmdir\s+/s\s+/q\s+[a-zA-Z]:\\?\s*$",
    r"\bRemove-Item\s+.*-Recurse.*-Force.*[A-Z]:\\?\s*$",   # PowerShell 递归删盘
    r"\bicacls\s+[A-Z]:\\?\s+/grant\s+Everyone:F",          # 权限全开
    r"\breg\s+delete\s+HKLM",                               # 删注册表
    r"\breg\s+delete\s+HKCR",
    r"\bshutdown\s+/[sr]\b",                                # 关机/重启
    r"\bbcdedit\b",                                         # 改启动配置
    r"\bvssadmin\s+delete\s+shadows",                       # 删卷影
    r"\bStop-Computer\b",                                   # PowerShell 关机
    r"\bRestart-Computer\b",
    r"\bSet-ExecutionPolicy\s+Unrestricted",                # 放开脚本执行策略
    r"\bnet\s+user\s+\S+\s+/add",                           # 加用户
    r"\bnet\s+localgroup\s+Administrators\s+\S+\s+/add",    # 加管理员
]

UNIX_DENIED_PATTERNS: list[str] = [
    r"rm\s+-rf\s+/(?:\s|$)",                                # rm -rf /
    r"rm\s+-rf\s+/\*",                                      # rm -rf /*
    r"rm\s+-rf\s+~(?:\s|$)",
    r":\(\)\s*\{.*\};\s*:",                                 # fork bomb
    r"dd\s+if=.*of=/dev/(sd|nvme|hd)",                      # 写裸盘
    r">\s*/dev/(sd|nvme|hd)",
    r"\bmkfs\.",                                            # 格式化
    r"\bchmod\s+-R\s+777\s+/(?:\s|$)",                      # 权限全开
    r"\bchown\s+-R\s+\S+\s+/(?:\s|$)",
    r"\bmv\s+/\s+/dev/null",
    r"\bshutdown\s+-[hr]\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\binit\s+0\b",
    r"\biptables\s+-F",                                     # 清空防火墙
    r"\bufw\s+disable",
]


# ============================================================
# 平台相关的提权关键词
# ============================================================

WINDOWS_CONFIRM_TOKENS: tuple[str, ...] = (
    "runas",
    "Start-Process -Verb RunAs",
)

UNIX_CONFIRM_TOKENS: tuple[str, ...] = (
    "sudo",
    "su -",
    "doas",
)


# ============================================================
# 对外接口
# ============================================================

def bootstrap_safety(
    *,
    mode: str = "normal",
    allow_sudo: bool = False,
    allow_network: bool = True,
    allow_write: bool = True,
    extra_allowed_paths: list[str] | None = None,
    extra_denied_patterns: list[str] | None = None,
) -> dict:
    """生成平台相关的默认安全策略。

    参数：
        mode: "strict" | "normal" | "yolo"
        allow_sudo: 是否允许提权（sudo / runas）
        allow_network: 是否允许网络操作
        allow_write: 是否允许写操作
        extra_allowed_paths: 追加到白名单的路径
        extra_denied_patterns: 追加到黑名单的正则

    返回：
        填充好的 SafetyPolicy 字典。
    """
    is_windows = sys.platform == "win32"

    # 两套黑名单同时生效：Windows 上 agent 也可能通过 Git Bash 执行 POSIX 命令，
    # 多一层校验只会多问一次，不会漏放危险操作。
    denied = list(WINDOWS_DENIED_PATTERNS) + list(UNIX_DENIED_PATTERNS)
    if extra_denied_patterns:
        denied.extend(extra_denied_patterns)

    confirm_tokens = list(
        set(WINDOWS_CONFIRM_TOKENS) | set(UNIX_CONFIRM_TOKENS)
        if is_windows
        else set(UNIX_CONFIRM_TOKENS)
    )

    allowed_paths = _default_allowed_paths(is_windows)
    if extra_allowed_paths:
        allowed_paths.extend(extra_allowed_paths)

    # strict 模式下收紧策略
    if mode == "strict":
        allow_sudo = False
        allow_network = False
        allow_write = False

    # yolo 模式下放开（但仍保留黑名单，防止误操作）
    elif mode == "yolo":
        allow_sudo = True
        allow_write = True

    return {
        "mode": mode,
        "allow_sudo": allow_sudo,
        "allow_network": allow_network,
        "allow_write": allow_write,
        "allowed_paths": allowed_paths,
        "denied_patterns": denied,
        "require_confirmation": confirm_tokens,
        "violations": [],
    }


# ============================================================
# 辅助
# ============================================================

def _default_allowed_paths(is_windows: bool) -> list[str]:
    """默认写操作白名单：当前工作目录 + 临时目录。"""
    paths = [os.getcwd()]

    if is_windows:
        temp = os.environ.get("TEMP") or os.environ.get("TMP")
        if temp:
            paths.append(temp)
    else:
        paths.append("/tmp")
        paths.append(tempfile.gettempdir())

    # 去重，保持顺序
    seen = set()
    result = []
    for p in paths:
        normalized = str(Path(p).resolve())
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


__all__ = [
    "bootstrap_safety",
    "WINDOWS_DENIED_PATTERNS",
    "UNIX_DENIED_PATTERNS",
    "WINDOWS_CONFIRM_TOKENS",
    "UNIX_CONFIRM_TOKENS",
]