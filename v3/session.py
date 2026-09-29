"""运行时会话。

把「图 + 检查点 + 对话存储」封装成面向交互的 API：

    session = AgentSession.open()            # 或 AgentSession.new("标题")
    result = session.send("把 x 改成 y")      # 一轮交互
    if result.interrupt:                     # 需要确认/回答
        result = session.resume("是")

也提供流式接口 ``session.stream(text)``，逐节点产出事件，便于 CLI / Web 实时展示。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.types import Command

from .bootstrap import bootstrap_all
from .config import settings
from .graph.builder import build_graph
from .graph.context import needs_summary, summarize_history
from .graph.llm import get_planner_llm
from .memory import (
    Conversation,
    get_checkpointer,
    get_conversation_store,
)

log = logging.getLogger(__name__)


# ============================================================
# 结果
# ============================================================

@dataclass
class TurnResult:
    """一轮交互的结果。"""

    conversation_id: str
    final_text: str = ""
    messages: list[BaseMessage] = field(default_factory=list)
    interrupt: Optional[dict] = None
    plan: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    end_reason: Optional[str] = None
    task_success: bool = True
    error: Optional[str] = None
    state: dict = field(default_factory=dict)
    # 当 plan_confirm 把本轮判定为「切换任务」时，把新任务内容带回来
    # CLI 检测到非空就开新会话跑这个内容
    switch_to_task: Optional[str] = None

    @property
    def needs_input(self) -> bool:
        return self.interrupt is not None

    @property
    def has_proposals(self) -> bool:
        return bool(self.proposals)

    @property
    def task_switched(self) -> bool:
        """本轮结束时是否要把当前任务切换为新任务。"""
        return bool(self.switch_to_task)

    def __repr__(self) -> str:
        kind = "interrupt" if self.needs_input else (self.end_reason or "?")
        return f"<TurnResult conv={self.conversation_id[:8]} {kind} msgs={len(self.messages)}>"


# ============================================================
# 会话
# ============================================================

class AgentSession:
    """一次持续对话。"""

    def __init__(
        self,
        conversation: Conversation,
        graph=None,
        checkpointer=None,
    ):
        settings.ensure_dirs()
        self.conversation = conversation
        self.store = get_conversation_store()
        self.checkpointer = checkpointer or get_checkpointer()
        self.graph = graph or build_graph(checkpointer=self.checkpointer)
        self.environment, self.safety = bootstrap_all()
        self.pending_interrupt: Optional[dict] = None
        self._persisted = 0

    # ---------------- 构造 ----------------

    @classmethod
    def new(cls, title: str = "", **kwargs) -> "AgentSession":
        """新建一个会话。"""
        store = get_conversation_store()
        conv = store.create(title=title)
        log.info("新建会话 %s", conv.id)
        return cls(conv, **kwargs)

    @classmethod
    def open(cls, conversation_id: Optional[str] = None, **kwargs) -> "AgentSession":
        """打开已有会话；``conversation_id`` 为空时取最近一个，没有则新建。"""
        store = get_conversation_store()
        if conversation_id:
            conv = store.get(conversation_id)
            if conv is None:
                conv = store.get_or_create(conversation_id)
        else:
            conv = store.latest() or store.create()
        session = cls(conv, **kwargs)
        session._sync_pending_from_graph()
        return session

    # ---------------- 配置 ----------------

    @property
    def config(self) -> dict:
        return {
            "configurable": {"thread_id": self.conversation.id},
            "recursion_limit": max(50, settings.MAX_ITERATIONS * 4 + 20),
        }

    @property
    def conversation_id(self) -> str:
        return self.conversation.id

    # ---------------- 状态 ----------------

    def get_state(self) -> dict:
        """读取图当前状态（无检查点时返回空 dict）。"""
        try:
            snap = self.graph.get_state(self.config)
            return dict(snap.values or {})
        except Exception as e:
            log.debug("读取图状态失败: %s", e)
            return {}

    def _sync_pending_from_graph(self) -> None:
        """进程重启后恢复挂起的中断。"""
        try:
            snap = self.graph.get_state(self.config)
        except Exception:
            return
        if snap is None:
            return
        interrupts = getattr(snap, "interrupts", None) or ()
        if interrupts:
            self.pending_interrupt = dict(getattr(interrupts[0], "value", {}) or {})

    # ---------------- 历史消息 ----------------

    def history(self, limit: Optional[int] = None) -> list:
        """读取持久化的消息日志。"""
        return self.store.get_messages(self.conversation.id, limit=limit)

    # ---------------- 主流程 ----------------

    def send(self, text: str) -> TurnResult:
        """发送一条用户输入（若存在挂起中断，则自动作为该中断的答复）。"""
        if self.pending_interrupt is not None:
            return self.resume(text)
        return self._run(text)

    def resume(self, answer: Any) -> TurnResult:
        """回答挂起的确认/提问。"""
        if self.pending_interrupt is None:
            log.warning("当前没有挂起的中断，按新输入处理")
            return self._run(str(answer))
        return self._run(Command(resume=answer), is_resume=True)

    def _run(self, payload, is_resume: bool = False) -> TurnResult:
        """执行图并获得本轮结果。"""
        is_new_turn = not is_resume

        if is_new_turn:
            self.store.append_message(self.conversation.id, "user", str(payload))
            self._maybe_summarize()
            graph_input: dict = {
                "messages": [HumanMessage(content=str(payload))],
                "conversation_id": self.conversation.id,
                "user_id": "local",
                "environment": self.environment,
                "safety": self.safety,
            }
        else:
            graph_input = payload  # Command(resume=...)

        before = len(self._graph_messages())

        try:
            self.graph.invoke(graph_input, self.config)
        except Exception as e:
            log.exception("图执行失败")
            return TurnResult(
                conversation_id=self.conversation.id,
                error=f"{type(e).__name__}: {e}",
                end_reason="error",
                task_success=False,
            )

        state = self.get_state()
        new_messages = self._graph_messages()[before:]
        self._persist(new_messages)

        # 检测中断
        self.pending_interrupt = None
        interrupt_payload: Optional[dict] = None
        try:
            snap = self.graph.get_state(self.config)
            interrupts = getattr(snap, "interrupts", None) or ()
            if interrupts:
                interrupt_payload = dict(getattr(interrupts[0], "value", {}) or {})
                self.pending_interrupt = interrupt_payload
        except Exception:
            pass

        self.store.touch(self.conversation.id)
        if not self.conversation.title:
            title = self.store.auto_title(self.conversation.id)
            if title:
                self.conversation.title = title

        return TurnResult(
            conversation_id=self.conversation.id,
            final_text=(state.get("final_answer") or "").strip(),
            messages=new_messages,
            interrupt=interrupt_payload,
            plan=state.get("plan") or [],
            proposals=state.get("skill_proposals") or [],
            end_reason=state.get("end_reason"),
            task_success=bool(state.get("task_success", True)),
            state=state,
            switch_to_task=state.get("_switch_to_task"),
        )

    # ---------------- 流式 ----------------

    def stream(self, text: str) -> Iterator[dict]:
        """流式执行，逐节点产出事件。

        事件类型：
            {"type": "node", "node": "agent"}
            {"type": "message", "message": AIMessage}
            {"type": "step", "text": "..."}
            {"type": "interrupt", "payload": {...}}
            {"type": "result", "result": TurnResult}
        """
        if self.pending_interrupt is not None:
            yield from self._stream_run(Command(resume=text), is_resume=True)
            return
        self.store.append_message(self.conversation.id, "user", str(text))
        self._maybe_summarize()
        yield from self._stream_run(
            {
                "messages": [HumanMessage(content=str(text))],
                "conversation_id": self.conversation.id,
                "user_id": "local",
                "environment": self.environment,
                "safety": self.safety,
            },
            is_resume=False,
        )

    def _stream_run(self, payload, is_resume: bool) -> Iterator[dict]:
        before = len(self._graph_messages())
        self.pending_interrupt = None
        interrupt_payload: Optional[dict] = None
        # 上一节点结束后的 plan 状态，用于检测步骤状态变化
        prev_plan_by_id: dict = {}
        for msg in self._graph_messages()[-12:]:
            pass  # 占位
        # 直接从当前 state 取一次
        try:
            snap = self.graph.get_state(self.config)
            if snap and snap.values:
                for s in (snap.values.get("plan") or []):
                    if isinstance(s, dict) and s.get("id"):
                        prev_plan_by_id[s["id"]] = s.get("status") or "pending"
        except Exception:
            pass

        try:
            for chunk in self.graph.stream(payload, self.config, stream_mode="updates"):
                for node, update in (chunk or {}).items():
                    if node == "__interrupt__":
                        for itr in update if isinstance(update, (list, tuple)) else [update]:
                            interrupt_payload = dict(getattr(itr, "value", {}) or {})
                        yield {"type": "interrupt", "payload": interrupt_payload}
                        continue

                    # 检测 plan 步骤状态变化（增量事件 plan_step）
                    # 注意：新计划首次生成时（new→pending）不发 plan_step 事件——
                    # 后面 plan_confirm 框会统一展示全部步骤，避免重复渲染。
                    new_plan = (update or {}).get("plan") if isinstance(update, dict) else None
                    if new_plan and isinstance(new_plan, list):
                        # plan 节点同时更新了 plan_revision → 这是新计划，跳过逐条事件
                        is_new_plan = isinstance(update, dict) and "plan_revision" in (update or {})
                        for s in new_plan:
                            if not isinstance(s, dict):
                                continue
                            sid = s.get("id")
                            new_status = s.get("status") or "?"
                            if not sid:
                                continue
                            old_status = prev_plan_by_id.get(sid, "new")
                            if old_status != new_status:
                                # 新计划首次生成（new→pending）跳过，避免和 plan_confirm 重复
                                if is_new_plan and old_status == "new" and new_status == "pending":
                                    prev_plan_by_id[sid] = new_status
                                    continue
                                yield {
                                    "type": "plan_step",
                                    "step_id": sid,
                                    "status": new_status,
                                    "description": s.get("description") or "",
                                    "error": s.get("error"),
                                }
                                prev_plan_by_id[sid] = new_status

                    yield {"type": "node", "node": node, "update": update}

                    if isinstance(update, dict):
                        for msg in update.get("messages") or []:
                            yield {"type": "message", "message": msg}
                        if update.get("plan_note"):
                            yield {"type": "step", "text": f"[plan] {update['plan_note']}"}
                        if update.get("needs_replan"):
                            yield {"type": "step", "text": "[replan] 失败次数超阈值，回到 plan 重新规划"}
        except Exception as e:
            log.exception("流式执行失败")
            yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
            return

        state = self.get_state()
        new_messages = self._graph_messages()[before:]
        self._persist(new_messages)

        if interrupt_payload is None:
            try:
                snap = self.graph.get_state(self.config)
                interrupts = getattr(snap, "interrupts", None) or ()
                if interrupts:
                    interrupt_payload = dict(getattr(interrupts[0], "value", {}) or {})
            except Exception:
                pass

        self.pending_interrupt = interrupt_payload
        self.store.touch(self.conversation.id)
        if not self.conversation.title:
            title = self.store.auto_title(self.conversation.id)
            if title:
                self.conversation.title = title

        yield {
            "type": "result",
            "result": TurnResult(
                conversation_id=self.conversation.id,
                final_text=(state.get("final_answer") or "").strip(),
                messages=new_messages,
                interrupt=interrupt_payload,
                plan=state.get("plan") or [],
                proposals=state.get("skill_proposals") or [],
                end_reason=state.get("end_reason"),
                task_success=bool(state.get("task_success", True)),
                state=state,
                switch_to_task=state.get("_switch_to_task"),
            ),
        }

    # ---------------- 内部 ----------------

    def _graph_messages(self) -> list[BaseMessage]:
        return list(self.get_state().get("messages") or [])

    def _persist(self, messages: list[BaseMessage]) -> None:
        """把新消息写入可读日志（跳过 system 与内部消息）。"""
        for msg in messages:
            role, name, tool_calls, tool_call_id = self._classify(msg)
            if role is None:
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            self.store.append_message(
                self.conversation.id,
                role=role,
                content=content,
                name=name,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
            )

    @staticmethod
    def _classify(msg: BaseMessage):
        if isinstance(msg, HumanMessage):
            return "user", None, [], None
        if isinstance(msg, ToolMessage):
            return "tool", getattr(msg, "name", None), [], msg.tool_call_id
        if isinstance(msg, AIMessage):
            calls = [
                {"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")}
                for tc in (getattr(msg, "tool_calls", None) or [])
            ]
            return "assistant", None, calls, None
        if getattr(msg, "type", "") == "system":
            return None, None, [], None
        return None, None, [], None

    def _maybe_summarize(self) -> None:
        """历史过长时压缩为摘要（写入 context_summary，不删消息）。"""
        state = self.get_state()
        messages = state.get("messages") or []
        if not messages or not needs_summary(messages):
            return
        existing = state.get("context_summary") or ""
        try:
            summary, consumed = summarize_history(
                get_planner_llm(), messages, existing_summary=existing
            )
        except Exception as e:
            log.warning("上下文摘要失败: %s", e)
            return
        if not summary:
            return
        try:
            self.graph.update_state(
                self.config,
                {
                    "context_summary": summary,
                    "summarized_upto": int(state.get("summarized_upto") or 0) + consumed,
                },
            )
            log.info("已压缩 %d 条历史为摘要（%d 字）", consumed, len(summary))
        except Exception as e:
            log.warning("写入摘要失败: %s", e)

    # ---------------- 便捷操作 ----------------

    def rename(self, title: str) -> None:
        self.store.rename(self.conversation.id, title)
        self.conversation.title = title

    def clear(self) -> None:
        """清空本会话的消息与图状态（保留会话本身）。"""
        self.store.clear_messages(self.conversation.id)
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            if isinstance(self.checkpointer, SqliteSaver):
                self.checkpointer.delete_thread(self.conversation.id)
            else:
                self.checkpointer.delete_thread(self.conversation.id)
        except Exception as e:
            log.debug("删除线程检查点失败: %s", e)
        self._sync_pending_from_graph()

    def __repr__(self) -> str:
        return f"<AgentSession {self.conversation_id[:8]} {self.conversation.display_title()!r}>"


# ============================================================
# 会话管理便捷函数
# ============================================================

def list_conversations(limit: int = 20) -> list[Conversation]:
    """列出最近的会话。"""
    return get_conversation_store().list(limit=limit)


def delete_conversation(conversation_id: str) -> bool:
    """删除会话及其消息。"""
    store = get_conversation_store()
    ok = store.delete(conversation_id)
    try:
        get_checkpointer().delete_thread(conversation_id)
    except Exception:
        pass
    return ok


def create_session(title: str = "") -> AgentSession:
    """新建会话。"""
    return AgentSession.new(title=title)


def open_session(conversation_id: Optional[str] = None) -> AgentSession:
    """打开（或新建）会话。"""
    return AgentSession.open(conversation_id=conversation_id)


__all__ = [
    "AgentSession",
    "TurnResult",
    "list_conversations",
    "delete_conversation",
    "create_session",
    "open_session",
]
