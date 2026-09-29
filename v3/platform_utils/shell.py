"""Shell 相关。"""

from __future__ import annotations

import os
import shutil
import sys


def default_shell() -> str:
    if sys.platform == "win32":
        for name in ("pwsh.exe", "powershell.exe"):
            path = shutil.which(name)
            if path:
                return path
        return os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    return os.environ.get("SHELL", "/bin/sh")


def is_powershell(shell_path: str | None) -> bool:
    if not shell_path:
        return False
    lower = shell_path.lower()
    return "powershell" in lower or "pwsh" in lower


def build_command(cmd: str, shell_path: str | None) -> list[str] | str:
    if sys.platform == "win32":
        if is_powershell(shell_path):
            return [shell_path, "-NoProfile", "-NonInteractive", "-Command", cmd]
        return cmd
    return cmd


def wrap_encoding(cmd: str, shell_path: str | None) -> str:
    if sys.platform != "win32":
        return cmd
    if is_powershell(shell_path):
        return "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + cmd
    return f"chcp 65001 >nul && {cmd}"


__all__ = ["default_shell", "is_powershell", "build_command", "wrap_encoding"]