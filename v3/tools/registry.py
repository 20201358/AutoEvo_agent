"""工具注册表（兼容层）。

历史接口 ``v3.tools.registry`` 仍然可用；所有数据来自 ``_base`` 的自动发现。
新代码请直接 ``from v3.tools import get_tools, get_tool_map, describe"``。
"""

from __future__ import annotations

from typing import Optional

from ._base import (
    RegisteredTool,
    describe as _describe,
    discover as _discover,
    discover_if_needed as _discover_if_needed,
    get as _get,
    get_registered_map as _get_registered_map,
    get_tool_map as _get_tool_map,
    get_tools as _get_tools,
    high_risk as _high_risk,
    is_high_risk as _is_high_risk,
    requires as _requires,
    reset as _reset,
)


# ============================================================
# 类别分组（按工具的 category 字段聚合）
# ============================================================

def _build_groups() -> dict[str, list]:
    """按类别分组所有工具。"""
    items = _get_registered_map()
    out: dict[str, list] = {}
    for name, r in items.items():
        out.setdefault(r.category, []).append(r.tool)
    return out


# 由图节点特殊处理的工具（不走普通执行路径）
INTERCEPTED_TOOLS = {"ask_user", "update_plan", "finish_task"}


def get_tools(
    include_rag: bool = True,
    include_skills: bool = True,
) -> list:
    """返回主 agent 可用的工具列表（向后兼容）。"""
    items = _get_registered_map()
    out = []
    for name, r in items.items():
        if r.category == "knowledge" and not include_rag:
            continue
        if r.category == "skills" and not include_skills:
            continue
        out.append(r.tool)
    return out


def get_tool_map(
    include_rag: bool = True,
    include_skills: bool = True,
) -> dict:
    """返回 name -> tool 的映射。"""
    return {t.name: t for t in get_tools(include_rag=include_rag, include_skills=include_skills)}


def get_tool(name: str):
    """按名称取一个工具。"""
    r = _get(name)
    return r.tool if r else None


def describe_tools(tools: Optional[list] = None) -> str:
    """生成工具清单文本（不含参数细节，细节由模型从 schema 获取）。"""
    if tools is None:
        return _describe()
    # 兼容：当外部传入一组工具时，按 register 分组
    names = {t.name for t in tools}
    items = _get_registered_map()
    by_cat: dict[str, list[RegisteredTool]] = {}
    for name, r in items.items():
        if name in names:
            by_cat.setdefault(r.category, []).append(r)
    if not by_cat:
        return "(无可用工具)"
    lines = []
    for cat, group in by_cat.items():
        lines.append(f"## {cat}")
        for r in group:
            desc = (r.description or "").splitlines()[0]
            lines.append(f"- {r.name}: {desc}")
        lines.append("")
    return "\n".join(lines).strip()


def tool_names(tools: Optional[list] = None) -> list[str]:
    """返回工具名列表。"""
    if tools is None:
        return sorted(_get_registered_map().keys())
    return [t.name for t in tools]


# 兼容旧字段，graph/nodes/tools.py 仍在用
CONFIRM_TOOLS: set[str] = set()
"""默认需要确认的工具名集合。"""


def refresh_confirm_tools() -> set[str]:
    """刷新并返回所有默认需要确认的工具名。"""
    global CONFIRM_TOOLS
    CONFIRM_TOOLS = {name for name, r in _get_registered_map().items() if r.requires_confirm}
    return CONFIRM_TOOLS


# 模块加载时立即建表
refresh_confirm_tools()


__all__ = [
    "TOOL_GROUPS" if False else "TOOL_GROUPS",  # 兼容旧 import
    "CONFIRM_TOOLS",
    "INTERCEPTED_TOOLS",
    "get_tools",
    "get_tool_map",
    "get_tool",
    "describe_tools",
    "tool_names",
    "refresh_confirm_tools",
    "RegisteredTool",
    "is_high_risk",
    "requires",
    "discover",
    "discover_if_needed",
    "reset",
    "high_risk",
]


# 让 ``TOOL_GROUPS`` 在被 import 时才计算
def __getattr__(name: str):
    if name == "TOOL_GROUPS":
        return _build_groups()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")