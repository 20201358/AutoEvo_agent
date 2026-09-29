"""跨平台适配层。"""

from .paths import is_subpath, normalize, to_posix
from .persistent_shell import (
    PersistentShell,
    close_session,
    get_session,
    reset_session,
    session_status,
)
from .process import (
    close_shell_session,
    get_persistent_session,
    reset_shell_session,
    run_command,
    run_command_persistent,
    set_session_cwd,
    shell_session_status,
)
from .shell import build_command, default_shell, is_powershell, wrap_encoding
from .tools_detect import detect_tools

__all__ = [
    # shell 语法
    "default_shell",
    "is_powershell",
    "build_command",
    "wrap_encoding",
    # 路径
    "normalize",
    "is_subpath",
    "to_posix",
    # 一次性执行
    "run_command",
    # 持久化执行
    "run_command_persistent",
    "get_persistent_session",
    "reset_shell_session",
    "close_shell_session",
    "shell_session_status",
    "set_session_cwd",
    # 持久化 shell 本体
    "PersistentShell",
    "get_session",
    "reset_session",
    "close_session",
    "session_status",
    # 工具探测
    "detect_tools",
]
