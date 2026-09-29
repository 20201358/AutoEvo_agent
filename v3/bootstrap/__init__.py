"""启动时一次性初始化。

对外暴露：
    - bootstrap_environment() : 探测机器环境，返回 EnvironmentInfo
    - bootstrap_safety()      : 生成默认安全策略，返回 SafetyPolicy

``bootstrap_all()`` 会结合 ``config.settings`` 一次性产出两者，供 CLI / Web 入口使用。
"""

from .environment import bootstrap_environment
from .safety_defaults import (
    UNIX_CONFIRM_TOKENS,
    UNIX_DENIED_PATTERNS,
    WINDOWS_CONFIRM_TOKENS,
    WINDOWS_DENIED_PATTERNS,
    bootstrap_safety,
)


def bootstrap_all() -> tuple[dict, dict]:
    """按当前配置生成 (environment, safety)。"""
    from ..config import settings

    env = bootstrap_environment()
    safety = bootstrap_safety(
        mode=settings.SAFETY_MODE,
        allow_sudo=settings.SAFETY_ALLOW_SUDO,
        allow_network=settings.SAFETY_ALLOW_NETWORK,
        allow_write=settings.SAFETY_ALLOW_WRITE,
    )
    return env, safety


__all__ = [
    "bootstrap_environment",
    "bootstrap_safety",
    "bootstrap_all",
    "WINDOWS_DENIED_PATTERNS",
    "UNIX_DENIED_PATTERNS",
    "WINDOWS_CONFIRM_TOKENS",
    "UNIX_CONFIRM_TOKENS",
]
