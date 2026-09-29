"""日志配置。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


_CONFIGURED = False


def setup_logging(
    level: int = logging.INFO,
    log_file: Path | None = None,
    quiet_console: bool = False,
) -> None:
    """初始化 root logger。

    Args:
        level: 日志级别。
        log_file: 可选的文件输出路径。
        quiet_console: True 时控制台只输出 WARNING 及以上（CLI 场景避免刷屏）。
    """
    global _CONFIGURED

    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(logging.WARNING if quiet_console else level)
    root.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)

    root.setLevel(level)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取 logger，未初始化时用默认配置兜底。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger"]
