"""安装器工具：运行 .exe / .msi 等安装程序。

这是「高风险」操作的代表：会修改系统（写注册表、装服务、改 PATH、装驱动等），
默认 ``requires_confirm=True`` 且 ``risk=high``，每次执行都需要用户明确同意。

设计要点：
    - 默认走「静默常用参数」（如 ``/S`` / ``/quiet``）；用户传 args 时可以覆盖
    - 给 Windows / Unix 两种风格都留接口
    - 把执行前后的状态摘要写回 stdout，方便用户审核
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..platform_utils.tools_detect import find
from ._base import register


def _is_windows() -> bool:
    return os.name == "nt"


# ============================================================
# PATH 写入辅助（Windows）
# ============================================================

def _is_admin() -> bool:
    """当前进程是否以管理员身份运行。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _read_path_scope(scope_ps: str) -> str | None:
    """读取某作用域（'Machine'/'User'）的当前 Path 值；失败返回 None。

    强制 PowerShell 输出 UTF-8（默认控制台 GBK 会让含中文的 PATH 解码失败）。
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
             f"[Environment]::GetEnvironmentVariable('Path', '{scope_ps}')"],
            capture_output=True, timeout=15,
        )
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def _path_contains(current: str, directory: str) -> bool:
    """某作用域 PATH 里是否已包含该目录（大小写、尾部反斜杠不敏感）。"""
    want = directory.rstrip("\\").lower()
    for part in (current or "").split(";"):
        if part.strip().rstrip("\\").lower() == want:
            return True
    return False


def _ps_quote(s: str) -> str:
    """转成 PowerShell 单引号字符串字面量。"""
    return "'" + s.replace("'", "''") + "'"


_PS_ADD_PATH_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$scope = __SCOPE__
$dir = __DIR__
try {
    $cur = [Environment]::GetEnvironmentVariable('Path', $scope)
    if ($null -eq $cur) { $cur = '' }
    $hit = $false
    foreach ($p in ($cur -split ';')) {
        if ($p.TrimEnd('\') -ieq $dir.TrimEnd('\')) { $hit = $true; break }
    }
    if ($hit) {
        Write-Output 'ALREADY_PRESENT'
    } else {
        $new = if ($cur.Trim()) { $cur.TrimEnd(';') + ';' + $dir } else { $dir }
        [Environment]::SetEnvironmentVariable('Path', $new, $scope)
        # 用 setx 触发 WM_SETTINGCHANGE 广播（比 Add-Type + SendMessageTimeout
        # 简单可靠，不会让提权子进程因 .NET 编译残留而不退出）
        setx __AE_REFRESH_VAR__ 1 2>$null | Out-Null
        Write-Output 'OK'
    }
} catch {
    Write-Output ('ERROR: ' + $_.Exception.Message)
    exit 1
}
"""


def _build_add_path_script(scope_ps: str, directory: str) -> str:
    """生成"读当前值→去重→追加→写回→广播"的 PS 脚本。"""
    return (
        _PS_ADD_PATH_TEMPLATE
        .replace("__SCOPE__", _ps_quote(scope_ps))
        .replace("__DIR__", _ps_quote(directory))
        .replace("__AE_REFRESH_VAR__", "AE_PATH_REFRESH")
    )


def _build_elevated_bat(directory: str, ps_script_path: str) -> str:
    """生成一个自提权 .bat 文件并返回其路径。

    .bat 逻辑：
      1. 检测当前是否管理员（net session）
      2. 非管理员 → Start-Process -Verb RunAs 重启自身（弹 UAC）
      3. 管理员 → 执行 PS 脚本写入系统 PATH
      4. 完成后 pause（让用户看到结果再关）

    之所以用 .bat 而不是直接在 Python 里 Start-Process -Verb RunAs：
    - .bat 的自提权是 Windows 原生机制，不阻塞调用方进程
    - os.startfile() 启动 .bat 是 fire-and-forget，agent 立即返回
    """
    import tempfile

    # PS 脚本路径放进 .bat 里（路径可能含空格，需引号）
    bat_content = f"""@echo off
chcp 65001 >nul 2>&1
REM AutoEvo Agent: add {directory} to SYSTEM Path (self-elevating)

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Running as admin: adding {directory} to SYSTEM Path...
powershell -NoProfile -ExecutionPolicy Bypass -File "{ps_script_path}"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Write failed. Check the message above.
) else (
    echo.
    echo Done. Open a NEW terminal to verify.
)
timeout /t 3 /nobreak >nul
"""

    bat_fd, bat_path = tempfile.mkstemp(suffix="_add_path.bat")
    try:
        with os.fdopen(bat_fd, "w", encoding="ascii", errors="replace") as f:
            f.write(bat_content)
    except Exception:
        try:
            os.close(bat_fd)
        except OSError:
            pass
        raise
    return bat_path


@register(category="installer", requires_confirm=True, risk="high")
def run_installer(
    installer_path: str,
    args: str = "",
    timeout: float = 600.0,
    capture_output: bool = True,
    dry_run: bool = False,
) -> str:
    """运行一个安装器。

    ⚠ **这会修改你的系统**：写注册表、装服务、改 PATH、写 Program Files、可能开机启动项等。
    除非你**真的**要安装软件，否则不要调用。默认要求用户确认。

    执行前你可以先 dry_run=True 看一眼将要跑的完整命令（不会真的运行）。

    Args:
        installer_path: 安装器可执行文件路径（.exe / .msi / .pkg / .run / .sh）。
        args: 额外的命令行参数（一个字符串）。常用静默参数：
            Windows NSIS 安装器: /S
            Windows MSI:         /quiet /norestart
            macOS .pkg:           -target /
        timeout: 超时秒数。
        capture_output: 是否捕获 stdout/stderr（默认 True）。
        dry_run: True 时只回显将要运行的命令而不实际执行。

    Returns:
        退出码 + 输出摘要。
    """
    p = Path(installer_path).expanduser().resolve()
    if not p.exists():
        return f"错误：安装器不存在 -> {p}"
    if not p.is_file():
        return f"错误：不是文件 -> {p}"

    arg_list = [a for a in (args or "").split() if a]
    cmd = [str(p)] + arg_list

    if dry_run:
        return f"[DRY-RUN] 将执行：\n  {' '.join(cmd)}\n（未真正修改系统）"

    # Windows 下 .msi 需要 msiexec；.exe 直接跑
    if _is_windows() and p.suffix.lower() == ".msi":
        cmd = ["msiexec", "/i", str(p)] + arg_list

    try:
        proc = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"错误：安装器超时（>{timeout}s），已强制结束"
    except Exception as e:
        return f"错误：执行失败 -> {type(e).__name__}: {e}"

    out = (proc.stdout or "").strip()[:1500]
    err = (proc.stderr or "").strip()[:1500]
    rc = proc.returncode
    parts = [f"已运行安装器：{' '.join(cmd)}", f"退出码：{rc}"]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    return "\n".join(parts)


@register(category="installer", requires_confirm=True, risk="medium")
def add_to_path(directory: str, scope: str = "user", timeout: float = 30.0) -> str:
    """把一个目录加到 PATH 环境变量（Windows / Unix）。

    ⚠ 这会修改你的 PATH**，默认要求用户确认。

    作用域按用户要求执行，不要擅自降级：
      - "user"   当前用户的 PATH（HKCU），直接写入。
      - "system" 所有用户的系统 PATH（HKLM）。**需要管理员权限**；当前进程
        未提权时会生成一个自提权 .bat 并**非阻塞启动**（不等待 UAC 确认），
        立即返回指引让用户留意屏幕上的授权弹窗。**不要在进程内同步等待 UAC**——
        桌面弹窗如果没及时处理会让 REPL 主线程永久卡死。
      - "session" 仅当前 shell 会话（提示改用 run_shell）。

    写入方式：先读取目标作用域的当前值、去重后追加，写回并广播
    （setx 触发 WM_SETTINGCHANGE），**不要**用 `%PATH%` 展开——
    那会把"系统+用户"的合并值写进单一作用域造成污染。

    Args:
        directory: 要加入的目录绝对路径。
        scope: "user" / "system" / "session"，默认 "user"。
        timeout: 直接执行（用户级/已提权）时的超时秒数。系统级非提权场景
            不等待（fire-and-forget），此参数不适用。
    """
    if scope not in ("user", "system", "session"):
        return f"错误：未知 scope {scope!r}（可选 user / system / session）"

    p = Path(directory).expanduser().resolve()
    if not p.is_dir():
        return f"错误：不是目录 -> {p}"

    if not _is_windows():
        # Unix：写到 ~/.bashrc / ~/.zshrc（粗略追加）
        shell_rc = Path.home() / (".zshrc" if find("zsh") else ".bashrc")
        marker = f"\n# AutoEvo Agent: added {p}\n"
        if shell_rc.exists() and str(p) in shell_rc.read_text(encoding="utf-8", errors="replace"):
            return f"已在 {shell_rc} 中跳过（已存在）"
        with open(shell_rc, "a", encoding="utf-8") as f:
            f.write(f"{marker}export PATH=\"{p}:$PATH\"\n")
        return f"已写入 {shell_rc}，新终端生效"

    if scope == "session":
        return (
            "提示：session 模式请用 run_shell 工具执行 "
            f"`$env:Path = $env:Path + ';{p}'`（只对当前 shell 会话生效）"
        )

    scope_ps = "Machine" if scope == "system" else "User"
    scope_label = "系统" if scope == "system" else "当前用户"

    # 先查重（读取不需要管理员）
    current = _read_path_scope(scope_ps)
    if current is not None and _path_contains(current, str(p)):
        return f"{scope_label} PATH 中已存在 {p}，无需重复添加"

    elevate = scope == "system" and not _is_admin()

    # 生成 PS 脚本（两个分支都要用）
    script = _build_add_path_script(scope_ps, str(p))
    import tempfile
    with tempfile.NamedTemporaryFile(
        "w", suffix=".ps1", delete=False, encoding="utf-8-sig"
    ) as f:
        f.write(script)
        script_path = f.name

    # ---------- 系统级 + 需提权：生成自提权 .bat，非阻塞启动，立即返回 ----------
    # 关键：agent 进程绝不同步等待桌面 UAC 交互——一旦弹窗没及时处理，
    # Start-Process -Wait 会永远不返回，REPL 主线程被卡死。
    # 改成"生成脚本 + fire-and-forget 启动 + 立即返回指引"，由用户自行确认 UAC。
    if elevate:
        bat_path = _build_elevated_bat(str(p), script_path)
        try:
            os.startfile(bat_path)  # 非阻塞：不等待子进程退出
        except Exception as e:
            return (
                f"⚠️ 自动启动提权脚本失败（{type(e).__name__}: {e}）。\n"
                f"请手动双击执行：{bat_path}\n"
                f"（它会弹出 UAC 授权框，确认后即写入系统 PATH）"
            )
        return (
            f"⏳ 已生成提权脚本并启动：{bat_path}\n"
            f"   **请留意屏幕上的 UAC 授权弹窗，点「是」确认。**\n"
            f"   脚本会自动完成写入并关闭，无需你做其他操作。\n"
            f"   完成后可用 run_shell 验证：\n"
            f"   [Environment]::GetEnvironmentVariable('Path', 'Machine')\n"
            f"   （含 {p} 即成功；新开终端立即可见）"
        )

    # ---------- 用户级 / 已是管理员的系统级：直接执行 ----------
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script_path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        out = (proc.stdout or "").strip()
        if "ALREADY_PRESENT" in out:
            return f"{scope_label} PATH 中已存在 {p}，无需重复添加"
        if proc.returncode != 0 or "ERROR" in out:
            err = (proc.stderr or "").strip() or out or f"退出码 {proc.returncode}"
            hint = ""
            if scope == "system":
                hint = "（系统级 PATH 需要管理员权限；当前进程未提权时请用 UAC 流程或管理员终端）"
            return f"错误：写入失败 -> {err}{hint}"
        return f"✅ 已把 {p} 写入{scope_label} PATH；新开的终端即可使用"
    except Exception as e:
        return f"错误：写入 PATH 失败 -> {type(e).__name__}: {e}"
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["run_installer", "add_to_path"]