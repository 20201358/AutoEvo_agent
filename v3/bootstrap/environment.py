"""启动时环境探测。

bootstrap_environment() 在进程启动时调用一次，返回 EnvironmentInfo 字典。
图内节点只读 environment，不再修改。

平台差异全部集中在此文件，节点代码不含 sys.platform 分支。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path


# ============================================================
# 对外接口
# ============================================================

def bootstrap_environment() -> dict:
    """探测当前机器环境，返回 EnvironmentInfo 字典。

    在进程启动时调用一次。结果作为会话级状态注入图。
    """
    system = platform.system()          # "Windows" | "Linux" | "Darwin"

    if system == "Windows":
        env = _probe_windows()
    else:
        env = _probe_unix(system)

    # 通用字段
    env["python_version"] = platform.python_version()
    env["cwd"] = os.getcwd()
    env["available_tools"] = _detect_tools(system)

    return env


# ============================================================
# 平台探测
# ============================================================

def _probe_windows() -> dict:
    return {
        "os": "windows",
        "distro": _windows_version(),
        "shell": _default_windows_shell(),
        "home": os.environ.get("USERPROFILE", str(Path.home())),
        "user": os.environ.get("USERNAME", "unknown"),
        "hostname": os.environ.get("COMPUTERNAME", platform.node()),
        "env_vars": {
            k: v for k, v in os.environ.items()
            if k in (
                "PATH",
                "USERPROFILE",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "COMSPEC",
                "PATHEXT",
                "USERNAME",
                "COMPUTERNAME",
            )
        },
    }


def _probe_unix(system: str) -> dict:
    return {
        "os": system.lower(),
        "distro": _read_os_release() if system == "Linux" else platform.platform(),
        "shell": os.environ.get("SHELL", "/bin/sh"),
        "home": os.environ.get("HOME", str(Path.home())),
        "user": os.environ.get("USER", "unknown"),
        "hostname": platform.node(),
        "env_vars": {
            k: v for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "LC_ALL", "SHELL", "USER")
        },
    }


# ============================================================
# 辅助函数
# ============================================================

def _windows_version() -> str:
    """返回 Windows 版本描述，如 "Windows 11 10.0.22631"。"""
    try:
        release, version, csd, _ = platform.win32_ver()
        parts = [p for p in (release, version, csd) if p]
        return "Windows " + " ".join(parts) if parts else platform.platform()
    except Exception:
        return platform.platform()


def _default_windows_shell() -> str:
    """优先 PowerShell 7 (pwsh)，其次 Windows PowerShell，最后 cmd。"""
    for name in ("pwsh.exe", "powershell.exe"):
        path = shutil.which(name)
        if path:
            return path
    return os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


def _read_os_release() -> str:
    """读取 /etc/os-release 的 PRETTY_NAME。"""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return platform.platform()


def _detect_tools(system: str) -> list[str]:
    """检测常用工具是否可用。"""
    if system == "Windows":
        candidates = [
            "powershell.exe", "pwsh.exe", "cmd.exe",
            "git.exe", "python.exe", "pip.exe",
            "docker.exe", "curl.exe", "node.exe", "npm.cmd",
            "winget.exe", "choco.exe", "scoop.cmd",
            "7z.exe", "tar.exe",
        ]
    else:
        candidates = [
            "bash", "sh", "zsh",
            "git", "python3", "pip3",
            "docker", "curl", "wget",
            "node", "npm",
            "apt", "apt-get", "dnf", "yum", "pacman",
            "brew",
            "tar", "gzip", "unzip",
            "sed", "awk", "grep",
        ]
    return [t for t in candidates if shutil.which(t)]


__all__ = ["bootstrap_environment"]