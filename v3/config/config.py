"""v3 全局配置。

所有可调参数集中在此。优先级：进程环境变量 > v3/.env > 仓库根 .env > 代码默认值。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


# ============================================================
# .env 加载
# ============================================================

_V3_ROOT = Path(__file__).resolve().parent.parent          # .../v3
_REPO_ROOT = _V3_ROOT.parent                                # .../AutoEvo_agent


def _load_env() -> Optional[Path]:
    """按优先级加载 .env，返回实际加载的文件（用于诊断）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None

    loaded: Optional[Path] = None
    # 先加载仓库根，再加载 v3 本地（后者覆盖前者）
    for candidate in (_REPO_ROOT / ".env", _V3_ROOT / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=True)
            loaded = candidate
    return loaded


ENV_FILE = _load_env()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


# ============================================================
# 配置
# ============================================================

class Settings:
    """应用配置。"""

    # ---------------- 应用 ----------------
    APP_NAME: str = os.getenv("APP_NAME", "AutoEvo Agent")
    APP_VERSION: str = "0.1.0"

    # ---------------- LLM ----------------
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL: Optional[str] = os.getenv("OPENAI_BASE_URL")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_TEMPERATURE: float = _env_float("OPENAI_TEMPERATURE", 0.1)
    OPENAI_MAX_TOKENS: Optional[int] = (
        int(os.getenv("OPENAI_MAX_TOKENS")) if os.getenv("OPENAI_MAX_TOKENS") else None
    )
    # 规划 / 反思这类结构化任务用更低的温度
    PLANNER_TEMPERATURE: float = _env_float("PLANNER_TEMPERATURE", 0.0)
    LLM_TIMEOUT: float = _env_float("LLM_TIMEOUT", 120.0)
    LLM_MAX_RETRIES: int = _env_int("LLM_MAX_RETRIES", 2)

    # ---------------- 路径 ----------------
    DATA_DIR: Path = Path(os.getenv("V3_DATA_DIR", str(_V3_ROOT / ".data")))
    SKILLS_DIR: Path = Path(os.getenv("SKILLS_DIR", str(_V3_ROOT / "skills")))
    AUDIT_DIR: Path = DATA_DIR / "audit"

    # ---------------- Agent 主循环 ----------------
    MAX_ITERATIONS: int = _env_int("MAX_ITERATIONS", 25)
    """单个用户回合内 agent ⇄ tools 循环的最大轮数。"""

    MAX_PLAN_STEPS: int = _env_int("MAX_PLAN_STEPS", 12)
    TOOL_TIMEOUT: int = _env_int("TOOL_TIMEOUT", 120)
    MAX_OUTPUT: int = _env_int("MAX_OUTPUT", 6000)
    MAX_RETRIES: int = _env_int("MAX_RETRIES", 2)

    # ---------------- 上下文工程 ----------------
    CONTEXT_MAX_TOKENS: int = _env_int("CONTEXT_MAX_TOKENS", 24000)
    """送进 LLM 的历史消息 token 预算（不含 system / 工具定义）。"""

    CONTEXT_KEEP_RECENT: int = _env_int("CONTEXT_KEEP_RECENT", 12)
    """无论如何都保留的最近消息条数。"""

    CONTEXT_SUMMARY_TRIGGER: int = _env_int("CONTEXT_SUMMARY_TRIGGER", 30)
    """消息条数超过该值时触发摘要压缩。"""

    # ---------------- 持久化 shell ----------------
    SHELL_PERSISTENT: bool = _env_bool("SHELL_PERSISTENT", True)
    SHELL_PATH: Optional[str] = os.getenv("SHELL_PATH") or None
    SHELL_STARTUP_TIMEOUT_S: float = _env_float("SHELL_STARTUP_TIMEOUT_S", 10.0)

    # ---------------- 安全检查 ----------------
    SAFETY_MODE: str = os.getenv("SAFETY_MODE", "normal")
    """strict | normal | yolo"""

    SAFETY_ALLOW_SUDO: bool = _env_bool("SAFETY_ALLOW_SUDO", False)
    SAFETY_ALLOW_NETWORK: bool = _env_bool("SAFETY_ALLOW_NETWORK", True)
    SAFETY_ALLOW_WRITE: bool = _env_bool("SAFETY_ALLOW_WRITE", True)
    SAFETY_CONFIRM_TIMEOUT_S: float = _env_float("SAFETY_CONFIRM_TIMEOUT_S", 300.0)

    # ---------------- RAG / 向量库 ----------------
    RAG_ENABLED: bool = _env_bool("RAG_ENABLED", True)
    RAG_TOP_K: int = _env_int("RAG_TOP_K", 4)
    RAG_MIN_SCORE: float = _env_float("RAG_MIN_SCORE", 0.15)
    RAG_CHUNK_SIZE: int = _env_int("RAG_CHUNK_SIZE", 800)
    RAG_CHUNK_OVERLAP: int = _env_int("RAG_CHUNK_OVERLAP", 120)
    RAG_MAX_CHARS_PER_HIT: int = _env_int("RAG_MAX_CHARS_PER_HIT", 1600)

    VECTOR_BACKEND: str = os.getenv("VECTOR_BACKEND", "auto")
    """auto | sqlite_vec | numpy"""

    # 嵌入：provider=auto 时优先尝试 OpenAI 兼容接口，失败则退回本地哈希嵌入
    EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "auto")
    """auto | openai | local"""
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "embedding-3")
    EMBEDDING_BASE_URL: Optional[str] = (
        os.getenv("EMBEDDING_BASE_URL") or OPENAI_BASE_URL
    )
    EMBEDDING_API_KEY: Optional[str] = (
        os.getenv("EMBEDDING_API_KEY") or OPENAI_API_KEY
    )
    EMBEDDING_DIM: int = _env_int("EMBEDDING_DIM", 1024)
    """本地兜底嵌入的维度（也用于探测远端维度失败时的默认值）。"""
    EMBEDDING_TIMEOUT: float = _env_float("EMBEDDING_TIMEOUT", 30.0)

    # ---------------- 技能 / 自进化 ----------------
    SKILL_AUTO_REFLECT: bool = _env_bool("SKILL_AUTO_REFLECT", True)
    """任务结束后是否自动触发技能反思。"""
    SKILL_REFLECT_MIN_TOOL_CALLS: int = _env_int("SKILL_REFLECT_MIN_TOOL_CALLS", 1)
    """本回合工具调用数低于此值则跳过反思（避免琐事打扰）。"""
    SKILL_MAX_PROPOSALS: int = _env_int("SKILL_MAX_PROPOSALS", 5)
    SKILL_LOADER_TOP_N: int = _env_int("SKILL_LOADER_TOP_N", 3)
    """每轮最多自动载入几个技能。"""

    # ---------------- 会话持久化 ----------------
    CONVERSATIONS_DB: Path = DATA_DIR / "conversations.sqlite"
    CHECKPOINT_DB: Path = DATA_DIR / "checkpoints.sqlite"
    CHECKPOINT_BACKEND: str = os.getenv("CHECKPOINT_BACKEND", "auto")
    """auto | sqlite | memory"""

    # ---------------- UI ----------------
    UI_STREAM: bool = _env_bool("UI_STREAM", True)
    WEB_PORT: int = _env_int("WEB_PORT", 7860)
    WEB_SHARE: bool = _env_bool("WEB_SHARE", False)

    # ---------------- 派生 ----------------
    @classmethod
    def llm_config(cls, temperature: Optional[float] = None) -> dict:
        cfg: dict = {
            "model": cls.OPENAI_MODEL,
            "temperature": (
                temperature if temperature is not None else cls.OPENAI_TEMPERATURE
            ),
            "api_key": cls.OPENAI_API_KEY,
            "base_url": cls.OPENAI_BASE_URL,
            "timeout": cls.LLM_TIMEOUT,
            "max_retries": cls.LLM_MAX_RETRIES,
        }
        if cls.OPENAI_MAX_TOKENS:
            cfg["max_tokens"] = cls.OPENAI_MAX_TOKENS
        return cfg

    @classmethod
    def ensure_dirs(cls) -> None:
        """确保所有数据目录存在。"""
        for path in (
            cls.DATA_DIR,
            cls.SKILLS_DIR,
            cls.AUDIT_DIR,
            cls.SKILLS_DIR / ".archive",
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    @classmethod
    def describe(cls) -> str:
        """返回一段人类可读的配置摘要（用于 /status）。"""
        return "\n".join(
            [
                f"模型           : {cls.OPENAI_MODEL}",
                f"接口           : {cls.OPENAI_BASE_URL or '(默认)'}",
                f"温度           : {cls.OPENAI_TEMPERATURE} (规划 {cls.PLANNER_TEMPERATURE})",
                f"数据目录       : {cls.DATA_DIR}",
                f"技能目录       : {cls.SKILLS_DIR}",
                f"持久化 shell   : {cls.SHELL_PERSISTENT}",
                f"安全模式       : {cls.SAFETY_MODE}",
                f"RAG            : {cls.RAG_ENABLED} (top_k={cls.RAG_TOP_K}, backend={cls.VECTOR_BACKEND})",
                f"嵌入           : {cls.EMBEDDING_PROVIDER} / {cls.EMBEDDING_MODEL}",
                f"技能自反思     : {cls.SKILL_AUTO_REFLECT}",
                f"检查点后端     : {cls.CHECKPOINT_BACKEND}",
                f"最大迭代       : {cls.MAX_ITERATIONS}",
                f"上下文预算     : {cls.CONTEXT_MAX_TOKENS} tokens",
            ]
        )


settings = Settings()

__all__ = ["settings", "Settings", "ENV_FILE"]
