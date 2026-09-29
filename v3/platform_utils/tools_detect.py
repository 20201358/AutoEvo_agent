"""探测系统可用工具。

按平台给出最常用的开发者工具候选清单，并通过 ``shutil.which`` 判断是否已安装。
用于：
  - 在 plan 阶段告诉 LLM 当前机器能用什么（避免规划出无法执行的步骤）
  - 给 ``devtools_tool`` 提供事实依据
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional


# ============================================================
# 候选清单（按"工具类别"组织，便于决策）
# ============================================================

DEVTOOL_CATALOG = [
    # ---- 语言运行时 ----
    {"key": "python", "category": "runtime", "platform": "all",
     "binaries": ["python", "python3", "python.exe"],
     "purpose": "Python 解释器"},
    {"key": "node", "category": "runtime", "platform": "all",
     "binaries": ["node", "node.exe"],
     "purpose": "Node.js 运行时"},
    {"key": "java", "category": "runtime", "platform": "all",
     "binaries": ["java", "java.exe"],
     "purpose": "Java 运行时"},
    {"key": "go", "category": "runtime", "platform": "all",
     "binaries": ["go", "go.exe"],
     "purpose": "Go 编译器与工具链"},
    {"key": "rust", "category": "runtime", "platform": "all",
     "binaries": ["rustc", "rustc.exe", "cargo", "cargo.exe"],
     "purpose": "Rust 工具链"},
    {"key": "ruby", "category": "runtime", "platform": "all",
     "binaries": ["ruby", "ruby.exe"],
     "purpose": "Ruby 解释器"},

    # ---- C/C++ ----
    {"key": "gcc", "category": "compiler", "platform": "all",
     "binaries": ["gcc", "gcc.exe"],
     "purpose": "GNU C 编译器"},
    {"key": "gpp", "category": "compiler", "platform": "all",
     "binaries": ["g++", "g++.exe"],
     "purpose": "GNU C++ 编译器"},
    {"key": "clang", "category": "compiler", "platform": "all",
     "binaries": ["clang", "clang.exe", "clang++", "clang++.exe"],
     "purpose": "LLVM clang 编译器"},
    {"key": "msvc", "category": "compiler", "platform": "win32",
     "binaries": ["cl.exe"],
     "purpose": "MSVC 编译器"},
    {"key": "make", "category": "build", "platform": "all",
     "binaries": ["make", "make.exe", "mingw32-make.exe"],
     "purpose": "Make 构建工具"},
    {"key": "cmake", "category": "build", "platform": "all",
     "binaries": ["cmake", "cmake.exe"],
     "purpose": "CMake 构建系统"},
    {"key": "ninja", "category": "build", "platform": "all",
     "binaries": ["ninja", "ninja.exe"],
     "purpose": "Ninja 构建系统"},

    # ---- 包管理 ----
    {"key": "pip", "category": "package", "platform": "all",
     "binaries": ["pip", "pip3", "pip.exe"],
     "purpose": "Python 包管理"},
    {"key": "uv", "category": "package", "platform": "all",
     "binaries": ["uv", "uv.exe"],
     "purpose": "Python 包管理器 uv"},
    {"key": "conda", "category": "package", "platform": "all",
     "binaries": ["conda", "conda.exe"],
     "purpose": "Conda 环境管理"},
    {"key": "npm", "category": "package", "platform": "all",
     "binaries": ["npm", "npm.cmd", "npm.exe"],
     "purpose": "Node 包管理"},
    {"key": "pnpm", "category": "package", "platform": "all",
     "binaries": ["pnpm", "pnpm.exe"],
     "purpose": "Node 包管理 pnpm"},
    {"key": "yarn", "category": "package", "platform": "all",
     "binaries": ["yarn", "yarn.cmd", "yarn.exe"],
     "purpose": "Node 包管理 yarn"},
    {"key": "maven", "category": "package", "platform": "all",
     "binaries": ["mvn", "mvn.cmd", "mvn.exe"],
     "purpose": "Maven 构建工具"},
    {"key": "gradle", "category": "package", "platform": "all",
     "binaries": ["gradle", "gradle.exe"],
     "purpose": "Gradle 构建工具"},

    # ---- 版本控制 ----
    {"key": "git", "category": "vcs", "platform": "all",
     "binaries": ["git", "git.exe"],
     "purpose": "Git 版本控制"},

    # ---- 容器 / 虚拟化 ----
    {"key": "docker", "category": "container", "platform": "all",
     "binaries": ["docker", "docker.exe"],
     "purpose": "Docker 容器"},
    {"key": "podman", "category": "container", "platform": "all",
     "binaries": ["podman", "podman.exe"],
     "purpose": "Podman 容器"},

    # ---- 网络 ----
    {"key": "curl", "category": "network", "platform": "all",
     "binaries": ["curl", "curl.exe"],
     "purpose": "curl 命令行下载工具"},
    {"key": "wget", "category": "network", "platform": "all",
     "binaries": ["wget", "wget.exe"],
     "purpose": "wget 下载工具"},

    # ---- 压缩 / 解压 ----
    {"key": "tar", "category": "archive", "platform": "all",
     "binaries": ["tar", "tar.exe"],
     "purpose": "tar 归档工具"},
    {"key": "unzip", "category": "archive", "platform": "all",
     "binaries": ["unzip", "unzip.exe"],
     "purpose": "unzip 解压工具"},
    {"key": "zip", "category": "archive", "platform": "all",
     "binaries": ["zip", "zip.exe"],
     "purpose": "zip 压缩工具"},
    {"key": "7z", "category": "archive", "platform": "all",
     "binaries": ["7z", "7z.exe"],
     "purpose": "7-Zip 高压缩率归档"},
    {"key": "gzip", "category": "archive", "platform": "all",
     "binaries": ["gzip", "gzip.exe"],
     "purpose": "gzip 压缩工具"},

    # ---- 文档 / Office ----
    {"key": "pandoc", "category": "document", "platform": "all",
     "binaries": ["pandoc", "pandoc.exe"],
     "purpose": "Pandoc 文档格式转换"},

    # ---- 媒体 ----
    {"key": "ffmpeg", "category": "media", "platform": "all",
     "binaries": ["ffmpeg", "ffmpeg.exe"],
     "purpose": "FFmpeg 音视频处理"},

    # ---- 包安装器（OS 级）---- ----
    {"key": "winget", "category": "os_pkg", "platform": "win32",
     "binaries": ["winget", "winget.exe"],
     "purpose": "Windows 包管理器"},
    {"key": "choco", "category": "os_pkg", "platform": "win32",
     "binaries": ["choco", "choco.exe"],
     "purpose": "Chocolatey Windows 包管理器"},
    {"key": "scoop", "category": "os_pkg", "platform": "win32",
     "binaries": ["scoop", "scoop.cmd"],
     "purpose": "Scoop Windows 包管理器"},
    {"key": "apt", "category": "os_pkg", "platform": "linux",
     "binaries": ["apt", "apt-get"],
     "purpose": "Debian/Ubuntu 包管理器"},
    {"key": "brew", "category": "os_pkg", "platform": "darwin",
     "binaries": ["brew"],
     "purpose": "Homebrew macOS 包管理器"},
]


# 简单的快速探测清单（保留旧接口）
WINDOWS_CANDIDATES = [
    "powershell.exe", "pwsh.exe", "cmd.exe",
    "git.exe", "python.exe", "pip.exe",
    "docker.exe", "curl.exe", "node.exe", "npm.cmd",
    "winget.exe", "choco.exe", "scoop.cmd",
    "7z.exe", "tar.exe",
]

UNIX_CANDIDATES = [
    "bash", "sh", "zsh",
    "git", "python3", "pip3",
    "docker", "curl", "wget",
    "node", "npm",
    "apt", "apt-get", "dnf", "yum", "pacman",
    "brew",
    "tar", "gzip", "unzip",
    "sed", "awk", "grep",
]


# ============================================================
# 探测
# ============================================================

@dataclass
class DetectedTool:
    """一个被探测到的工具。"""

    key: str
    category: str
    binary: str
    path: str
    version: Optional[str] = None
    purpose: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def detect_tools() -> list[str]:
    """旧接口：列出当前平台能直接 ``shutil.which`` 到的可执行文件名。"""
    candidates = WINDOWS_CANDIDATES if sys.platform == "win32" else UNIX_CANDIDATES
    return [t for t in candidates if shutil.which(t)]


def _version_for(binary: str, timeout: float = 3.0) -> Optional[str]:
    """尝试抓取版本号（不保证成功）。"""
    for flag in ("--version", "-version", "-V"):
        try:
            out = subprocess.run(
                [binary, flag],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            text = (out.stdout or out.stderr or "").strip()
            if text:
                # 取第一行
                first = text.splitlines()[0]
                return first[:160]
        except Exception:
            continue
    return None


def detect_devtool(
    categories: Optional[list[str]] = None,
    include_unavailable: bool = False,
) -> list[dict]:
    """按"开发者关心的工具"维度扫描当前机器。

    Args:
        categories: 限定返回的类别（runtime / compiler / build / package / vcs /
            container / network / archive / document / media / os_pkg）。
            None 表示全部。
        include_unavailable: 是否包含未安装的工具（默认只返回已安装的）。

    Returns:
        列表，每项一个 dict：{key, category, binary, path, version, purpose}
    """
    results: list[DetectedTool] = []
    for spec in DEVTOOL_CATALOG:
        if spec["platform"] not in ("all", sys.platform):
            continue
        if categories and spec["category"] not in categories:
            continue
        binary = None
        path = None
        for cand in spec["binaries"]:
            p = shutil.which(cand)
            if p:
                binary = cand
                path = p
                break
        if not path and not include_unavailable:
            continue
        version = _version_for(binary) if binary else None
        results.append(
            DetectedTool(
                key=spec["key"],
                category=spec["category"],
                binary=binary or "",
                path=path or "",
                version=version,
                purpose=spec["purpose"],
            )
        )
    return [r.as_dict() for r in results]


def detect_summary() -> str:
    """人类可读的探测结果（给 plan 阶段用）。"""
    rows = detect_devtool()
    if not rows:
        return "(未探测到任何开发工具)"
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    lines: list[str] = []
    for cat in ("runtime", "compiler", "build", "package", "vcs",
                "network", "archive", "container", "document", "media", "os_pkg"):
        items = by_cat.get(cat)
        if not items:
            continue
        lines.append(f"### {cat}")
        for r in items:
            v = r["version"] or ""
            if v:
                v = " " + v.split("version", 1)[0].strip()[:50]
            lines.append(f"- {r['key']}: {r['binary']} ({r['path']}){v}")
        lines.append("")
    return "\n".join(lines).strip()


def find(binary: str) -> Optional[str]:
    """找某个可执行文件的绝对路径。"""
    return shutil.which(binary)


__all__ = [
    "DetectedTool",
    "DEVTOOL_CATALOG",
    "detect_tools",
    "detect_devtool",
    "detect_summary",
    "find",
]