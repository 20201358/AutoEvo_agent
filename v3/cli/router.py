"""输入路由：把用户输入分类成 v3 命令 / 终端命令 / 自然语言。

约定：

* ``v3``          —— 单独敲 ``v3`` 不进任何会话，进入 list/help 模式。
* ``v3help`` / ``v3 list`` 等 —— ``v3`` 前缀的内置命令（与 shell 命令分隔）。
* 其它非 ``v3`` 前缀输入 —— 看起来像 shell 命令就直跑 shell，
  看起来像自然语言就发到 agent（新建或继续会话）。

设计目标：

* 不引入额外延迟（不再"先跑 shell 看是否 command not found"再判断）。
* 用白名单 + 元字符规则识别 shell，避免误判自然语言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ============================================================
# 常见 shell 命令白名单（按平台细分）
# ============================================================

# 通用命令（跨平台常用）
_COMMON_SHELL_CMDS = {
    # 文件 / 目录
    "ls", "dir", "pwd", "cd", "mkdir", "rmdir", "rm", "del", "rd",
    "cp", "copy", "mv", "move", "ren", "rename",
    "cat", "type", "more", "less", "head", "tail",
    "touch", "echo", "printf",
    "find", "findstr", "tree", "ls",
    "stat", "file", "du", "df",
    "chmod", "chown", "attrib", "icacls",
    # 查看 / 查找
    "which", "where", "whereis", "type",
    "grep", "rg", "ag", "ack", "select-string", "sls",
    "sort", "uniq", "wc", "cut", "awk", "sed",
    # 系统 / 进程
    "ps", "tasklist", "kill", "taskkill", "killall",
    "top", "htop", "taskmgr",
    "env", "set", "export", "unset", "printenv",
    "whoami", "hostname", "who", "id", "uptime", "date",
    "uname", "ver", "systeminfo",
    "ipconfig", "ifconfig", "ping", "tracert", "traceroute",
    "netstat", "nslookup", "dig", "arp", "route",
    "sc", "net", "wmic", "gpresult", "gpupdate",
    # 文本处理
    "diff", "fc", "compare", "comp", "comm",
    "xargs", "tee",
    # 网络 / 远程
    "ssh", "scp", "rsync", "ftp", "curl", "wget", "httpie",
    # 包管理 / 工具
    "git", "svn", "hg",
    "pip", "pip3", "pipx", "conda", "npm", "yarn", "pnpm", "bun", "cargo",
    "docker", "kubectl", "helm",
    "make", "cmake", "ninja", "gradle", "mvn",
    # 压缩
    "tar", "zip", "unzip", "7z", "gzip", "gunzip", "compress",
    # 显示
    "cls", "clear", "color",
    # 路径切换
    "pushd", "popd",
    # 终端 / 编辑器
    "code", "notepad", "vim", "vi", "nano", "emacs",
    # 用户常见
    "chcp", "title", "prompt", "logoff", "shutdown", "restart",
    "exit", "logout", "history",
    "bash", "sh", "zsh", "fish",
    "powershell", "pwsh", "cmd",
    # 编程语言运行（注意：python / node 这种 REPL 命令会被 shell 启动交互式会话
    # —— 但用户敲 "python foo.py" 这种带参的就合理。我们只把"裸词"识别为可疑）
    "perl", "ruby", "lua", "go", "rustc",
    # 其它常见
    "tasklist", "taskkill", "timeout", "pause",
}


# 黑名单：这些命令即使敲了也"应该"被 agent 处理（避免误跑交互式 REPL）
# 如果用户真的想跑 python foo.py，走 shell；
# 但 "python"（无参数）会进入 REPL，所以自然语言里含 "python" 时仍走 agent。
_BLACKLIST_CMDS = set()  # 暂不黑名单（白名单已收窄）


# shell 元字符：出现任意一个强烈暗示是 shell 命令
_SHELL_METACHARS = set("|<>;$&*?`")

# 路径前缀：./ .\\ / ~/ 开头一定是 shell 命令
_PATH_PREFIXES = ("./", ".\\", "/", "~/")


@dataclass
class RouteResult:
    """路由结果。"""

    kind: str           # "v3" | "shell" | "natural"
    payload: str = ""   # v3: 去掉 v3 前缀的剩余；shell: 完整命令；natural: 原始输入
    v3_cmd: str = ""    # v3 命令名（list/once/help/...）
    v3_args: str = ""   # v3 命令的参数

    @property
    def is_v3(self) -> bool:
        return self.kind == "v3"

    @property
    def is_shell(self) -> bool:
        return self.kind == "shell"

    @property
    def is_natural(self) -> bool:
        return self.kind == "natural"


# v3 前缀正则：
#   ^v3(\b|$)       —— "v3" 单独（无后缀或后接空白）
#   ^v3[0-9a-z]+    —— "v3help" / "v3list" 这种（紧贴）
_V3_PREFIX = re.compile(r"^v3(?=$|\s|[0-9a-z])", re.IGNORECASE)
# v3 命令名（v3 之后的连续字母数字）
_V3_CMD_NAME = re.compile(r"^v3([0-9a-z]+)\b(.*)$", re.IGNORECASE | re.DOTALL)


def classify(text: str) -> RouteResult:
    """把一行输入分成 v3 / shell / natural。"""
    raw = text or ""
    s = raw.strip()
    if not s:
        return RouteResult(kind="natural", payload="")

    # ---- v3 前缀 ----
    if _V3_PREFIX.match(s):
        # "v3" 单独 / "v3 list" / "v3list" / "v3once 4dc7"
        m = _V3_CMD_NAME.match(s)
        if m:
            cmd = m.group(1).lower()
            args = m.group(2).strip()
            return RouteResult(
                kind="v3",
                payload=s,
                v3_cmd=cmd,
                v3_args=args,
            )
        # 仅 "v3" 一个词
        if s.lower() == "v3":
            return RouteResult(kind="v3", payload=s, v3_cmd="", v3_args="")
        # "v3 list" / "v3 help" 这种：v3 后是空格 + 单词
        rest = s[2:].strip()  # 去掉 v3
        parts = rest.split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        return RouteResult(
            kind="v3",
            payload=s,
            v3_cmd=cmd,
            v3_args=args,
        )

    # ---- 看起来像 shell 命令？ ----
    if _looks_like_shell(s):
        return RouteResult(kind="shell", payload=s)

    # ---- 自然语言 ----
    return RouteResult(kind="natural", payload=s)


def _looks_like_shell(s: str) -> bool:
    """判断一行输入是否"看起来像 shell 命令"。

    启发式：
    1. 路径前缀（./foo / ~/bar / /bin/ls）→ shell
    2. 含 shell metacharacter（| & > < ; $ `）→ shell
    3. 第一个 token（按空白切）在白名单 → shell
    4. 否则 → 不是 shell
    """
    # 1. 路径前缀
    low = s.lower()
    for p in _PATH_PREFIXES:
        if s.startswith(p):
            return True

    # 2. metacharacter
    if any(c in s for c in _SHELL_METACHARS):
        return True

    # 3. 白名单首词
    first = re.split(r"\s+", s, maxsplit=1)[0].lower()
    # 去掉路径前缀后剩下的（例如 ./foo.sh）
    base = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base in _COMMON_SHELL_CMDS:
        return True

    return False


# ============================================================
# v3 命令别名表（一个命令多个输入形式）
# ============================================================

# 内部统一名字 → 用户可敲的形式集合
V3_ALIASES = {
    "help":     {"help", "h", "?"},
    "list":     {"list", "ls", "l"},
    "once":     {"once", "o", "open", "resume"},
    "new":      {"new", "n"},
    "rename":   {"rename", "rn", "title", "name"},
    "clear":    {"clear", "clr", "reset"},
    "tools":    {"tools", "tool", "t"},
    "shell":    {"shell", "sh"},
    "config":   {"config", "cfg", "c"},
    "conv":     {"conv", "history", "hist", "msg", "msgs"},
    "skills":   {"skills", "skill", "sk"},
    "proposals":{"proposals", "prop", "p"},
    "state":    {"state", "st", "status"},
    "exit":     {"exit", "quit", "q", "bye"},
}


def resolve_v3_alias(cmd: str) -> Optional[str]:
    """把用户敲的 v3 命令别名解析成内部统一名（找不到返回 None）。"""
    if not cmd:
        return None
    c = cmd.lower()
    for canonical, aliases in V3_ALIASES.items():
        if c in aliases:
            return canonical
    return None