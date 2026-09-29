"""上下文工程。

三件事：

1. **token 估算**：用 tiktoken 近似（模型未知时按字符数估算），用于预算控制。
2. **消息裁剪**：按「消息组」从新到旧装入预算，且不会切断
   ``AIMessage(tool_calls) -> ToolMessage*`` 的配对关系。
3. **历史摘要**：历史过长时把较早的部分压缩成一段摘要，保留信息但省 token。
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from ..config import settings

log = logging.getLogger(__name__)


# ============================================================
# token 估算
# ============================================================

_ENCODER = None
_ENCODER_FAILED = False


def _get_encoder():
    global _ENCODER, _ENCODER_FAILED
    if _ENCODER is not None or _ENCODER_FAILED:
        return _ENCODER
    try:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODER_FAILED = True
        _ENCODER = None
    return _ENCODER


def count_tokens(text: str) -> int:
    """估算文本的 token 数。"""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    # 兜底：中文约 1 token/字，英文约 1 token/4 字符
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return int(ascii_chars / 4) + non_ascii


def message_tokens(msg: BaseMessage, extra: int = 0) -> int:
    """估算单条消息的 token（含工具调用开销）。"""
    total = 4 + extra
    content = msg.content
    if isinstance(content, str):
        total += count_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                total += count_tokens(str(part.get("text", "")))
            else:
                total += count_tokens(str(part))
    else:
        total += count_tokens(str(content))

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        total += count_tokens(str(tool_calls))
    if getattr(msg, "name", None):
        total += count_tokens(str(msg.name))
    return total


def messages_tokens(messages: Iterable[BaseMessage]) -> int:
    return sum(message_tokens(m) for m in messages)


# ============================================================
# 消息分组与清洗
# ============================================================

def group_messages(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    """把消息按「工具调用回合」分组。

    一个 ``AIMessage`` 若带 ``tool_calls``，则它和紧随其后、与之配对的
    ``ToolMessage`` 属于同一组，裁剪时不可拆散。
    """
    groups: list[list[BaseMessage]] = []
    pending_ids: set[str] = set()
    current: list[BaseMessage] = []

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            if current:
                groups.append(current)
            current = [msg]
            pending_ids = {tc.get("id") for tc in msg.tool_calls if tc.get("id")}
            continue

        if isinstance(msg, ToolMessage):
            if current and pending_ids:
                current.append(msg)
                pending_ids.discard(msg.tool_call_id)
                if not pending_ids:
                    groups.append(current)
                    current = []
                continue
            # 孤儿 ToolMessage：单独成组，稍后会清理
            if current:
                groups.append(current)
            groups.append([msg])
            current = []
            pending_ids = set()
            continue

        if current:
            groups.append(current)
            current = []
            pending_ids = set()

        groups.append([msg])

    if current:
        groups.append(current)

    return groups


def sanitize_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """移除孤立的 ToolMessage（没有对应 tool_call 的），避免部分模型报错。"""
    valid_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc.get("id"):
                    valid_ids.add(tc["id"])

    out: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.tool_call_id not in valid_ids:
            continue
        out.append(msg)
    return out


# ============================================================
# 裁剪
# ============================================================

def trim_messages(
    messages: Sequence[BaseMessage],
    max_tokens: Optional[int] = None,
    keep_recent: Optional[int] = None,
) -> list[BaseMessage]:
    """在 token 预算内保留尽可能多的近期消息。

    Args:
        messages: 已按时间排序的消息（不含 system）。
        max_tokens: 预算；默认取 ``settings.CONTEXT_MAX_TOKENS``。
        keep_recent: 至少保留的最近消息条数。

    Returns:
        裁剪后的消息列表（保持原顺序）。
    """
    max_tokens = max_tokens or settings.CONTEXT_MAX_TOKENS
    keep_recent = keep_recent if keep_recent is not None else settings.CONTEXT_KEEP_RECENT

    if not messages:
        return []

    clean = sanitize_messages(messages)
    groups = group_messages(clean)
    if not groups:
        return []

    # 先必须满足 keep_recent 条
    must_have: list[list[BaseMessage]] = []
    count = 0
    for grp in reversed(groups):
        must_have.insert(0, grp)
        count += len(grp)
        if count >= keep_recent:
            break

    used = messages_tokens([m for g in must_have for m in g])

    # 在预算内继续向前装载
    selected: list[list[BaseMessage]] = list(must_have)
    start = len(groups) - len(must_have)

    for i in range(start - 1, -1, -1):
        grp = groups[i]
        cost = messages_tokens(grp)
        if used + cost > max_tokens:
            break
        selected.insert(0, grp)
        used += cost

    return [m for grp in selected for m in grp]


# ============================================================
# 历史摘要
# ============================================================

SUMMARY_SYSTEM = """你是一个对话压缩器。请把给定的一段对话历史压缩成简洁的中文摘要。

要求：
- 保留：用户的目标与要求、已经完成的动作与结果、关键的文件路径/命令/变量、
  尚未解决的问题、用户表达的偏好与约束、已经确认的决策。
- 丢弃：寒暄、重复的解释、冗长的工具输出细节、失败后已放弃的尝试。
- 用要点列表，控制在 400 字以内。
- 只输出摘要正文，不要任何前言。"""


def needs_summary(messages: Sequence[BaseMessage], trigger: Optional[int] = None) -> bool:
    """判断是否需要压缩历史。"""
    trigger = trigger or settings.CONTEXT_SUMMARY_TRIGGER
    return len(sanitize_messages(messages)) > trigger


def summarize_history(
    llm,
    messages: Sequence[BaseMessage],
    existing_summary: str = "",
    keep_recent: Optional[int] = None,
) -> tuple[str, int]:
    """把较早的历史压缩成摘要。

    Args:
        llm: 用于摘要的模型。
        messages: 完整历史（不含 system）。
        existing_summary: 已有摘要，会被一起压缩进去。
        keep_recent: 保留最近多少条不压缩。

    Returns:
        ``(new_summary, consumed_count)``。``consumed_count`` 是被纳入摘要的
        消息条数（调用方据此设置 ``summarized_upto``）。
    """
    keep_recent = keep_recent if keep_recent is not None else settings.CONTEXT_KEEP_RECENT
    clean = sanitize_messages(messages)

    if len(clean) <= keep_recent + 2:
        return existing_summary, 0

    older = clean[: len(clean) - keep_recent]
    if existing_summary:
        head = f"（已有摘要）\n{existing_summary}\n\n（以下是新增的历史）"
    else:
        head = "（以下是历史对话）"

    transcript_lines: list[str] = []
    for msg in older:
        role = {
            "human": "用户",
            "ai": "助手",
            "tool": "工具",
            "system": "系统",
        }.get(msg.type, msg.type)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        content = " ".join(content.split())
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            calls = ", ".join(
                f"{tc.get('name')}({tc.get('args')})" for tc in msg.tool_calls[:3]
            )
            content = (content + f" [调用工具: {calls}]").strip()
        if len(content) > 400:
            content = content[:400] + "…"
        if content:
            transcript_lines.append(f"{role}: {content}")

    transcript = "\n".join(transcript_lines[-120:])

    try:
        from langchain_core.messages import HumanMessage as HM

        resp = llm.invoke([SystemMessage(content=SUMMARY_SYSTEM), HM(content=f"{head}\n\n{transcript}")])
        summary = resp.content if isinstance(resp.content, str) else str(resp.content)
        summary = summary.strip()
        if not summary:
            raise ValueError("空摘要")
    except Exception as e:
        log.warning("历史摘要失败，退化为截断: %s", e)
        summary = existing_summary + ("\n" if existing_summary else "") + transcript[-1500:]

    return summary, len(older)


def build_removals(messages: Sequence[BaseMessage], upto: int) -> list[RemoveMessage]:
    """构造删除前 ``upto`` 条消息的 RemoveMessage 列表。

    注意：与 ``add_messages`` 归约器配合使用；删除后请确保
    ``summarized_upto`` 同步更新。
    """
    out: list[RemoveMessage] = []
    for msg in messages[:upto]:
        if getattr(msg, "id", None):
            out.append(RemoveMessage(id=msg.id))
    return out


__all__ = [
    "count_tokens",
    "message_tokens",
    "messages_tokens",
    "group_messages",
    "sanitize_messages",
    "trim_messages",
    "needs_summary",
    "summarize_history",
    "build_removals",
]
