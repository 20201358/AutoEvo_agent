"""会话持久化层。

- ``conversation_store`` 维护对话元数据与可读消息日志（列表 / 重命名 / 删除 / 回看）。
- ``checkpointer``       为 LangGraph 提供状态持久化，使同一 thread_id 可跨进程恢复。
"""

from .checkpointer import (
    checkpointer_backend,
    close_checkpointer,
    get_checkpointer,
    get_sqlite_conn,
    reset_checkpointer,
    sqlite_available,
)
from .conversation_store import (
    Conversation,
    ConversationStore,
    StoredMessage,
    get_conversation_store,
    reset_conversation_store,
)

__all__ = [
    "Conversation",
    "StoredMessage",
    "ConversationStore",
    "get_conversation_store",
    "reset_conversation_store",
    "get_checkpointer",
    "get_sqlite_conn",
    "checkpointer_backend",
    "sqlite_available",
    "reset_checkpointer",
    "close_checkpointer",
]
