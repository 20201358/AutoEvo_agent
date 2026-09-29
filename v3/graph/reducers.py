"""LangGraph 状态归约器。

``merge_dict`` 支持一个特殊约定：当右值带有 ``"__reset__": True`` 时，
表示「先清空再合并」，用于把累计字典重置掉（删掉 ``__reset__`` 键本身）。
"""

from __future__ import annotations

RESET_KEY = "__reset__"


def merge_dict(left: dict, right: dict) -> dict:
    """浅合并字典，right 覆盖 left 的同名键。

    若 ``right`` 含 ``RESET_KEY``，则丢弃 ``left`` 的全部内容（等价于重置）。
    """
    right = right or {}
    if right.get(RESET_KEY):
        return {k: v for k, v in right.items() if k != RESET_KEY}
    return {**(left or {}), **right}


def keep_last(left, right):
    """只保留最新值。"""
    return right


def append_unique(left: list, right: list) -> list:
    """追加并去重（按值比较），保持顺序。"""
    seen = set()
    result = []
    for item in list(left or []) + list(right or []):
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def extend_list(left: list, right: list) -> list:
    """单纯拼接。"""
    return list(left or []) + list(right or [])


def reset_dict(new: dict | None = None) -> dict:
    """构造一个重置指令（供节点返回）。"""
    payload = {RESET_KEY: True}
    if new:
        payload.update(new)
    return payload


__all__ = [
    "RESET_KEY",
    "merge_dict",
    "keep_last",
    "append_unique",
    "extend_list",
    "reset_dict",
]
