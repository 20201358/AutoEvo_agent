"""对话存储（SQLite）。

保存两份数据：
    1. ``conversations`` 对话元数据：标题、创建/更新时间、消息数
    2. ``messages``      可读消息日志：角色、内容、工具调用信息

LangGraph 的状态检查点是另一套机制（见 ``checkpointer.py``），
两者用同一个 conversation_id 作为 thread_id 关联。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from ..config import settings

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# 数据结构
# ============================================================

@dataclass
class Conversation:
    """一条对话记录。"""

    id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Conversation":
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return cls(
            id=row["id"],
            title=row["title"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            message_count=row["message_count"] or 0,
            meta=meta,
        )

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def display_title(self) -> str:
        return self.title or "(未命名对话)"

    def __repr__(self) -> str:
        return f"<Conversation {self.short_id} {self.display_title()!r} msgs={self.message_count}>"


@dataclass
class StoredMessage:
    """一条持久化消息。"""

    id: int
    conversation_id: str
    role: str
    content: str
    name: Optional[str] = None
    tool_calls: list = field(default_factory=list)
    tool_call_id: Optional[str] = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "StoredMessage":
        try:
            tc = json.loads(row["tool_calls"] or "[]")
        except json.JSONDecodeError:
            tc = []
        return cls(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            content=row["content"] or "",
            name=row["name"],
            tool_calls=tc,
            tool_call_id=row["tool_call_id"],
            created_at=row["created_at"] or "",
        )

    def preview(self, n: int = 120) -> str:
        text = " ".join((self.content or "").split())
        return text[:n] + ("…" if len(text) > n else "")

    def __repr__(self) -> str:
        return f"<StoredMessage {self.role} {self.preview(40)!r}>"


# ============================================================
# 存储实现
# ============================================================

class ConversationStore:
    """基于 SQLite 的对话存储。"""

    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path or settings.CONVERSATIONS_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id            TEXT PRIMARY KEY,
                    title         TEXT NOT NULL DEFAULT '',
                    created_at    TEXT NOT NULL DEFAULT '',
                    updated_at    TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    meta          TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role            TEXT NOT NULL,
                    content         TEXT NOT NULL DEFAULT '',
                    name            TEXT,
                    tool_calls      TEXT NOT NULL DEFAULT '[]',
                    tool_call_id    TEXT,
                    created_at      TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conv
                    ON messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(updated_at DESC);
                """
            )
            self._conn.commit()

    # ---------------- 对话 ----------------

    def create(self, title: str = "", meta: Optional[dict] = None) -> Conversation:
        """新建一个对话。"""
        conv_id = uuid.uuid4().hex
        ts = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at, message_count, meta)"
                " VALUES(?,?,?,?,0,?)",
                (conv_id, title, ts, ts, json.dumps(meta or {}, ensure_ascii=False)),
            )
            self._conn.commit()
        return Conversation(
            id=conv_id, title=title, created_at=ts, updated_at=ts, meta=meta or {}
        )

    def get(self, conv_id: str) -> Optional[Conversation]:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        return Conversation.from_row(row) if row else None

    def get_or_create(self, conv_id: Optional[str], title: str = "") -> Conversation:
        """按 id 取对话，不存在则用该 id 创建；``conv_id`` 为空时新建一个。"""
        if not conv_id:
            return self.create(title=title)

        existing = self.get(conv_id)
        if existing:
            return existing

        ts = _now()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO conversations"
                "(id, title, created_at, updated_at, message_count, meta)"
                " VALUES(?,?,?,?,0,'{}')",
                (conv_id, title, ts, ts),
            )
            self._conn.commit()
        created = self.get(conv_id)
        return created if created else self.create(title=title)

    def list(self, limit: int = 50, offset: int = 0) -> list[Conversation]:
        """按最近更新倒序列出对话。"""
        rows = self._conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC, rowid DESC"
            " LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [Conversation.from_row(r) for r in rows]

    def latest(self) -> Optional[Conversation]:
        rows = self.list(limit=1)
        return rows[0] if rows else None

    def find_by_title(self, keyword: str, limit: int = 20) -> list[Conversation]:
        """按标题关键词模糊搜索。"""
        rows = self._conn.execute(
            "SELECT * FROM conversations WHERE title LIKE ?"
            " ORDER BY updated_at DESC LIMIT ?",
            (f"%{keyword}%", limit),
        ).fetchall()
        return [Conversation.from_row(r) for r in rows]

    def rename(self, conv_id: str, title: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, _now(), conv_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def touch(self, conv_id: str) -> None:
        """更新 updated_at。"""
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_now(), conv_id),
            )
            self._conn.commit()

    def delete(self, conv_id: str) -> bool:
        """删除对话及其消息。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conv_id,)
            )
            self._conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conv_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()
        return int(row["n"])

    # ---------------- 消息 ----------------

    def append_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
    ) -> int:
        """追加一条消息，返回消息 id。"""
        ts = _now()
        payload = json.dumps(tool_calls or [], ensure_ascii=False, default=str)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(conversation_id, role, content, name, tool_calls, tool_call_id, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (conv_id, role, content or "", name, payload, tool_call_id, ts),
            )
            self._conn.execute(
                "UPDATE conversations SET message_count = message_count + 1, updated_at = ?"
                " WHERE id = ?",
                (ts, conv_id),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def get_messages(
        self,
        conv_id: str,
        limit: Optional[int] = None,
        order: str = "asc",
    ) -> list[StoredMessage]:
        """读取对话消息。``limit`` 给定时取最近 N 条（再按 order 排序返回）。"""
        if limit and order == "asc":
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ?"
                " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (conv_id, limit),
            ).fetchall()
        else:
            direction = "DESC" if order.lower() == "desc" else "ASC"
            sql = (
                f"SELECT * FROM messages WHERE conversation_id = ? ORDER BY id {direction}"
            )
            params: tuple = (conv_id,)
            if limit:
                sql += " LIMIT ?"
                params = (conv_id, limit)
            rows = self._conn.execute(sql, params).fetchall()
        return [StoredMessage.from_row(r) for r in rows]

    def message_count(self, conv_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (conv_id,)
        ).fetchone()
        return int(row["n"])

    def clear_messages(self, conv_id: str) -> int:
        """清空某对话的消息（保留对话本身）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conv_id,)
            )
            self._conn.execute(
                "UPDATE conversations SET message_count = 0, updated_at = ? WHERE id = ?",
                (_now(), conv_id),
            )
            self._conn.commit()
            return cur.rowcount

    # ---------------- 标题 ----------------

    def auto_title(self, conv_id: str, max_len: int = 30) -> str:
        """用第一条用户消息自动生成标题。"""
        rows = self._conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user'"
            " ORDER BY id ASC LIMIT 1",
            (conv_id,),
        ).fetchone()
        if not rows:
            return ""
        text = " ".join((rows["content"] or "").split())
        title = text[:max_len] + ("…" if len(text) > max_len else "")
        if title:
            self.rename(conv_id, title)
        return title

    # ---------------- 维护 ----------------

    def stats(self) -> dict:
        conv = self.count()
        msg = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        return {"conversations": conv, "messages": int(msg), "db": str(self.db_path)}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ============================================================
# 单例
# ============================================================

_store_lock = threading.Lock()
_store: Optional[ConversationStore] = None


def get_conversation_store(force_new: bool = False) -> ConversationStore:
    """获取全局对话存储。"""
    global _store
    with _store_lock:
        if _store is None or force_new:
            _store = ConversationStore()
        return _store


def reset_conversation_store() -> None:
    """关闭并丢弃全局对话存储。"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


__all__ = [
    "Conversation",
    "StoredMessage",
    "ConversationStore",
    "get_conversation_store",
    "reset_conversation_store",
]
