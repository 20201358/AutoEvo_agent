"""长生命周期 Shell 会话。

目标：同一个会话绑定同一个 shell 子进程，避免每次 shell 工具调用都 fork 一个
新进程。这样可以：

- 保留 shell 状态（cwd、环境变量、shell 变量、函数、别名）
- 减少进程创建开销
- 支持 ``cd /d D:\\foo`` 后 ``dir`` 仍在同一目录之类的连续操作

实现要点：

1. 启动一个长期存活的 shell 子进程（bash / PowerShell）
2. 通过 stdin 写入命令 + 空行作为提交标记，通过 stdout 读回带
   ``__WBMARK_<nonce>__rc=<n>__`` 分隔符的输出
3. 使用后台线程持续把 stdout/stderr 抽到共享 buffer，主线程阻塞等待 marker
4. shell 进程异常时自动重建；命令超时则强制 kill 并重建
5. 进程级单例 + 线程锁，确保同一时刻只有一个命令在执行

仅依赖 Python 标准库，不引入 pexpect / pywinpty 等第三方库。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .shell import default_shell, is_powershell


# ============================================================
# Wrapper 脚本：嵌入到子进程里运行的循环
# ============================================================

# Bash 包装脚本：
#   - 第一行 echo READY 标记，供父进程确认子进程已就绪
#   - 之后从 stdin 逐行读取，累积到 buf；遇空行则把 buf eval 一次
#   - 父进程负责把 nonce 嵌入到命令里（\"echo __WBMARK_<nonce>__rc=$?__\"），所以
#     wrapper 不用自己生成 nonce，跨平台一致性更好
#   - 收到 __WBSHELL_EXIT__ 则退出循环
#
# 采用\"遇空行提交\"的协议以支持多行命令。
BASH_WRAPPER = r"""#!/usr/bin/env bash
echo __WBSHELL_READY__
buf=""
while IFS= read -r line; do
    if [ "$line" = "__WBSHELL_EXIT__" ]; then
        break
    fi
    if [ -z "$line" ]; then
        if [ -n "$buf" ]; then
            eval "$buf" 2>&1
            buf=""
        fi
    else
        if [ -z "$buf" ]; then
            buf="$line"
        else
            buf="$buf"$'\n'"$line"
        fi
    fi
done
"""


# PowerShell 包装脚本：
#   - 启动时强制 UTF-8，避免中文输出乱码
#   - 同样的\"累积 + 遇空行提交\"协议
#   - 父进程负责把 nonce 嵌入到 marker echo
#   - 关键：把 ``Invoke-Expression`` 的输出（含用户命令的 stdout 与 marker echo
#     两部分）整体 capture 到 ``$output``，最后再统一 Write，避免 marker 早于命令
#     输出导致父进程在还没拿到完整输出时就切走 buffer
POWERSHELL_WRAPPER = r"""try { $Host.UI.RawUI.WindowTitle = 'WBSHELL' } catch { }
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::ErrorEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
[Console]::Out.WriteLine('__WBSHELL_READY__')
[Console]::Out.Flush()
$buf = @()
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($line -eq $null) { break }
    if ($line -eq '__WBSHELL_EXIT__') { break }
    if ([string]::IsNullOrEmpty($line)) {
        if ($buf.Count -gt 0) {
            $script = $buf -join "`n"
            try {
                # 用 dot-source 而非 Invoke-Expression，让 Set-Location / $env: 等
                # 进程级状态在脚本执行后依然保持，否则子作用域退出时 cwd 会回退
                $sb = [scriptblock]::Create($script)
                $output = . $sb 2>&1 | Out-String -Width 240
                [Console]::Out.Write($output)
            } catch {
                [Console]::Out.WriteLine("ERROR: $_")
            }
            [Console]::Out.Flush()
            $buf = @()
        }
    } else {
        $buf += $line
    }
}
"""


# cmd.exe 包装脚本：
#   - cmd 通过管道交互比较脆弱，采用\"单行命令\"协议（不支持空行提交）
#   - 通过 set /p 读一行，__WBSHELL_EXIT__ 退出，否则直接 call 执行
#   - 不支持跨行命令；遇到需要多行的脚本请改用 PowerShell
#   - nonce 由父进程嵌入到 echo 语句中
CMD_WRAPPER = r"""@echo off
echo __WBSHELL_READY__
:loop
set "line="
set /p "line="
if /i "%line%"=="__WBSHELL_EXIT__" goto end
call %line%
goto loop
:end
"""


# ============================================================
# 工具函数
# ============================================================

def _shell_kind(shell_path: str) -> str:
    """识别 shell 类型：powershell | bash | cmd | other。"""
    name = Path(shell_path).name.lower()
    # 去掉 .exe / .cmd / .bat 等扩展名
    if name.endswith(".exe") or name.endswith(".bat") or name.endswith(".cmd"):
        name = name.rsplit(".", 1)[0]
    if "powershell" in name or "pwsh" in name:
        return "powershell"
    if name == "bash" or name.endswith("-bash"):
        return "bash"
    if name in ("sh", "zsh", "dash", "ksh", "fish") or name.endswith("-sh"):
        return "sh"
    if name == "cmd" or name == "command":
        return "cmd"
    return "other"


def _wrapper_for(shell_path: str) -> tuple[str, str]:
    """返回 (wrapper_script, file_suffix)。"""
    kind = _shell_kind(shell_path)
    if kind == "powershell":
        return POWERSHELL_WRAPPER, ".ps1"
    if kind in ("bash", "sh"):
        return BASH_WRAPPER, ".sh"
    if kind == "cmd":
        return CMD_WRAPPER, ".bat"
    # 未知 shell：默认按 bash 处理（多数 POSIX 兼容）
    return BASH_WRAPPER, ".sh"


# ============================================================
# PersistentShell
# ============================================================

class PersistentShell:
    """一个长生命周期的 shell 子进程。"""

    READY_MARKER = "__WBSHELL_READY__"
    EXIT_TOKEN = "__WBSHELL_EXIT__"

    def __init__(
        self,
        shell_path: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        startup_timeout_s: float = 10.0,
    ):
        self.shell_path = shell_path or default_shell()
        self.cwd = cwd or os.getcwd()
        self.env = env
        self.startup_timeout_s = startup_timeout_s

        self.proc: Optional[subprocess.Popen] = None
        self._script_path: Optional[str] = None
        self._script_kind: str = _shell_kind(self.shell_path)

        # 输出缓冲（按到达顺序）
        self._stdout_buf: list[str] = []
        self._stderr_buf: list[str] = []
        self._buf_lock = threading.Lock()
        self._stdout_event = threading.Event()
        self._stderr_event = threading.Event()

        # 串行执行命令的锁
        self._exec_lock = threading.Lock()

        # 后台读线程
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

        self._started_at: Optional[datetime] = None
        self._cmd_count = 0

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------

    @property
    def kind(self) -> str:
        return self._script_kind

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        """启动 shell 子进程；已启动则幂等返回。"""
        if self.is_alive:
            return
        self._cleanup_script_file()

        wrapper, suffix = _wrapper_for(self.shell_path)

        # 写临时脚本。PowerShell 在 Windows 上看到 UTF-8 BOM 会按 UTF-8 解码脚本
        # 和之后的 stdin 输入，避免中文路径乱码。
        fd, self._script_path = tempfile.mkstemp(
            prefix="wbshell_", suffix=suffix
        )
        try:
            with os.fdopen(fd, "wb") as f:
                if suffix == ".ps1":
                    f.write(b"\xef\xbb\xbf")  # UTF-8 BOM
                f.write(wrapper.encode("utf-8"))
            try:
                os.chmod(self._script_path, 0o644)
            except OSError:
                pass
        except Exception:
            self._script_path = None
            raise

        # 拼出 argv
        argv = self._build_argv(self.shell_path, self._script_path)

        # 启动进程：使用二进制 pipe，避免文本模式的 buffering 在 Windows 上
        # 引发子进程 stdin 提前 EOF 的问题
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                bufsize=0,
            )
        except Exception:
            self._cleanup_script_file()
            raise

        self._started_at = datetime.now(timezone.utc)
        self._cmd_count = 0

        # 清空缓冲并启动读线程
        with self._buf_lock:
            self._stdout_buf.clear()
            self._stderr_buf.clear()
        self._stdout_event.clear()
        self._stderr_event.clear()

        self._stdout_thread = threading.Thread(
            target=self._reader_loop,
            args=("stdout",),
            daemon=True,
            name="PShell-stdout",
        )
        self._stderr_thread = threading.Thread(
            target=self._reader_loop,
            args=("stderr",),
            daemon=True,
            name="PShell-stderr",
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        # 等 READY marker
        if not self._wait_for_text(self.READY_MARKER, self.startup_timeout_s):
            self.close()
            raise RuntimeError(
                f"持久化 shell 启动超时（>{self.startup_timeout_s}s）"
            )

        # READY 之后清理临时脚本（已加载到内存）
        self._cleanup_script_file()

    def _build_argv(self, shell_path: str, script_path: str) -> list[str]:
        """根据 shell 类型构造启动参数。"""
        if self._script_kind == "powershell":
            return [
                shell_path,
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-NoExit",
                "-File", script_path,
            ]
        if self._script_kind in ("bash", "sh"):
            return [shell_path, "--norc", "--noprofile", script_path]
        if self._script_kind == "cmd":
            return [shell_path, "/Q", "/K", script_path]
        return [shell_path, script_path]

    def _reader_loop(self, stream_name: str) -> None:
        """后台线程：从子进程 stdout/stderr 持续读 bytes 到列表，按行切分。"""
        try:
            if stream_name == "stdout":
                stream = self.proc.stdout
                buf = self._stdout_buf
                event = self._stdout_event
            else:
                stream = self.proc.stderr
                buf = self._stderr_buf
                event = self._stderr_event

            if stream is None:
                return
            pending = b""
            decoder_buf = b""
            for raw in iter(stream.readline, b""):
                # 解码为字符串再 append，便于上层做字符串匹配
                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:
                    line = raw.decode("latin-1", errors="replace")
                with self._buf_lock:
                    buf.append(line)
                event.set()
        except (OSError, ValueError):
            pass
        except Exception:
            pass

    def _wait_for_text(
        self, needle: str, timeout: float, snapshot_index: int = 0
    ) -> bool:
        """阻塞直到 needle 出现在 stdout 缓冲区或超时。

        ``snapshot_index`` 用于兼容旧调用，不再单独使用；调用方应当在
        ``_wait_for_text_atomic`` 中配合 buffer 快照一起使用。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._buf_lock:
                for line in self._stdout_buf:
                    if needle in line:
                        return True
            if not self.is_alive:
                return False
            self._stdout_event.wait(timeout=0.1)
            self._stdout_event.clear()
        return False

    def _wait_for_text_atomic(
        self, needle: str, timeout: float, start_idx: int
    ) -> tuple[bool, list[str]]:
        """阻塞直到 needle 出现或超时；返回 ``(found, new_stdout)``。

        关键点：找到 needle 时，立刻在同一锁内对 stdout 缓冲做切片；
        切片范围限定为 ``[start_idx, marker_idx+1)``，避免 reader 线程在
        marker 检测之后又追加若干行导致输出归错位。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._buf_lock:
                for i, line in enumerate(self._stdout_buf):
                    if needle in line:
                        new_stdout = list(self._stdout_buf[start_idx:i + 1])
                        return True, new_stdout
            if not self.is_alive:
                return False, []
            self._stdout_event.wait(timeout=0.1)
            self._stdout_event.clear()
        return False, []

    # --------------------------------------------------------
    # 命令执行
    # --------------------------------------------------------

    def execute(
        self,
        cmd: str,
        timeout: int = 120,
        max_output: int = 4000,
    ) -> dict:
        """在持久化 shell 中执行一条命令，返回 dict。"""
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()

        with self._exec_lock:
            # 如果进程已死，重启
            if not self.is_alive:
                try:
                    self.start()
                except Exception as e:
                    return self._err(
                        started, t0, -4, f"重启 shell 失败: {type(e).__name__}: {e}"
                    )

            nonce = uuid.uuid4().hex
            marker = f"__WBMARK_{nonce}__"

            # 记录发送前的缓冲区位置，便于提取本命令的输出
            with self._buf_lock:
                stdout_start = len(self._stdout_buf)
                stderr_start = len(self._stderr_buf)

            # 构造 stdin payload
            try:
                self._write_command(cmd, marker)
            except (BrokenPipeError, OSError, ValueError) as e:
                # pipe 已断，重试一次
                self.close()
                try:
                    self.start()
                    with self._buf_lock:
                        stdout_start = len(self._stdout_buf)
                        stderr_start = len(self._stderr_buf)
                    self._write_command(cmd, marker)
                except Exception as e2:
                    return self._err(
                        started, t0, -5, f"写入失败: {type(e2).__name__}: {e2}"
                    )

            # 等 marker（同时在 marker 出现的瞬间对 buffer 做原子切片）
            found, new_stdout = self._wait_for_text_atomic(
                marker, timeout, stdout_start
            )
            duration_ms = int((time.perf_counter() - t0) * 1000)

            if not found:
                # 超时或进程死亡：强杀并重建
                self.close()
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"命令超时（>{timeout}s），已强制终止并重建 shell",
                    "started_at": started.isoformat(),
                    "duration_ms": duration_ms,
                }

            # stderr 单独取
            with self._buf_lock:
                stderr_since = list(self._stderr_buf[stderr_start:])

            stdout_text, exit_code = self._extract_output(new_stdout, marker)

            # 截断输出
            if len(stdout_text) > max_output:
                stdout_text = stdout_text[-max_output:]
            stderr_text = "".join(stderr_since).rstrip("\r\n")
            if len(stderr_text) > max_output:
                stderr_text = stderr_text[-max_output:]

            self._cmd_count += 1
            duration_ms = int((time.perf_counter() - t0) * 1000)

            return {
                "exit_code": exit_code if exit_code is not None else 0,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "started_at": started.isoformat(),
                "duration_ms": duration_ms,
            }

    def _write_command(self, cmd: str, marker: str) -> None:
        """把命令 + marker-echo + 空行终止符写入 stdin（二进制）。"""
        assert self.proc and self.proc.stdin
        if self._script_kind == "powershell":
            # PowerShell：用 Write-Output 让 marker 走 success stream，
            # 这样它会跟用户命令的输出一起被 ``| Out-String`` 捕获，
            # 顺序上 marker 一定在用户命令输出之后到达父进程
            payload = (
                f"{cmd}\n"
                f"Write-Output \"{marker}__\"\n"
                f"\n"
            )
        elif self._script_kind in ("bash", "sh"):
            payload = f"{cmd}\necho \"{marker}__rc=$?__\"\n\n"
        elif self._script_kind == "cmd":
            payload = f"{cmd} & echo {marker}__rc=%ERRORLEVEL%__\n"
        else:
            payload = f"{cmd}\necho \"{marker}__rc=$?__\"\n\n"

        self.proc.stdin.write(payload.encode("utf-8"))
        self.proc.stdin.flush()

    def _extract_output(
        self, lines: list[str], marker: str
    ) -> tuple[str, Optional[int]]:
        """从捕获到的行里剔除所有 marker 行，从 marker 中提取 exit_code。"""
        kept: list[str] = []
        exit_code: Optional[int] = None
        # marker 形如 "__WBMARK_<nonce>__rc=<n>__"（bash / sh）
        # 或        "__WBMARK_<nonce>____"（PowerShell，rc 由 marker echo 内部捕获）
        # 关键：要过滤掉所有 __WBMARK_*__ 行，不仅是当前 marker —— 否则前一条命令的
        # marker 在 reader 线程里延迟到达时，会被错误归入当前命令的输出
        for line in lines:
            if "__WBMARK_" in line:
                # 顺便解析当前 marker 行的 exit code
                if marker in line:
                    idx = line.find("rc=")
                    if idx >= 0:
                        tail = line[idx + 3:]
                        num_str = ""
                        for ch in tail:
                            if ch.isdigit() or (ch == "-" and not num_str):
                                num_str += ch
                            else:
                                break
                        try:
                            exit_code = int(num_str)
                        except ValueError:
                            exit_code = None
                continue
            kept.append(line)
        return "".join(kept).rstrip("\r\n"), exit_code

    def _err(
        self,
        started: datetime,
        t0: float,
        code: int,
        msg: str,
    ) -> dict:
        duration = int((time.perf_counter() - t0) * 1000)
        return {
            "exit_code": code,
            "stdout": "",
            "stderr": msg,
            "started_at": started.isoformat(),
            "duration_ms": duration,
        }

    # --------------------------------------------------------
    # 重置 / 关闭
    # --------------------------------------------------------

    def reset(self) -> None:
        """杀掉当前 shell，重新启动一个。"""
        self.close()
        self.start()

    def close(self) -> None:
        """优雅关闭子进程；必要时强杀。"""
        try:
            if self.proc and self.proc.poll() is None and self.proc.stdin:
                try:
                    self.proc.stdin.write((self.EXIT_TOKEN + "\n").encode("utf-8"))
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            if self.proc:
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        try:
                            self.proc.wait(timeout=1)
                        except Exception:
                            pass
        except Exception:
            pass

        try:
            if self.proc and self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.stderr:
                self.proc.stderr.close()
        except Exception:
            pass

        self.proc = None
        self._cleanup_script_file()

    def _cleanup_script_file(self) -> None:
        if self._script_path:
            try:
                os.unlink(self._script_path)
            except OSError:
                pass
            self._script_path = None

    def _cleanup(self) -> None:
        self.close()
        with self._buf_lock:
            self._stdout_buf.clear()
            self._stderr_buf.clear()

    def __del__(self) -> None:
        try:
            self._cleanup()
        except Exception:
            pass

    def __repr__(self) -> str:
        alive = self.is_alive
        return (
            f"<PersistentShell kind={self._script_kind} "
            f"shell={self.shell_path!r} alive={alive} cmds={self._cmd_count}>"
        )


# ============================================================
# 进程级单例（同一会话复用同一 shell）
# ============================================================

_session_lock = threading.Lock()
_session: Optional[PersistentShell] = None


def get_session(
    shell_path: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> PersistentShell:
    """获取（或懒创建）当前进程的持久化 shell 会话。"""
    global _session
    with _session_lock:
        if _session is None:
            _session = PersistentShell(
                shell_path=shell_path,
                cwd=cwd,
                env=env,
            )
            _session.start()
        else:
            if not _session.is_alive:
                _session.start()
    return _session


def reset_session() -> None:
    """重置会话：杀掉当前 shell 并重启一个新的。"""
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.reset()
            except Exception:
                try:
                    _session.close()
                except Exception:
                    pass
                _session = None


def close_session() -> None:
    """关闭并清空会话。"""
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
            _session = None


def session_status() -> dict:
    """返回当前会话的可观测信息（用于工具输出 / 调试）。"""
    with _session_lock:
        if _session is None:
            return {
                "active": False,
                "shell": None,
                "alive": False,
                "cmd_count": 0,
                "started_at": None,
            }
        return {
            "active": True,
            "shell": _session.shell_path,
            "kind": _session.kind,
            "alive": _session.is_alive,
            "cmd_count": _session._cmd_count,
            "started_at": _session._started_at.isoformat() if _session._started_at else None,
            "cwd": _session.cwd,
        }


__all__ = [
    "PersistentShell",
    "get_session",
    "reset_session",
    "close_session",
    "session_status",
]