"""工具层入口。

工具注册流程：

    v3/tools/<name>.py 文件里，用 ``@register(category=..., risk=..., requires_confirm=...)``
    装饰函数即可。本文件 ``import v3.tools`` 时会触发 ``discover()``，自动扫描所有
    子模块并执行它们的装饰器，所以新工具只要放进 ``v3/tools/`` 文件夹即可被自动识别。

旧的 ``from v3.tools import get_tools, get_tool_map, ..." 接口保留，便于迁移。
"""

from __future__ import annotations

from ..tools._base import (
    INTERCEPTED_TOOLS,
    RegisteredTool,
    describe,
    discover,
    discover_if_needed,
    get,
    get_registered_map,
    get_tool_map,
    get_tools,
    high_risk,
    is_high_risk,
    register,
    requires,
    reset,
)

# 触发自动发现（import v3.tools 就把所有子模块 import 进来）
discover()


# 重新导出旧工具（按分组）。tools.py 文件本身在被 discover() 时
# 就把它们的注册项写进 _REGISTRY 了，这里只是给 ``from v3.tools import run_shell``
# 这种老写法一个兼容入口。
from . import file_tools as _file_tools  # noqa: F401, E402
from . import kb_tools as _kb_tools  # noqa: F401, E402
from . import shell_tool as _shell_tool  # noqa: F401, E402
from . import skill_tools as _skill_tools  # noqa: F401, E402
from . import task_tools as _task_tools  # noqa: F401, E402


# ============================================================
# 兼容旧接口
# ============================================================

def get_tool(name: str):
    """按名字取一个 LangChain 工具对象。"""
    r = get(name)
    return r.tool if r else None


def tool_names() -> list[str]:
    """所有已注册工具名（按类别、名字排序）。"""
    items = sorted(get_registered_map().values(), key=lambda r: (r.category, r.name))
    return [r.name for r in items]


def high_risk_tool_names() -> list[str]:
    """高风险工具名列表（用于 plan 警示）。"""
    return [r.name for r in high_risk()]


def confirm_categories() -> set[str]:
    """所有默认需要确认的工具。"""
    return {r.name for r in get_registered_map().values() if r.requires_confirm}


# ============================================================
# 旧 registry.py 的几个常量（供 graph/nodes/tools.py 等位置继续用）
# ============================================================

# 由图节点特殊处理的工具（不走普通执行路径）
INTERCEPTED_TOOLS = {"ask_user", "update_plan", "finish_task"}


__all__ = [
    # 装饰器 + 元数据
    "register",
    "RegisteredTool",
    # 入口
    "get_tools",
    "get_tool_map",
    "get_tool",
    "get_registered_map",
    "describe",
    "tool_names",
    "high_risk_tool_names",
    "confirm_categories",
    "is_high_risk",
    "requires",
    "discover",
    "discover_if_needed",
    "reset",
    # 兼容
    "INTERCEPTED_TOOLS",
]