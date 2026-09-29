"""Shell 工具：持久化会话执行命令。

同一个进程只保留一个 shell 子进程，因此连续命令之间 cwd / 环境变量 /
shell 变量都保持。遇到卡死可用 ``shell_reset`` 重建。

删/写类命令（rm / del / Remove-Item / mv / cp / 重定向等）执行成功后，
会自动追加一条 ``ls/dir/Get-ChildItem`` 验证，把实际目录状态一并返回，
避免 agent 看到 ``exit_code=0 + stdout 空`` 时误以为成功而反复重试。
"""

from __future__ import annotations

import re

from ..config import settings
from ..platform_utils import (
    default_shell,
    reset_shell_session,
    run_command,
    run_command_persistent,
    shell_session_status,
)
from ..platform_utils.persistent_shell import _shell_kind
from ._base import register


# ============================================================
# 删/写命令识别 + 自动验证
# ============================================================

# 检测命令里是否有"可能改变文件状态"的动作
_DESTRUCTIVE_PATTERNS = (
    # 删除
    r"\bdel\b", r"\brm\b", r"\brmdir\b", r"\bRemove-Item\b",
    r"\berase\b", r"\bclear-content\b",
    # 移动 / 重命名
    r"\bmv\b", r"\bmove\b", r"\bMove-Item\b", r"\bren\b", r"\bRename-Item\b",
    # 拷贝（不一定需要验证，但写操作算一类）
    r"\bcp\b", r"\bcopy\b", r"\bCopy-Item\b", r"\brobocopy\b",
    # 写文件 / 管道重定向
    r"\bOut-File\b", r"\bSet-Content\b", r"\bAdd-Content\b", r"\bNew-Item\b",
    r"\btee\b", r"\btruncate\b",
    # shell 重定向：> / >> 写到文件（必须后接路径字符）
    r">>\s*[\w/\\\.\-]",          # >> file
    r"(?<!\d)>\s*[\w/\\\.\-]",    # > file（排除 2> / &> / >| 等 fd 重定向）
)


def _is_destructive(command: str) -> bool:
    """粗略判断命令里是否含破坏性动作。"""
    if not command:
        return False
    for pat in _DESTRUCTIVE_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return True
    return False


# 不同 shell 的"列出当前目录内容"命令
_VERIFY_BY_KIND = {
    "powershell": "Get-ChildItem -Force -ErrorAction SilentlyContinue | Format-Table Name, Length, LastWriteTime -AutoSize",
    "bash": "ls -la",
    "sh": "ls -la",
    "cmd": "dir",
}


def _verify_command(kind: str) -> str:
    return _VERIFY_BY_KIND.get(kind, "ls -la")


def _detect_shell_kind() -> str:
    try:
        st = shell_session_status()
        if st.get("active"):
            return st.get("kind") or "unknown"
    except Exception:
        pass
    try:
        return _shell_kind(default_shell())
    except Exception:
        return "unknown"


# ============================================================
# PowerShell 语法硬拦截
# ============================================================
# 这些 cmd.exe / POSIX 语法在 Windows PowerShell 5.1 里是**解析错误**，
# 会把持久化 shell 的协议 marker 一起吞掉 → 命令假死 120s 直到超时。
# 与其让它假死，不如直接秒回错误让 agent 改写。

def _strip_quotes(cmd: str) -> str:
    """去掉引号内的内容，避免把字符串字面量里的 && 误判成语法。"""
    return re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "", cmd or "")


def _powershell_syntax_guard(command: str) -> str | None:
    """命令含 PowerShell 5.1 不支持的语法时返回错误信息；放行返回 None。"""
    bare = _strip_quotes(command)
    if "&&" in bare:
        return (
            "⛔ 命令未执行：Windows PowerShell 5.1 不支持 `&&`。请改写：\n"
            "  - 顺序执行用 `;` 分隔：`cmd1; cmd2`\n"
            "  - 前一条成功才执行下一条：`cmd1; if ($?) { cmd2 }`\n"
            "  - 需要完整 POSIX 语法可显式调用：`bash -lc \"cmd1 && cmd2\"`"
        )
    if "||" in bare:
        return (
            "⛔ 命令未执行：Windows PowerShell 5.1 不支持 `||`。请改写：\n"
            "  - `cmd1; if ($LASTEXITCODE -ne 0) { cmd2 }`\n"
            "  - 需要完整 POSIX 语法可显式调用：`bash -lc \"cmd1 || cmd2\"`"
        )
    if re.search(r"2>\s*nul\b", bare, re.IGNORECASE):
        return (
            "⛔ 命令未执行：`2>nul` 是 cmd.exe 语法，在 PowerShell 中无效且会"
            "吞掉输出。请改用 `-ErrorAction SilentlyContinue`，"
            "或丢弃输出用 `| Out-Null`。"
        )
    if re.search(r"\becho\.(?!\w)", bare, re.IGNORECASE):
        return (
            "⛔ 命令未执行：`echo.` 是 cmd.exe 语法，PowerShell 输出空行"
            "用 `echo ''` 或 `Write-Host ''`。"
        )
    if re.search(r"(?:^|[;|])\s*cd\s+/d\b", bare, re.IGNORECASE):
        return (
            "⛔ 命令未执行：`cd /d` 是 cmd.exe 语法，PowerShell 的 `cd`（Set-Location）"
            "本身就支持跨盘切换，直接 `cd D:\\path` 即可。"
        )
    # 嵌套 shell：持久会话本身就是 shell，再起 powershell/cmd 子 shell
    # 极易因 stdio 占用导致 marker 丢失 → 命令假死超时
    if re.match(r"\s*(?:powershell|pwsh|cmd)\b", bare, re.IGNORECASE):
        return (
            "⛔ 命令未执行：当前持久化会话本身就是 shell，不要再嵌套启动 "
            "`powershell` / `pwsh` / `cmd` 子 shell（极易导致命令假死超时）。"
            "请把内层命令直接写出来执行；确实需要 POSIX 语法用 `bash -lc \"...\"`。"
        )
    return None


def _run_verify(kind: str, timeout: int) -> str:
    """跑一条 ls/dir，把 stdout 返回；拿不到就空串。"""
    cmd = _verify_command(kind)
    try:
        if settings.SHELL_PERSISTENT:
            r = run_command_persistent(
                cmd,
                timeout=max(5, min(timeout, 30)),
                max_output=settings.MAX_OUTPUT,
            )
        else:
            r = run_command(cmd, timeout=max(5, min(timeout, 30)),
                            max_output=settings.MAX_OUTPUT)
        return (r.get("stdout") or "").strip()
    except Exception as e:
        return f"[自动验证执行失败: {type(e).__name__}: {e}]"


def _format(result: dict, command: str = "") -> str:
    parts = [f"exit_code={result.get('exit_code')}"]
    out = (result.get("stdout") or "").strip("\n")
    err = (result.get("stderr") or "").strip("\n")
    parts.append(f"stdout:\n{out}" if out else "stdout: (空)")
    if err:
        parts.append(f"stderr:\n{err}")
    dur = result.get("duration_ms")
    if dur is not None:
        parts.append(f"(耗时 {dur} ms)")
    # 验证目录提示：如果命令是删/写类、自动验证是 cwd，但目标可能在其它路径，
    # 提示 agent 必要时再跑一次 ls/dir <path>
    if command and _is_destructive(command):
        parts.append(
            "\n💡 验证使用的是当前 shell 会话的工作目录。"
            "如本次操作的目标路径不在 cwd 里，请再跑一次 `Get-ChildItem <path>` "
            "/ `ls -la <path>` / `dir <path>` 核对。"
        )
    return "\n".join(parts)


@register(category="shell", risk="medium")
def run_shell(command: str, timeout: int = 120, cwd: str = "") -> str:
    """执行 shell 命令并返回输出。

    注意：
      - 命令在**同一个持久化会话**中执行，`cd` 之后的下一条命令仍在同一目录，
        `export`/`$env:` 设置的变量也会保留。
      - Windows 上默认 shell 是 **PowerShell 5.1**，必须使用 PowerShell 语法：
        * 多条命令用 `;` 分隔，不要用 `&&`
        * 环境变量写作 `$env:NAME`
        * **绝对不要用 cmd.exe 语法**（`2>nul` / `echo.` / `dir /b` / `find /c` 等），
          它们会报错或把协议输出重定向掉，导致命令假死直到超时
        * 创建文件用 `New-Item` / `Set-Content`，丢弃输出用 `| Out-Null`
        * 需要 POSIX 语法时可显式调用 `bash -lc "..."`
      - 输出过长的部分会被截断。
      - 中等风险：可以读取文件与运行程序，但请避免 rm -rf、关闭系统等危险命令。

    **删/写后自动验证**：当命令含有 ``rm / del / Remove-Item / mv / cp / 重定向``
    等破坏性动作时，工具会在原命令之后自动追加一条 ``ls / dir / Get-ChildItem``
    并把实际目录状态合并到 stdout 返回。目的是让模型能看到真实文件清单，避免
    仅凭 ``exit_code=0 + stdout 空`` 就误判成功而反复重试。

    Args:
        command: 要执行的命令。
        timeout: 超时秒数。
        cwd: 可选，执行前切换到这个目录（会话内保持）。
    """
    eff_timeout = int(timeout or settings.TOOL_TIMEOUT)

    # PowerShell 语法硬拦截：cmd.exe 语法会造成解析错误 → marker 被吞 →
    # 假死 120s。直接秒回错误，让 agent 立刻改写，省一轮超时重试。
    try:
        _kind = (_detect_shell_kind() or "").lower()
    except Exception:
        _kind = ""
    if "powershell" in _kind or "pwsh" in _kind:
        guard_err = _powershell_syntax_guard(command)
        if guard_err:
            return guard_err

    if settings.SHELL_PERSISTENT:
        result = run_command_persistent(
            command,
            cwd=cwd or None,
            timeout=eff_timeout,
            max_output=settings.MAX_OUTPUT,
            shell_path=settings.SHELL_PATH,
        )
    else:
        result = run_command(
            command,
            cwd=cwd or None,
            shell_path=settings.SHELL_PATH,
            timeout=eff_timeout,
            max_output=settings.MAX_OUTPUT,
        )

    # 删/写后自动追加验证：只有"成功"的命令才有必要验证
    if _is_destructive(command) and int(result.get("exit_code") or 0) == 0:
        kind = _detect_shell_kind()
        verify_text = _run_verify(kind, eff_timeout)
        if verify_text:
            sep = "\n\n--- 自动验证（" + _verify_command(kind) + "）---\n"
            base_out = (result.get("stdout") or "").rstrip()
            if base_out:
                result["stdout"] = base_out + sep + verify_text
            else:
                result["stdout"] = sep.lstrip() + verify_text
        else:
            base_out = (result.get("stdout") or "").rstrip()
            note = f"\n\n[自动验证：{_verify_command(kind)} 未返回任何内容]"
            result["stdout"] = (base_out + note) if base_out else note.strip()

    return _format(result, command)


@register(category="shell")
def shell_reset() -> str:
    """重置持久化 shell 会话（关掉旧 shell 并启动新的）。

    当 shell 因不可恢复错误卡住（例如某个后台进程占住了 stdin）时使用。
    重置后之前设置的 cwd / 环境变量 / 别名都会丢失。
    """
    reset_shell_session()
    return "持久化 shell 会话已重置"


@register(category="shell")
def shell_info() -> str:
    """查看当前 shell 会话信息（类型、路径、工作目录、累计命令数、是否存活）。"""
    st = shell_session_status()
    if not st.get("active"):
        return f"当前没有活跃的 shell 会话。默认 shell 为：{default_shell()}"
    return "\n".join(
        [
            f"shell      : {st.get('shell')}",
            f"kind       : {st.get('kind')}",
            f"alive      : {st.get('alive')}",
            f"cwd        : {st.get('cwd')}",
            f"cmd_count  : {st.get('cmd_count')}",
            f"started_at : {st.get('started_at')}",
        ]
    )


# 兼容旧导入
def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


SHELL_TOOLS = ["run_shell", "shell_reset", "shell_info"]

__all__ = ["run_shell", "shell_reset", "shell_info", "SHELL_TOOLS"]