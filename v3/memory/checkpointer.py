"""LangGraph 状态检查点。

优先使用 SQLite 检查点（跨进程可恢复）；``langgraph-checkpoint-sqlite`` 不可用时
退回 ``InMemorySaver``（进程内有效，历史仍可通过对话存储回看）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger(__name__)

_lock = threading.RLock()
_checkpointer = None
_conn: Optional[sqlite3.Connection] = None
_backend: str = "uninitialized"


# ============================================================
# 后端可用性
# ============================================================

def sqlite_available() -> bool:
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401
        return True
    except Exception:
        return False


def _make_sqlite_saver(db_path: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    return saver, conn


# ============================================================
# 工厂
# ============================================================

def get_checkpointer():
    """返回全局检查点保存器（带缓存）。"""
    global _checkpointer, _conn, _backend

    with _lock:
        if _checkpointer is not None:
            return _checkpointer

        backend = (settings.CHECKPOINT_BACKEND or "auto").lower()
        db_path = Path(settings.CHECKPOINT_DB)

        if backend in ("auto", "sqlite") and sqlite_available():
            try:
                _checkpointer, _conn = _make_sqlite_saver(db_path)
                _backend = "sqlite"
                log.info("检查点后端: sqlite (%s)", db_path)
                return _checkpointer
            except Exception as e:
                if backend == "sqlite":
                    raise
                log.warning("SQLite 检查点不可用（%s），回退到内存检查点", e)

        from langgraph.checkpoint.memory import InMemorySaver

        _checkpointer = InMemorySaver()
        _backend = "memory"
        log.info("检查点后端: memory")
        return _checkpointer


def get_sqlite_conn() -> Optional[sqlite3.Connection]:
    """返回底层 SQLite 连接（内存后端时为 None）。"""
    get_checkpointer()
    return _conn


def checkpointer_backend() -> str:
    """返回当前后端名称。"""
    get_checkpointer()
    return _backend


def reset_checkpointer() -> None:
    """丢弃并关闭检查点（切换配置或测试时使用）。"""
    global _checkpointer, _conn, _backend
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _checkpointer = None
        _conn = None
        _backend = "uninitialized"


def close_checkpointer() -> None:
    """进程退出时释放资源。"""
    global _checkpointer, _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        _checkpointer = None


__all__ = [
    "get_checkpointer",
    "get_sqlite_conn",
    "checkpointer_backend",
    "sqlite_available",
    "reset_checkpointer",
    "close_checkpointer",
]
