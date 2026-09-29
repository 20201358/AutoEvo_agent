"""confirm 节点：human-in-the-loop。

用 LangGraph 的 ``interrupt()`` 把控制权交回调用方（CLI / Web），
用户答复后用 ``Command(resume=...)`` 恢复。

只要存在挂起项，``tools`` 节点就不写任何 ToolMessage，所以本节点被重新执行
（interrupt 恢复时会重跑节点）也不会造成重复副作用。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

log = logging.getLogger(__name__)


_YES = {"y", "yes", "true", "1", "是", "确认", "同意", "允许", "执行", "可以", "好", "ok"}
_NO = {"n", "no", "false", "0", "否", "拒绝", "不要", "取消", "跳过", "不行", "no"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _YES:
            return True
        if low in _NO:
            return False
    return bool(value)


def confirm_node(state: dict) -> dict:
    """暂停并把待确认/待回答的内容交给用户。"""
    payload = state.get("pending_confirmation") or {}
    items = payload.get("items") or []

    if not items:
        return {"pending_confirmation": None}

    kind = payload.get("kind") or "confirm"

    # 抛出 interrupt，调用方会收到 {"__interrupt__": [Interrupt(value=payload)]}
    answer = interrupt(payload)

    resolved: dict[str, bool] = {}
    answers: dict[str, str] = {}

    if kind == "ask":
        cid = items[0].get("tool_call_id") or ""
        if isinstance(answer, dict):
            # {tool_call_id: text}
            for k, v in answer.items():
                answers[str(k)] = str(v)
        elif isinstance(answer, (list, tuple)):
            for item, val in zip(items, answer):
                answers[str(item.get("tool_call_id") or "")] = str(val)
        else:
            answers[cid] = str(answer)
        log.info("confirm: 收到用户回答")

    else:
        if isinstance(answer, dict):
            # 允许 {tool_call_id: bool} 或 {"all": bool}
            if "all" in answer:
                flag = _to_bool(answer["all"])
                for item in items:
                    resolved[str(item.get("tool_call_id") or "")] = flag
            else:
                for item in items:
                    cid = str(item.get("tool_call_id") or "")
                    if cid in answer:
                        resolved[cid] = _to_bool(answer[cid])
                    else:
                        resolved[cid] = False
        elif isinstance(answer, (list, tuple)):
            for item, val in zip(items, answer):
                resolved[str(item.get("tool_call_id") or "")] = _to_bool(val)
        else:
            flag = _to_bool(answer)
            for item in items:
                resolved[str(item.get("tool_call_id") or "")] = flag
        log.info("confirm: 用户决定 %s", resolved)

    return {
        "pending_confirmation": None,
        "resolved_confirmations": resolved,
        "user_answers": answers,
    }


__all__ = ["confirm_node"]
