"""本地开发工具探测工具（让 agent 知道机器上能用什么）。"""

from __future__ import annotations

import json

from ..platform_utils.tools_detect import detect_devtool, detect_summary, find
from ._base import register


@register(category="devtools")
def list_local_devtools(category: str = "all") -> str:
    """列出当前机器上已安装的开发工具（python / git / gcc / docker / 7z 等）。

    在制定「下载→解压→编译→安装」之类的多步计划前，先调用本工具，看看目标工具是否存在。
    返回的 JSON 包含工具类别、绝对路径、版本号，供后续 plan 直接引用。

    Args:
        category: 可选，只看某一类：
            runtime | compiler | build | package | vcs |
            container | network | archive | document | media | os_pkg
            不传则返回全部。
    """
    cats = None if category in ("", "all") else [c.strip() for c in category.split(",") if c.strip()]
    rows = detect_devtool(categories=cats)
    return json.dumps({"count": len(rows), "tools": rows}, ensure_ascii=False, indent=2)


@register(category="devtools")
def find_tool(binary: str) -> str:
    """查找某个可执行文件在当前机器的绝对路径（不在 PATH 上的会返回 null）。

    Args:
        binary: 可执行文件名，如 "python.exe"、"7z"、"git"。
    """
    p = find(binary)
    return json.dumps({"binary": binary, "path": p}, ensure_ascii=False)


@register(category="devtools")
def describe_local_env() -> str:
    """人类可读的本地开发环境清单（plan 阶段会注入到提示词里）。"""
    return detect_summary()


# 兼容旧导入
def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "list_local_devtools",
    "find_tool",
    "describe_local_env",
]