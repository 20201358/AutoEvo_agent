"""工具自动注册基础设施。

新增工具的流程：

    1. 在 ``v3/tools/`` 下新建一个 ``.py`` 文件
    2. 用 ``@register(category=...)`` 装饰函数（取代 ``@tool``）
    3. 重启进程即可，``v3.tools.discover()`` 会自动扫描并 import

示例：

    from v3.tools._base import register

    @register(category="devtools", risk="low")
    def probe_python(path: str = "python") -> str:
        '''查看某路径上的 Python 解释器信息。'''
        ...

设计要点：
    - 用装饰器同时产出 ``StructuredTool``（给 LangGraph 用）与原函数（方便单测直接调用）
    - 通过 ``pkgutil.iter_modules`` 自动 import 整个 ``v3.tools`` 包，触发所有装饰器
    - 通过 ``category`` 与 ``requires_confirm`` 让 graph 在不同风险下自动加 confirm
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from langchain_core.tools import StructuredTool

log = logging.getLogger(__name__)

# ============================================================
# 数据结构
# ============================================================

@dataclass
class RegisteredTool:
    """一条工具注册记录。"""

    name: str
    tool: StructuredTool
    fn: Callable
    category: str = "general"
    requires_confirm: bool = False
    risk: str = "low"
    """low | medium | high —— high 的步骤在 plan 阶段就要警示用户。"""
    module: str = ""
    description: str = ""

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        """兼容：直接调用底层 StructuredTool。"""
        return self.tool.invoke(*args, **kwargs)



# ============================================================
# 注册表
# ============================================================

_REGISTRY: dict[str, RegisteredTool] = {}
_LOCK = threading.RLock()

# 由图节点特殊处理的工具（不走普通执行路径）。
# 放在 _base 里避免 v3.tools.__init__ 半初始化时的循环导入。
INTERCEPTED_TOOLS = {"ask_user", "update_plan", "finish_task"}


def register(
    name: Optional[str] = None,
    *,
    description: str = "",
    category: str = "general",
    requires_confirm: bool = False,
    risk: str = "low",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """工具装饰器，同时充当 ``@tool``。

    Args:
        name: 工具名；缺省取函数名。
        description: 工具描述；缺省取 ``__doc__``。
        category: 工具分类（用于 prompt 分组 / 仪表盘）。
        requires_confirm: 是否默认需要用户确认（高风险工具）。
        risk: low / medium / high —— 在 plan 阶段会写到提示词里警示用户。
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        doc = (fn.__doc__ or "").strip()
        desc = (description or doc).strip()

        # 包装成 StructuredTool（LangChain 的 LLM 工具协议）
        tool = StructuredTool.from_function(
            func=fn,
            name=tool_name,
            description=desc,
            parse_docstring=True,
        )

        with _LOCK:
            existing = _REGISTRY.get(tool_name)
            if existing and existing.module != fn.__module__:
                log.warning(
                    "register: 工具 %s 已被 %s 注册，正在被 %s 覆盖",
                    tool_name, existing.module, fn.__module__,
                )
            _REGISTRY[tool_name] = RegisteredTool(
                name=tool_name,
                tool=tool,
                fn=fn,
                category=category,
                requires_confirm=requires_confirm,
                risk=risk,
                module=fn.__module__,
                description=desc,
            )

        # 返回原函数，import 时能直接调用做单测
        return fn

    return deco



def _all() -> dict[str, RegisteredTool]:
    """返回当前已注册的工具（拷贝）。"""
    with _LOCK:
        return dict(_REGISTRY)



def get(name: str) -> Optional[RegisteredTool]:
    """按名取一个注册项。"""
    with _LOCK:
        return _REGISTRY.get(name)


def by_category(category: str) -> list[RegisteredTool]:
    """按类别筛选。"""
    with _LOCK:
        return [r for r in _REGISTRY.values() if r.category == category]


def high_risk() -> list[RegisteredTool]:
    """返回所有高风险工具。"""
    with _LOCK:
        return [r for r in _REGISTRY.values() if r.risk == "high"]


def reset() -> None:
    """清空注册表（测试用）。"""
    with _LOCK:
        _REGISTRY.clear()



# ============================================================
# 自动发现：import v3.tools.* 下所有模块
# ============================================================

_DISCOVER_LOCK = threading.RLock()
"""用 RLock：discover() 过程中 import 子模块时，子模块可能再触发
discover_if_needed()（例如 registry.py 模块级调用），可重入避免死锁。"""
_DISCOVERED = False


def discover(package_path: str = "v3.tools", force: bool = False) -> list[RegisteredTool]:
    """扫描并 import 包内所有模块，触发装饰器。

    自动跳过以下文件：
        - ``_base``（本文件）
        - 以 ``_`` 开头的私有模块
        - ``registry``（兼容层，它内部会反过来读注册表）
    """
    global _DISCOVERED
    with _DISCOVER_LOCK:
        if _DISCOVERED and not force:
            return list(_all().values())

        pkg = importlib.import_module(package_path)
        for info in pkgutil.iter_modules(getattr(pkg, "__path__", [])):
            mod_name = info.name
            if mod_name.startswith("_") or mod_name in {"registry"}:
                continue
            full = f"{package_path}.{mod_name}"
            try:
                importlib.import_module(full)
            except Exception as e:
                log.warning("discover: 跳过 %s: %s", full, e)

        _DISCOVERED = True
        return list(_all().values())


def discover_if_needed() -> None:
    """首次调用时触发发现。"""
    if not _DISCOVERED:
        discover()


# ============================================================
# 公共 API
# ============================================================

def get_tools() -> list[StructuredTool]:
    """返回所有工具的 LangChain 列表（按类别、名称排序）。"""
    discover_if_needed()
    items = sorted(_all().values(), key=lambda r: (r.category, r.name))
    return [r.tool for r in items]


def get_tool_map() -> dict[str, StructuredTool]:
    discover_if_needed()
    return {name: r.tool for name, r in _all().items()}


def get_registered_map() -> dict[str, RegisteredTool]:
    discover_if_needed()
    return dict(_all())


def describe() -> str:
    """人类可读的工具说明（用于提示词）。"""
    discover_if_needed()
    items = sorted(_all().values(), key=lambda r: (r.category, r.name))
    if not items:
        return "(无可用工具)"
    by_cat: dict[str, list[RegisteredTool]] = {}
    for r in items:
        by_cat.setdefault(r.category, []).append(r)
    lines: list[str] = []
    for cat, group in by_cat.items():
        lines.append(f"## {cat}")
        for r in group:
            desc = (r.description or "").splitlines()[0]
            tag = ""
            if r.risk == "high":
                tag = " ⚠高风险"
            elif r.risk == "medium":
                tag = " ⚠中等风险"
            if r.requires_confirm:
                tag += "（执行前需确认）"
            lines.append(f"- {r.name}{tag}: {desc}")
        lines.append("")
    return "\n".join(lines).strip()


def is_high_risk(name: str) -> bool:
    """快速判断是否高风险。"""
    r = get(name)
    return bool(r and r.risk == "high")


def requires(name: str) -> bool:
    """是否默认需要确认。"""
    r = get(name)
    return bool(r and r.requires_confirm)


__all__ = [
    "RegisteredTool",
    "register",
    "INTERCEPTED_TOOLS",
    "get",
    "by_category",
    "high_risk",
    "discover",
    "discover_if_needed",
    "reset",
    "get_tools",
    "get_tool_map",
    "get_registered_map",
    "describe",
    "is_high_risk",
    "requires",
]