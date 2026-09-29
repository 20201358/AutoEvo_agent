"""CLI 提示符格式化。

输入提示：``[v3_0c7c] <cwd>> ``（默认无会话时 ``v3 <cwd>> ``）
Agent 输出前缀：``🤖[v3_0c7c]：``

提示符里只保留 4 位短 id（带 v3_ 前缀），不带长标题，避免遮挡输入。
"""

from __future__ import annotations

import os
from typing import Optional

from ..platform_utils import shell_session_status


SHORT_ID_LEN = 4  # 用户指定的"四位数"


def short_id(full_id: str) -> str:
    """从完整 uuid 里取前 SHORT_ID_LEN 位。"""
    if not full_id:
        return "----"
    return full_id[:SHORT_ID_LEN]


def format_session_tag(full_id: Optional[str], title: str = "") -> str:
    """``[v3_0c7c]``。title 参数保留以兼容旧调用，但不再进提示符。"""
    return f"[v3_{short_id(full_id or '')}]"


def cwd() -> str:
    """当前工作目录（短形式）。

    优先读持久化 shell 的 cwd（与 cd 等命令后的实际位置一致），
    shell 未启动时回退到进程 cwd。
    """
    try:
        st = shell_session_status()
        if st.get("active") and st.get("cwd"):
            return st["cwd"]
    except Exception:
        pass
    try:
        return os.getcwd()
    except OSError:
        return "?"


def input_prompt(full_id: Optional[str], title: str = "") -> str:
    """用户输入提示符。"""
    return f"{format_session_tag(full_id)} {cwd()}> "


def agent_prefix(full_id: Optional[str], title: str = "") -> str:
    """Agent 输出前缀（不含换行）：``🤖[v3_0c7c]：``"""
    return f"🤖[v3_{short_id(full_id or '')}]："


def no_session_prompt() -> str:
    """未进入任何会话时的提示符（v3 单独输入时）。"""
    return f"v3 {cwd()}> "
