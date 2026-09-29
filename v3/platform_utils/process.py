"""进程执行。

两种模式：

- ``run_command``            每次调用 fork 一个新子进程。无状态、适合一次性探测。
- ``run_command_persistent`` 复用同一个持久 shell 子进程。保留 cwd / 环境变量 /
                              shell 变量等状态，跨调用连续。

``run_command_persistent`` 是 ``tools.shell_tool`` 的默认入口。
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .shell import build_command, wrap_encoding
from .persistent_shell import PersistentShell, get_session as _get_session


# ============================================================
# 一次性执行（每次新建进程）
# ============================================================

def run_command(
    cmd: str,
    cwd: str | None = None,
    shell_path: str | None = None,
    timeout: int = 120,
    max_output: int = 4000,
) -> dict:
    """每次调用启动一个新子进程执行 ``cmd``。"""
    cmd = wrap_encoding(cmd, shell_path)
    argv = build_command(cmd, shell_path)
    is_windows = sys.platform == "win32"

    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    try:
        proc = subprocess.run(
            argv,
            shell=isinstance(argv, str),
            capture_output=True,
            cwd=cwd,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        exit_code = proc.returncode
        stdout = (proc.stdout or "")[-max_output:]
        stderr = (proc.stderr or "")[-max_output:]
    except subprocess.TimeoutExpired as e:
        if is_windows and getattr(e, "pid", None):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(e.pid)],
                capture_output=True,
            )
        exit_code, stdout, stderr = -1, "", f"命令超时（>{timeout}s）"
    except FileNotFoundError as e:
        exit_code, stdout, stderr = -2, "", f"命令未找到: {e}"
    except Exception as e:
        exit_code, stdout, stderr = -3, "", f"执行异常: {type(e).__name__}: {e}"

    duration = int((time.perf_counter() - t0) * 1000)

    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started.isoformat(),
        "duration_ms": duration,
    }


# ============================================================
# 持久化执行（复用同一 shell 子进程）
# ============================================================

def get_persistent_session(
    shell_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> PersistentShell:
    """获取持久化会话（存在则复用，不存在则创建）。

    注意：``shell_path`` / ``cwd`` 仅在首次创建会话时生效。
    """
    return _get_session(shell_path=shell_path, cwd=cwd)


def run_command_persistent(
    cmd: str,
    cwd: str | None = None,
    timeout: int = 120,
    max_output: int = 4000,
    shell_path: str | None = None,
    env: dict | None = None,
) -> dict:
    """在持久化 shell 中执行 ``cmd``。

    Args:
        cmd: 要执行的命令。
        cwd: 若与当前会话 cwd 不同，先切换到该目录。
        timeout: 超时秒数；超时后强制重建 shell。
        max_output: 输出截断上限（字符）。
        shell_path: 首次创建会话时使用的 shell。
        env: 首次创建会话时使用的环境变量。

    Returns:
        与 ``run_command`` 字段一致的 dict。
    """
    session = _get_session(shell_path=shell_path, cwd=cwd)

    # cwd 与会话当前目录不一致时先切换
    if cwd:
        try:
            session_cwd = session.cwd
        except Exception:
            session_cwd = None
        if session_cwd and str(session_cwd) != str(cwd):
            cd_result = _change_dir(session, cwd)
            if cd_result.get("exit_code", 0) == 0:
                try:
                    session.cwd = cwd
                except Exception:
                    pass
            else:
                msg = cd_result.get("stderr") or cd_result.get("stdout") or ""
                if session.kind in ("bash", "sh"):
                    cmd = f"# warning: 切换到 {cwd} 失败: {msg}\n{cmd}"
                else:
                    cmd = f"# warning: 切换到 {cwd} 失败: {msg}\n{cmd}"

    return session.execute(cmd=cmd, timeout=timeout, max_output=max_output)


def _change_dir(session: PersistentShell, cwd: str) -> dict:
    """在会话中切换目录，按 shell 类型生成合适语法。"""
    target = str(Path(cwd))
    if session.kind == "powershell":
        cmd = f"Set-Location -LiteralPath '{target}'"
    elif session.kind == "cmd":
        cmd = f'cd /d "{target}"'
    else:
        escaped = target.replace("'", "'\\''")
        cmd = f"cd '{escaped}'"
    return session.execute(cmd, timeout=15, max_output=2000)


def reset_shell_session() -> None:
    """显式重置持久化 shell 会话。"""
    from .persistent_shell import reset_session
    reset_session()


def close_shell_session() -> None:
    """显式关闭持久化 shell 会话（释放子进程）。"""
    from .persistent_shell import close_session
    close_session()


def shell_session_status() -> dict:
    """返回当前会话的简要状态信息。"""
    from .persistent_shell import session_status
    return session_status()


def set_session_cwd(cwd: str) -> None:
    """把会话记录的 cwd 同步为 ``cwd``（供工具层使用）。"""
    session = _get_session()
    try:
        session.cwd = cwd
    except Exception:
        pass


__all__ = [
    "run_command",
    "run_command_persistent",
    "get_persistent_session",
    "reset_shell_session",
    "close_shell_session",
    "shell_session_status",
    "set_session_cwd",
]
