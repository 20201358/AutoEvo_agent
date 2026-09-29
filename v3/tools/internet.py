"""互联网工具：下载文件。

默认 ``requires_confirm=True``（下载到磁盘有副作用，且通常后续是安装/解压等敏感动作）。
"""

from __future__ import annotations

import os
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from ._base import register


def _socket_create_connection(*args, timeout):
    return socket.create_connection(*args, timeout=timeout)


@register(category="internet", requires_confirm=True, risk="medium")
def download_url(
    url: str,
    dest: str,
    overwrite: bool = False,
    timeout: float = 60.0,
    user_agent: str = "AutoEvo-Agent/3.0",
) -> str:
    """从 URL 下载文件到本地路径。

    **会发起真实网络请求**，默认要求用户确认。
    下载完成后请用 archive.extract_archive / installer.run_installer 等工具继续。

    Args:
        url: 要下载的 URL（仅 http/https）。
        dest: 目标路径（绝对或相对当前目录）。
        overwrite: 目标已存在时是否覆盖（默认 False，避免误覆盖）。
        timeout: 下载超时秒数。
        user_agent: User-Agent 头。

    Returns:
        简短文本，包含路径、大小、HTTP 状态。
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return "错误：仅支持 http/https URL"

    target = Path(dest).expanduser().resolve()
    if target.exists() and not overwrite:
        return f"错误：目标已存在 -> {target}（如需覆盖请设 overwrite=true）"

    target.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": user_agent})

    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status_code = resp.getcode()
            length = resp.headers.get("Content-Length")
            tmp_path = target.with_suffix(target.suffix + ".part")
            written = 0
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
            os.replace(tmp_path, target)
    except urllib.error.HTTPError as e:
        return f"错误：HTTP {e.code} {e.reason} -> {url}"
    except urllib.error.URLError as e:
        return f"错误：URL 错误 -> {e.reason} ({url})"
    except (socket.timeout, TimeoutError):
        return f"错误：下载超时（>{timeout}s）-> {url}"
    except Exception as e:
        return f"错误：{type(e).__name__}: {e}"

    size_h = f"{written} B"
    if written >= 1024 * 1024:
        size_h = f"{written / (1024 * 1024):.2f} MB"
    elif written >= 1024:
        size_h = f"{written / 1024:.2f} KB"

    length_note = f" (Content-Length {length})" if length else ""
    return (
        f"已下载 {size_h}{length_note} -> {target}\n"
        f"HTTP {status_code} | {url}"
    )


# 兼容
def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["download_url"]