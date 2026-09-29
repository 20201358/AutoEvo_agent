"""安全策略执行层。

把 ``bootstrap.safety_defaults`` 里定义的静态策略应用到具体操作上：

    check_command(cmd, policy)   -> Verdict
    check_path(path, policy, mode) -> Verdict

Verdict.decision 取值：
    - "allow"   直接执行
    - "confirm" 需要用户确认（由图的 confirm 节点通过 interrupt 完成）
    - "deny"    直接拒绝，不执行

设计原则：宁可多问一次，也不要静默执行破坏性操作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional


Decision = Literal["allow", "confirm", "deny"]


@dataclass
class Verdict:
    """一次安全检查的结论。"""

    decision: Decision = "allow"
    reason: str = ""
    rule: Optional[str] = None
    matched: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    @property
    def needs_confirmation(self) -> bool:
        return self.decision == "confirm"


# ============================================================
# 命令检查
# ============================================================

def check_command(cmd: str, policy: dict) -> Verdict:
    """检查 shell 命令是否允许执行。"""
    if not cmd or not cmd.strip():
        return Verdict("allow", "空命令")

    mode = (policy or {}).get("mode", "normal")

    # yolo 模式：仍然查黑名单，但不要求确认（除提权外）
    denied: Iterable[str] = (policy or {}).get("denied_patterns") or []
    for pattern in denied:
        try:
            m = re.search(pattern, cmd, flags=re.IGNORECASE)
        except re.error:
            continue
        if m:
            return Verdict(
                decision="deny",
                reason=f"命中危险命令黑名单：{pattern}",
                rule=pattern,
                matched=m.group(0),
            )

    # 提权
    confirm_tokens: Iterable[str] = (policy or {}).get("require_confirmation") or []
    for token in confirm_tokens:
        if token and token.lower() in cmd.lower():
            if mode == "strict" or not (policy or {}).get("allow_sudo", False):
                return Verdict(
                    decision="confirm",
                    reason=f"命令包含提权操作「{token}」，需要确认",
                    rule=f"confirm:{token}",
                    matched=token,
                )
            return Verdict(
                decision="confirm",
                reason=f"命令包含提权操作「{token}」，请确认",
                rule=f"confirm:{token}",
                matched=token,
            )

    # 网络操作
    if not (policy or {}).get("allow_network", True):
        net_tokens = (
            "curl", "wget", "Invoke-WebRequest", "iwr", "Invoke-RestMethod",
            "ssh", "scp", "ftp", "git clone", "npm install", "pip install",
        )
        for token in net_tokens:
            if token.lower() in cmd.lower():
                return Verdict(
                    decision="deny",
                    reason=f"当前策略禁止网络操作，命中「{token}」",
                    rule="network",
                    matched=token,
                )

    # strict 模式下写操作也要确认
    if mode == "strict":
        write_tokens = (
            "rm ", "del ", "rmdir", "Remove-Item", "mv ", "move ",
            "set-content", "out-file", ">", ">>", "tee",
        )
        for token in write_tokens:
            if token.lower() in cmd.lower():
                return Verdict(
                    decision="confirm",
                    reason=f"strict 模式下写操作需要确认（命中「{token.strip()}」）",
                    rule="strict-write",
                    matched=token,
                )

    return Verdict("allow", "通过命令安全检查")


# ============================================================
# 路径检查
# ============================================================

_SENSITIVE_PREFIXES_WINDOWS = (
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\programdata",
)

_SENSITIVE_PREFIXES_UNIX = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/sys",
    "/proc",
    "/var/lib",
)


def check_path(path: str, policy: dict, mode: Literal["read", "write"]) -> Verdict:
    """检查文件路径操作是否允许。

    Args:
        path: 目标路径。
        policy: SafetyPolicy 字典。
        mode: "read" 或 "write"。
    """
    if not path:
        return Verdict("deny", "路径为空")

    try:
        p = Path(path).expanduser().resolve()
    except Exception as e:
        return Verdict("deny", f"路径无法解析: {e}")

    text = str(p).lower()
    is_windows = ":" in text[:3]

    sensitive = _SENSITIVE_PREFIXES_WINDOWS if is_windows else _SENSITIVE_PREFIXES_UNIX
    for prefix in sensitive:
        if text.startswith(prefix):
            if mode == "read":
                return Verdict(
                    decision="confirm",
                    reason=f"读取系统目录需要确认：{p}",
                    rule=f"sensitive:{prefix}",
                    matched=str(p),
                )
            return Verdict(
                decision="deny",
                reason=f"禁止写入系统目录：{p}",
                rule=f"sensitive:{prefix}",
                matched=str(p),
            )

    if mode == "write" and not (policy or {}).get("allow_write", True):
        return Verdict("deny", "当前策略禁止写操作", rule="write-disabled")

    # 白名单：写操作默认限定在 cwd / 用户目录 / 临时目录内
    if mode == "write":
        allowed = (policy or {}).get("allowed_paths") or []
        if allowed:
            from .platform_utils.paths import is_subpath

            home = str(Path.home())
            if not any(is_subpath(str(p), a) for a in allowed) and not is_subpath(
                str(p), home
            ):
                return Verdict(
                    decision="confirm",
                    reason=(
                        f"写入工作目录/用户目录之外的路径需要确认：{p}"
                    ),
                    rule="outside-allowed-paths",
                    matched=str(p),
                    extra={"allowed_paths": list(allowed)},
                )

    return Verdict("allow", "通过路径安全检查")


__all__ = ["Verdict", "Decision", "check_command", "check_path"]
